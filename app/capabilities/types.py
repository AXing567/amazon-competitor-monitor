"""L4 · 能力层的类型定义。

## 为什么不把能力继续堆在 `app/agent/tools.py` 里

`tools.py` 原本把三件事混在一起：**给模型看的 schema**、**参数解析**、**业务实现**。
结果就是任何第二个入口（面板、飞书卡片、CLI）要用同一段逻辑，只能复制一遍。
而复制一遍之后，「面板显示的竞品数」和「Agent 回答的竞品数」就会在某次改动后悄悄分叉——
这类 bug 不会报错，只会让老板觉得「系统在瞎说」。

拆成这一层之后的关系是：

    L5A 飞书 / L5B 面板 / Agent 工具   ← 三个入口，都只做「组装上下文 + 转发」
                  ↓
    L4 能力层（本包）                  ← 唯一实现，参数校验 + 上下文 + 调下游 + 包返回
                  ↓
    L3 编排（pool / collector / settings_store / ...）

## 为什么 `CapContext` 要显式携带身份

面板上「切用户」之后动作记到谁名下、阈值按谁的覆盖层解析，取决于「当前用户」。
如果能力层去读一个模块级单例（`CURRENT_USER`），会同时踩两个坑：

1. **进程内状态会被多 worker 分叉**：uvicorn 起 4 个 worker 时，每个进程一份单例，
   面板请求打到哪个 worker 就按哪个 worker 记住的用户算——老板改完自己的阈值，
   下一个请求可能落到别人的上下文里。这种错只在并发下复现，查起来极其痛苦。
2. **无法追溯**：单例是"最后一次写入获胜"，出问题时没有任何办法还原
   「这个请求当时认为是哪个用户」。

所以身份是**入参**：`CallContext.user_id` 从 HTTP 头 / 消息事件一路显式传下来。
能力层永远不猜「现在是谁」，宁可拿不到（`user_id=None`）也不去全局变量里捡一个。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# ============================================================
# 调用来源（决定采集日志里的 trigger、动作记录里的 source）
# ============================================================

SOURCE_PANEL = "panel"          # 管理面板（人点的）
SOURCE_AGENT = "agent"          # 模型对话（Agent 替人点的）
SOURCE_FEISHU = "feishu_card"   # 飞书卡片按钮（人点的，但走回调）
SOURCE_SYSTEM = "system"        # 定时任务 / 脚本

SOURCES: tuple[str, ...] = (SOURCE_PANEL, SOURCE_AGENT, SOURCE_FEISHU, SOURCE_SYSTEM)

#: 来源 → `collect_runs.trigger`。写死在类型层，避免每个调用点各猜一次。
#: `agent` 单独成一档而不是并进 `manual`：健康页要能回答
#: 「这次采集是人点的，还是模型自己决定采的」——两者的失败模式完全不同。
TRIGGER_BY_SOURCE: dict[str, str] = {
    SOURCE_PANEL: "panel",
    SOURCE_AGENT: "agent",
    SOURCE_FEISHU: "agent",     # 卡片按钮的语义是"模型/用户在那个会话里要求采集"
    SOURCE_SYSTEM: "schedule",
}


# ============================================================
# 作用域 / 分组
# ============================================================

SCOPE_SHARED = "shared"           # 面板与 Agent 都能调
SCOPE_AGENT_ONLY = "agent_only"   # 只给模型用（面板调用一律 403）

SCOPES: tuple[str, ...] = (SCOPE_SHARED, SCOPE_AGENT_ONLY)

GROUP_QUERY = "query"
GROUP_WRITE = "write"
GROUP_JUDGE = "judge"
GROUP_META = "meta"

GROUPS: tuple[str, ...] = (GROUP_QUERY, GROUP_WRITE, GROUP_JUDGE, GROUP_META)

#: 参数类型（面板按它渲染控件，能力层按它做转换与范围校验）
T_STR = "str"
T_INT = "int"
T_FLOAT = "float"
T_BOOL = "bool"
T_JSON = "json"     # 任意 JSON：对象 / 数组 / 字符串形式的 JSON 都收
T_ENUM = "enum"

PARAM_TYPES: tuple[str, ...] = (T_STR, T_INT, T_FLOAT, T_BOOL, T_JSON, T_ENUM)


# ============================================================
# 上下文 / 参数规格 / 能力
# ============================================================


@dataclass(frozen=True)
class CapContext:
    """一次调用的上下文。**身份是入参，不是全局单例**（见模块文档）。"""

    tenant_id: str
    user_id: Optional[int] = None
    source: str = SOURCE_PANEL

    def with_source(self, source: str) -> "CapContext":
        """换来源但保留身份。用于「面板代理 Agent 的某个动作」这类转发场景。"""
        return CapContext(tenant_id=self.tenant_id, user_id=self.user_id, source=source)

    @property
    def trigger(self) -> str:
        """本上下文触发的采集应记成什么 trigger。"""
        return TRIGGER_BY_SOURCE.get(self.source, "manual")


@dataclass(frozen=True)
class ParamSpec:
    """一个参数的规格。面板直接拿 `label` / `help` / `enum` 渲染，不用自己写中文。"""

    key: str
    label: str
    type: str
    required: bool = False
    default: Any = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    unit: str = ""
    enum: Optional[list[str]] = None
    help: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "required": self.required,
            "default": self.default,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "unit": self.unit,
            "enum": list(self.enum) if self.enum else None,
            "help": self.help,
        }


@dataclass(frozen=True)
class Capability:
    """一个能力。`handler(ctx, **kwargs) -> dict`，永远返回带 `ok` 的 dict。

    `handler` 里**不许写业务判断**：只做「组装上下文 → 调下游服务 → 包装返回」。
    业务判断属于 L2/L3，放在这里会导致「同一个判断在面板和 Agent 两条路上各写一遍」，
    这正是本次重构要消灭的东西。
    """

    name: str
    scope: str
    group: str
    title: str
    description: str
    params: tuple[ParamSpec, ...] = field(default_factory=tuple)
    handler: Callable[..., dict] = field(default=lambda ctx, **kw: {"ok": True})

    def param(self, key: str) -> Optional[ParamSpec]:
        for spec in self.params:
            if spec.key == key:
                return spec
        return None

    def to_schema(self) -> dict:
        """给面板 / OpenAPI 用的自描述结构（不含 handler，不可序列化）。"""
        return {
            "name": self.name,
            "title": self.title,
            "scope": self.scope,
            "group": self.group,
            "description": self.description,
            "params": [p.to_dict() for p in self.params],
        }


# ============================================================
# 错误
# ============================================================

CODE_NOT_FOUND = "not_found"
CODE_FORBIDDEN = "forbidden"


class CapabilityError(Exception):
    """路由级错误：能力不存在 / 无权限。

    为什么单独一个异常而不是返回 dict：这两种错误在 HTTP 层要映射成 404 / 403，
    而业务失败必须是 200 + `ok:false`（前端不必区分网络错与业务错）。
    混在返回值里就没法在路由层干净地分开。
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "error",
        payload: Optional[dict] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


class ParamError(ValueError):
    """参数类型/范围不合法。属于业务失败（200 + ok:false），不是路由错误。"""
