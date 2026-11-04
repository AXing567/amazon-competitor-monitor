"""Agent 工具层 —— **薄适配层**，实现在 `app/capabilities`。

## 这一层为什么现在这么薄

以前这里有 12 个 `tool_xxx` 函数，每个都直接开数据库会话、拼业务参数、算结论。
面板要同样的数据时只能另写一份，于是同一个问题（"有几个 S 级"）有两个答案。

现在这里只剩三件事：

1. `TOOL_SCHEMAS` —— 给模型看的函数定义（**这是 Agent 独有的东西**，
   面板不需要它，所以它留在这里而不下沉）；
2. `execute_tool` —— 把 `(name, args)` 转成 `capabilities.call(name, ctx=..., **args)`；
3. `tool_names()` —— 模型可见的工具名，**必须与 `TOOL_SCHEMAS` 完全一致**。

业务逻辑一行都不在这里了。想改行为，去 `app/capabilities/` 改一处，两个入口同时生效。

## `ctx.source` 为什么是 `"agent"`

它决定了「这次采集算谁触发的」（`collect_runs.trigger`）和「这条动作的来源」
（`actions.source`）。如果这里偷懒传 `"panel"`，健康页上"模型自己决定去采集"
和"人在面板上点的采集"就分不开了，而这两者的失败率差得很远。

## 身份（`user_id`）为什么是 `None`

Agent 这一侧目前没有把飞书 `open_id` 一路传到工具层（`llm.achat` 只带 `session_id`）。
所以这里不编造用户，显式传 `None` —— 下游会回落默认用户，并且这处回落是
**有据可查的行为**，而不是"某个模块级单例里恰好存着上次请求的用户"。
把 `open_id` 传下来是下一轮的事（`app/capabilities` 的 `CapContext` 已经准备好了）。
"""
from __future__ import annotations

import logging
from typing import Any

from app.capabilities import CapContext, safe_call
from app.capabilities.types import SOURCE_AGENT
from app.config import settings

logger = logging.getLogger(__name__)


# ============================================================
# 函数定义（DeepSeek / OpenAI function calling 格式）
#
# 这一份是 Agent 独占的：模型靠它决定"什么时候调哪个"，
# 所以 description 写的是使用场景而不是参数说明——参数说明在 properties 里。
# ============================================================

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_daily_brief",
            "description": (
                "获取竞品日报：过去 N 小时所有 P0/P1 级变化，每条都带建议动作。"
                "用户问『今天有什么情况』『有什么要处理的』『汇报一下』时用这个。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "回看多少小时，默认 24。问『这周』就传 168。",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_competitors",
            "description": (
                "列出当前监控的所有竞品及其最新状态（价格、排名、星级、库存、分层）。"
                "用户问『我在盯谁』『池子里有什么』『有几个 S 级』时用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tier": {
                        "type": "string",
                        # unknown 必须在这里：止血补丁之后「证据不足、暂未判定」的竞品
                        # 就是 unknown 级。枚举里没有它，模型就无法回答「哪些还没判定」，
                        # 只能猜成 C 级——那正是我们要修的那个 bug。
                        "enum": ["S", "A", "B", "C", "unknown"],
                        "description": "只看某一层，不传则看全部；unknown 表示证据不足、暂未判定",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_competitor_detail",
            "description": (
                "查单个竞品的完整档案：当前状态、历史价格与排名趋势、最近所有信号、历史最低价。"
                "用户提到具体某个 ASIN，或问『这个对手最近怎么样』时用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "asin": {"type": "string", "description": "竞品 ASIN"},
                    "history": {
                        "type": "integer",
                        "description": "回溯多少条快照，默认 14",
                    },
                },
                "required": ["asin"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_competitor",
            "description": (
                "把一个新的 ASIN 加进监控池。系统会自动拉数据、跑四同法则（价格带/流量词重合/节点）、"
                "判分层，并建立基线快照。用户说『盯一下这个 ASIN』『把 XXX 加进来』时用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "asin": {"type": "string", "description": "要加的竞品 ASIN"},
                    "force": {
                        "type": "boolean",
                        "description": "若未通过四同法则但用户坚持要加，传 true",
                    },
                },
                "required": ["asin"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "archive_competitor",
            "description": "把竞品移出监控池（保留历史）。用户说『不用盯 XXX 了』『删掉这个』时用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "asin": {"type": "string"},
                    "reason": {"type": "string", "description": "归档原因，便于日后回看"},
                },
                "required": ["asin"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_anchor",
            "description": (
                "体检自家锚点：五大维度缺了什么、缺了会导致哪个判断失灵、现在能做哪些判断。"
                "用户问『我配置齐了吗』『为什么判断不了』时用。也是新用户第一次对话的推荐动作。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_baseline",
            "description": (
                "设置或更新自家产品的锚点数据（售价、保本价、日均销量、库存、补货周期等）。"
                "用户说『我售价 29.99』『我库存还有 800 件』时用。只传要改的字段，其他不变。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "asin": {"type": "string", "description": "自家 ASIN，首次设置时必填"},
                    "current_price": {"type": "number", "description": "当前售价（美元）"},
                    "break_even_price": {"type": "number", "description": "保本价，跌破就不该跟"},
                    "target_margin_rate": {
                        "type": "number",
                        "description": "目标毛利率，0~1 之间。填了它可自动推算保本价",
                    },
                    "daily_sales_avg": {"type": "number", "description": "日均销量（单）"},
                    "fba_available_units": {"type": "integer", "description": "FBA 可售库存"},
                    "lead_time_days": {"type": "integer", "description": "补货周期（天）"},
                    "sub_category_rank": {"type": "integer", "description": "子类目 BSR"},
                    "rating": {"type": "number", "description": "星级"},
                    "review_count": {"type": "integer", "description": "评论数"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "judge_price_follow",
            "description": (
                "判断对手降到某个价位时，我方该不该跟进。会跑五道关卡：保本线、清仓、库存、"
                "盈亏平衡单量、战场玩家数。用户问『他降到 19.99 我要不要跟』时用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "competitor_new_price": {
                        "type": "number",
                        "description": "对手的新价格。不传则用他当前净价",
                    },
                    "competitor_is_clearance": {
                        "type": "boolean",
                        "description": "是否已判定为清仓，默认 false",
                    },
                    "market_players": {
                        "type": "integer",
                        "description": "这个战场大概有几个玩家在抢，默认 3",
                    },
                },
                "required": ["competitor_new_price"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "judge_clearance",
            "description": (
                "判断某个竞品是不是在清仓。会从历史快照里自动提取能拿到的信号（降价幅度、库存趋势），"
                "拿不到的信号会明确标注为「未知」，不会瞎猜。"
                "用户问『他是不是要跑了』『这波降价是不是清仓』时用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "asin": {"type": "string", "description": "竞品 ASIN"},
                    "ads_stopped": {
                        "type": "boolean",
                        "description": "你是否观察到他的核心词广告位消失（拿不到就留空）",
                    },
                    "brand_shrinking": {
                        "type": "boolean",
                        "description": "同品牌其他 ASIN 是否也在收缩",
                    },
                },
                "required": ["asin"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "collect_now",
            "description": (
                "立刻采集一次最新数据，不等定时任务。用户说『现在去查一下』『马上看看他改价没』时用。"
                "不传 asin 则采集全部竞品，会比较慢。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "asin": {"type": "string", "description": "只采这个 ASIN，不传则采全部"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_season_phase",
            "description": (
                "设置季节日历。旺季会自动提高采集频率并收紧信号阈值，淡季则相反。"
                "用户提到『10 到 12 月旺季要盯紧点』时用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "类目名，如 toys"},
                    "phase": {
                        "type": "string",
                        "enum": ["warmup", "blast", "cooldown"],
                        "description": "储备期 / 爆发期 / 退潮期",
                    },
                    "start_md": {"type": "string", "description": "开始日期 MM-DD，如 11-20"},
                    "end_md": {"type": "string", "description": "结束日期 MM-DD，如 12-20"},
                },
                "required": ["category", "phase", "start_md", "end_md"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_my_status",
            "description": (
                "查自家的当前状态汇总：锚点就绪度、可售天数、库存状态、池子规模、最近信号量。"
                "用户问『我现在什么情况』时用。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


#: 模型可见的工具名。**与 `TOOL_SCHEMAS` 同源**，
#: 所以两边永远不会漂移（验收标准 #2 就是这条等式）。
_TOOL_NAMES: tuple[str, ...] = tuple(s["function"]["name"] for s in TOOL_SCHEMAS)


# ============================================================
# 唯一出口
# ============================================================


def _ctx() -> CapContext:
    """Agent 侧的调用上下文。

    `user_id=None` 是**显式**的"我不知道是谁"，不是遗漏：
    `llm.achat` 目前只带 `session_id`，把 open_id 传下来是下一轮的事。
    编一个 user_id 出来只会让动作记录指向错误的人。
    """
    return CapContext(tenant_id=settings.tenant_id, user_id=None, source=SOURCE_AGENT)


def execute_tool(name: str, args: dict | None = None) -> dict:
    """统一入口。任何异常都被收成 `ok=false`，绝不把堆栈丢给模型。

    签名与返回值契约（`{"ok": bool, ...}`）**保持不变**——`llm.py` 与既有的
    飞书回调链都依赖它，改动签名会让"工具调用"这条主链路静默失效。
    """
    payload: dict[str, Any] = args if isinstance(args, dict) else {}
    if not isinstance(args, dict) and args is not None:
        logger.warning("工具 %s 的入参不是 dict（%r），已按空参数处理", name, type(args))

    result = safe_call(name, ctx=_ctx(), **payload)
    if not isinstance(result, dict):
        return {"ok": True, "result": result}
    return result


def tool_names() -> list[str]:
    """模型可见的工具名（= `TOOL_SCHEMAS` 里的名字，不含能力层的另外 21 个）。

    为什么不返回全部 33 个能力：模型看不见的能力它就不会调，这 12 个才是它的全部世界。
    多返回一些"技术上存在"的名字，只会让「schema 与注册表一致」这条自检失去意义。
    """
    return list(_TOOL_NAMES)
