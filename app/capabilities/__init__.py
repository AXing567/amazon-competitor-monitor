"""L4 · 能力层唯一入口。

    from app.capabilities import call, CapContext
    result = call("list_competitors", ctx=CapContext("default", user_id=1), status="active")

## 为什么必须只有一个入口

`execute_tool`（Agent 工具）和 `POST /api/cap/{name}`（面板）如果各走一条实现，
「面板显示 7 个竞品、Agent 说 6 个」这种账就会被记到「模型胡说」头上，而真相是
两条路的过滤条件不一样。把「解析参数 → 调 handler → 包装返回」收成一个函数之后，
两条路唯一的差别只剩**来源标记**（`ctx.source`），这个差别是显式可见的。

## 为什么参数校验集中在这里

1. `handler` 里就不用再写 `int(args.get("hours") or 24)` 这种重复兜底；
2. 面板可以靠 `ParamSpec` 自动渲染表单，校验规则与控件范围来自同一份定义；
3. 类型错误的返回格式统一（`{"ok": false, "error": "hours 需要整数，收到 'abc'"}`），
   模型读得懂，人读得懂，前端不用为每个能力写一套提示。

## 错误分层（与 HTTP 状态码一一对应）

| 情况 | 表现 | HTTP |
|---|---|---|
| 能力不存在 | 抛 `CapabilityError(code="not_found")` | 404 |
| 无权限（`agent_only` 被面板调用） | 抛 `CapabilityError(code="forbidden")` | 403 |
| 参数不合法 / 业务失败 | **返回** `{"ok": false, "error": ...}` | 200 |

业务失败一律 200：前端只需要判断一个 `ok` 字段，不必区分「网络断了」「参数错了」
「库空了」——这三种在用户眼里都是「没拿到结果」，分开处理只会让每个页面都写三遍分支。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app.capabilities import judgments, queries, writes
from app.capabilities.types import (
    CODE_FORBIDDEN,
    CODE_NOT_FOUND,
    GROUPS,
    PARAM_TYPES,
    SCOPE_AGENT_ONLY,
    SCOPES,
    SOURCE_AGENT,
    SOURCE_PANEL,
    TRIGGER_BY_SOURCE,
    CapContext,
    Capability,
    CapabilityError,
    ParamError,
    ParamSpec,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CapContext",
    "Capability",
    "CapabilityError",
    "ParamSpec",
    "ParamError",
    "call",
    "safe_call",
    "get",
    "list_capabilities",
    "to_schema",
    "tool_names",
    "counts",
    "CODE_NOT_FOUND",
    "CODE_FORBIDDEN",
    "SOURCE_AGENT",
    "SOURCE_PANEL",
    "TRIGGER_BY_SOURCE",
]


# ============================================================
# 注册表
# ============================================================

_ALL: tuple[Capability, ...] = (
    queries.CAPABILITIES + writes.CAPABILITIES + judgments.CAPABILITIES
)

_REGISTRY: dict[str, Capability] = {cap.name: cap for cap in _ALL}

# 启动期硬断言而不是注释：重名会让后注册的静默覆盖先注册的，
# 而"某个能力的实现莫名变成另一个"是最难查的一类 bug。
assert len(_REGISTRY) == len(_ALL), "能力重名：" + ", ".join(
    sorted({c.name for c in _ALL if sum(1 for x in _ALL if x.name == c.name) > 1})
)

# 参数类型拼错会让 `_coerce_value` 悄悄走到 str 分支，所以启动期就炸。
for _cap in _ALL:
    for _spec in _cap.params:
        assert _spec.type in PARAM_TYPES, f"{_cap.name}.{_spec.key} 类型非法：{_spec.type}"
        assert _cap.scope in SCOPES, f"{_cap.name} scope 非法：{_cap.scope}"
        assert _cap.group in GROUPS, f"{_cap.name} group 非法：{_cap.group}"
        # title / description 是面板与文档的自描述来源：空着不会报错，
        # 只会在面板上显示成一片空白。这类"静默降级"必须在启动期拦住。
        assert _cap.title.strip(), f"{_cap.name} 缺 title"
        assert _cap.description.strip(), f"{_cap.name} 缺 description"
        for _spec in _cap.params:
            assert _spec.label.strip(), f"{_cap.name}.{_spec.key} 缺中文标签"
del _cap, _spec


_MISSING = object()


# ============================================================
# 参数转换 / 校验
# ============================================================


def _coerce_value(spec: ParamSpec, raw: Any) -> Any:
    """把入参转成 handler 能直接用的类型；不合法抛 `ParamError`（人话原因）。"""
    kind = spec.type

    if kind == "bool":
        if isinstance(raw, bool):
            value: Any = raw
        elif isinstance(raw, (int, float)):
            value = bool(raw)
        elif isinstance(raw, str):
            low = raw.strip().lower()
            if low in ("1", "true", "yes", "y", "on", "是"):
                value = True
            elif low in ("0", "false", "no", "n", "off", "否"):
                value = False
            else:
                raise ParamError(f"{spec.key} 需要布尔值（true/false），收到 {raw!r}")
        else:
            raise ParamError(f"{spec.key} 需要布尔值（true/false），收到 {raw!r}")

    elif kind == "int":
        try:
            value = int(raw) if not isinstance(raw, str) else int(float(raw.strip()))
        except (TypeError, ValueError):
            raise ParamError(f"{spec.key} 需要整数，收到 {raw!r}") from None
        if isinstance(raw, float) and raw != int(raw):
            raise ParamError(f"{spec.key} 需要整数，收到 {raw!r}")

    elif kind == "float":
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ParamError(f"{spec.key} 需要数字，收到 {raw!r}") from None

    elif kind == "json":
        if isinstance(raw, (dict, list)):
            value = raw
        elif isinstance(raw, str):
            text = raw.strip()
            if not text:
                value = None
            else:
                try:
                    value = json.loads(text)
                except json.JSONDecodeError:
                    raise ParamError(
                        f"{spec.key} 需要 JSON 对象，收到无法解析的字符串 {raw!r}"
                    ) from None
        else:
            raise ParamError(f"{spec.key} 需要 JSON 对象，收到 {raw!r}")

    elif kind == "enum":
        value = str(raw).strip()
        allowed = spec.enum or []
        if allowed and value not in allowed:
            # 大小写不敏感兜底：模型常发 "s"、人常在 URL 里写 "PAUSED"。
            # 归一到枚举里的规范写法，比回一句"只能是 S/A/B/C"更有用；
            # 但**只**做大小写归一，不做模糊匹配——"paus" 这种拼一半的不该被猜。
            lowered = {a.lower(): a for a in allowed}
            if value.lower() in lowered:
                value = lowered[value.lower()]
            else:
                raise ParamError(f"{spec.key} 只能是 {'/'.join(allowed)}，收到 {value!r}")

    else:  # str
        value = str(raw)

    # 范围校验只对数值做；bool 是 int 的子类，必须排除掉，
    # 否则 True 会被拿去和 minimum 比较，报出「true 超出 1~50」这种莫名其妙的错。
    if spec.type in ("int", "float") and isinstance(value, (int, float)):
        if spec.minimum is not None and value < spec.minimum:
            raise ParamError(f"{spec.key} 不能小于 {spec.minimum}，收到 {value}")
        if spec.maximum is not None and value > spec.maximum:
            raise ParamError(f"{spec.key} 不能大于 {spec.maximum}，收到 {value}")

    return value


def _coerce_params(cap: Capability, kwargs: dict) -> dict:
    """按 `cap.params` 逐项转换；未声明的参数直接忽略。

    为什么忽略未声明参数而不是报错：模型经常多带一个 `reason` / `note` 之类的
    顺手字段，为此让整次调用失败，用户体验上的损失远大于"少一个字段"。
    真的需要那个字段时，把它加进 `params` —— 这才是正确的修法。
    """
    overrides = dict(kwargs or {})
    out: dict[str, Any] = {}

    for spec in cap.params:
        raw = overrides.get(spec.key, _MISSING)
        blank = raw is _MISSING or raw is None or (
            isinstance(raw, str) and not raw.strip()
        )
        if blank:
            if spec.required:
                raise ParamError(
                    f"缺少必填参数 {spec.key}（{spec.label}）"
                    + (f"：{spec.help}" if spec.help else "")
                )
            out[spec.key] = spec.default
            continue
        out[spec.key] = _coerce_value(spec, raw)

    return out


# ============================================================
# 唯一入口
# ============================================================


def _param_hint(cap: Capability) -> str:
    """参数校验失败时给一句能照做的建议。

    为什么不去解析异常消息（例如从"tier 只能是 S/A/B/C"里抠可选值）：
    那是拿字符串当接口用——改一次文案就悄悄失效，而且漏掉的消息格式不会有任何报错。
    这里直接按 `cap.params` 的声明生成，声明改了提示跟着改。

    为什么非要给 hint：这条出口**模型也走**。只回一句"参数不合法"，
    模型下一轮会拿同一个值再试一次——它不知道合法值是什么。对人同理，
    面板上只显示一行红字、不告诉你能填什么，等于让人去翻文档。
    """
    if not cap.params:
        return f"{cap.name} 不接受参数，去掉请求体里的字段再试。"

    parts: list[str] = []
    for spec in cap.params:
        if spec.enum:
            opt = "/".join(str(x) for x in spec.enum)
            parts.append(f"{spec.key}={opt}")
        elif spec.required:
            parts.append(f"{spec.key}（必填，{spec.label}）")
        else:
            parts.append(f"{spec.key}（选填，{spec.type}）")

    return f"{cap.name} 接受的参数：" + "；".join(parts)


def call(name: str, *, ctx: CapContext, **kwargs: Any) -> dict:
    """执行一个能力。

    - 路由级错误（不存在 / 无权限）抛 `CapabilityError`，让 HTTP 层映射成 404 / 403；
    - 其余一切（参数不合法、下游报错、业务拒绝）都返回 `{"ok": false, ...}`。

    handler 抛出的任何异常都在这里被收成 `ok:false`：能力层的契约是
    **永不把堆栈抛给上层**。堆栈丢给模型，模型就会开始猜；丢给面板，用户只会看到 500。
    """
    cap = _REGISTRY.get(name)
    if cap is None:
        raise CapabilityError(
            f"未知能力：{name}",
            code=CODE_NOT_FOUND,
            payload={"available": sorted(_REGISTRY)},
        )

    if cap.scope == SCOPE_AGENT_ONLY and ctx.source != SOURCE_AGENT:
        raise CapabilityError(
            f"{name} 是对话专用能力，面板不可调用",
            code=CODE_FORBIDDEN,
            payload={"capability": name, "scope": cap.scope},
        )

    try:
        params = _coerce_params(cap, kwargs)
    except ParamError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_code": "invalid_params",
            # 机器可读的错误类别：让 `/api/panel/*` 之类的外层不必去猜 error 字符串
            # 该配哪句排查建议（"参数写错了"和"数据库挂了"不该给同一个建议）。
            "error_kind": "param",
            "hint": _param_hint(cap),
            "capability": name,
        }

    try:
        result = cap.handler(ctx, **params)
    except Exception as exc:  # noqa: BLE001 —— 契约要求：不向上抛
        logger.exception("能力 %s 执行失败", name)
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "error_code": "handler_failed",
            "capability": name,
        }

    if result is None:
        return {"ok": True, "capability": name}
    if not isinstance(result, dict):
        return {"ok": True, "capability": name, "result": result}
    result.setdefault("ok", True)
    return result


def safe_call(name: str, *, ctx: CapContext, **kwargs: Any) -> dict:
    """`call` 的不抛版：路由级错误也压成 `ok:false`。

    Agent 工具路径用它——模型手里的返回值必须是 dict，不能是异常。
    """
    try:
        return call(name, ctx=ctx, **kwargs)
    except CapabilityError as exc:
        return {"ok": False, "error": str(exc), **(exc.payload or {})}
    except Exception as exc:  # noqa: BLE001
        logger.exception("能力 %s 调用失败", name)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ============================================================
# 自描述
# ============================================================


def get(name: str) -> Capability:
    """取能力定义；不存在抛 `CapabilityError(not_found)`。"""
    cap = _REGISTRY.get(name)
    if cap is None:
        raise CapabilityError(
            f"未知能力：{name}",
            code=CODE_NOT_FOUND,
            payload={"available": sorted(_REGISTRY)},
        )
    return cap


def list_capabilities(
    *,
    scope: Optional[str] = None,
    group: Optional[str] = None,
) -> list[Capability]:
    """按 group / name 排序返回能力定义（面板与文档都靠它自描述）。"""
    caps = list(_REGISTRY.values())
    if scope:
        caps = [c for c in caps if c.scope == scope]
    if group:
        caps = [c for c in caps if c.group == group]
    return sorted(caps, key=lambda c: (c.group, c.name))


def to_schema(name: str) -> dict:
    """单个能力的自描述结构（不含 handler）。"""
    return get(name).to_schema()


def tool_names() -> list[str]:
    """全部能力名（用于自检与文档；**不等于** Agent 可见的工具名）。"""
    return sorted(_REGISTRY)


def counts() -> dict:
    """数量汇总，给验收脚本和文档用。"""
    by_group: dict[str, int] = {}
    by_scope: dict[str, int] = {}
    for cap in _REGISTRY.values():
        by_group[cap.group] = by_group.get(cap.group, 0) + 1
        by_scope[cap.scope] = by_scope.get(cap.scope, 0) + 1
    return {"total": len(_REGISTRY), "by_group": by_group, "by_scope": by_scope}
