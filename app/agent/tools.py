"""Agent 工具层。

这里定义模型能调用的全部能力。原则：

1. **工具只做一件事**，名称即语义，模型不会误用。
2. **返回值永远是 JSON 友好的 dict**，模型能直接读，不会因为格式错误乱编。
3. **失败也返回结构化结果**（ok=false + error），不要抛异常给模型——
   异常会让模型进入「猜」的模式，比直接说「不行」更糟。
4. **每个工具的 description 都写清楚「什么时候用」**，
   这是决定模型会不会正确调用的关键，比参数描述重要得多。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import desc, select

from app.config import settings
from app.db import get_session
from app.domain import baseline as baseline_mod
from app.domain.decision import (
    build_out_of_stock_decision,
    judge_clearance,
    judge_price_follow,
)
from app.models import Competitor, SeasonCalendar, Signal, Snapshot
from app.services import briefer, collector, pool

logger = logging.getLogger(__name__)


# ============================================================
# 函数定义（DeepSeek / OpenAI function calling 格式）
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
                        "enum": ["S", "A", "B", "C"],
                        "description": "只看某一层，不传则看全部",
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
                        "description": "若未通过四同法则但用户坚持要加，传 true，会标记为观察级",
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
                "用户说『我售价 29.99』『我库存还有 800 件』时用。"
                "只传要改的字段，其他不变。"
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
                "判断对手降到某个价位时，我方该不该跟进。会跑五道关卡：保本线、清仓、库存、盈亏平衡单量、战场玩家数。"
                "用户问『他降到 19.99 我要不要跟』时用。"
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


# ============================================================
# 实现
# ============================================================


def tool_get_daily_brief(args: dict) -> dict:
    hours = int(args.get("hours") or 24)
    brief = briefer.build_daily_brief(settings.tenant_id, hours=hours)
    data = brief.to_dict()
    data["text"] = brief.to_text()
    return {"ok": True, **data}


def tool_list_competitors(args: dict) -> dict:
    items = pool.list_competitors(settings.tenant_id, tier=args.get("tier"))
    counts: dict[str, int] = {}
    for it in items:
        counts[it["tier"]] = counts.get(it["tier"], 0) + 1
    return {"ok": True, "count": len(items), "by_tier": counts, "items": items}


def tool_get_competitor_detail(args: dict) -> dict:
    return pool.get_competitor_detail(
        settings.tenant_id, args.get("asin", ""), history=int(args.get("history") or 14)
    )


def tool_add_competitor(args: dict) -> dict:
    return pool.add_competitor(
        settings.tenant_id,
        args.get("asin", ""),
        force=bool(args.get("force")),
    )


def tool_archive_competitor(args: dict) -> dict:
    return pool.archive_competitor(
        settings.tenant_id, args.get("asin", ""), args.get("reason", "")
    )


def tool_check_anchor(args: dict) -> dict:
    with get_session() as s:
        from app.models import MyProduct

        my = s.execute(
            select(MyProduct).where(MyProduct.tenant_id == settings.tenant_id).limit(1)
        ).scalar_one_or_none()
        if my is None:
            return {
                "ok": True,
                "exists": False,
                "message": "还没设置自家锚点。至少先告诉我三个数：售价、保本价、日均销量。",
                "text": "还没设置自家锚点。最先要补的三样：售价 / 保本价 / 日均销量。",
            }
        a = baseline_mod.assess(my)
        data = a.to_dict()
        data["exists"] = True
        data["text"] = _render_anchor_text(a)
        return {"ok": True, **data}


def _render_anchor_text(a) -> str:
    lines = [f"锚点就绪度 {a.readiness}%（{a.grade} 级）"]
    for d in a.dimensions:
        mark = "✅" if d.complete else ("⚠️" if d.filled else "❌")
        lines.append(
            f"{mark} {d.name} {d.filled}/{d.total}"
            + ("" if d.complete else f" — 缺：{'、'.join(d.missing)}")
        )
    if a.warnings:
        lines.append("")
        lines += [f"· {w}" for w in a.warnings]
    caps = {
        "分层判定": a.can_tier,
        "跟价判断": a.can_judge_price_follow,
        "进攻窗口": a.can_judge_attack_window,
        "抄 listing 检测": a.can_detect_listing_copy,
    }
    lines.append("")
    lines.append(
        "当前能做：" + "、".join(k for k, v in caps.items() if v) or "当前什么都做不了"
    )
    missing_caps = [k for k, v in caps.items() if not v]
    if missing_caps:
        lines.append("暂时做不了：" + "、".join(missing_caps))
    return "\n".join(lines)


def tool_set_baseline(args: dict) -> dict:
    fields = {k: v for k, v in args.items() if v is not None}
    res = pool.update_baseline(settings.tenant_id, **fields)
    res["ok"] = True
    return res


def tool_judge_price_follow(args: dict) -> dict:
    with get_session() as s:
        from app.models import MyProduct

        my = s.execute(
            select(MyProduct).where(MyProduct.tenant_id == settings.tenant_id).limit(1)
        ).scalar_one_or_none()
        if my is None or not my.current_price:
            return {"ok": False, "error": "还没有设置自家售价，无法判断"}
        if not my.break_even_price and not my.target_margin_rate:
            return {
                "ok": False,
                "error": "缺保本价，无法判断。要么直接告诉我保本价，要么告诉我目标毛利率我帮你算",
            }

        be_price = my.break_even_price
        if not be_price and my.target_margin_rate:
            be_price = baseline_mod.estimate_break_even_price(
                price=my.current_price, margin_rate=my.target_margin_rate
            )

        new_price = args.get("competitor_new_price")
        if new_price is None:
            # 退化为用最近一次采集到的最低净价
            latest = s.execute(
                select(Snapshot)
                .join(Competitor, Snapshot.competitor_id == Competitor.id)
                .where(Competitor.tenant_id == settings.tenant_id)
                .order_by(desc(Snapshot.captured_at))
                .limit(1)
            ).scalar_one_or_none()
            new_price = (latest.net_price or latest.price) if latest else None
        if not new_price:
            return {"ok": False, "error": "没有可用的对手价格"}

        assessment = baseline_mod.assess(my)

        res = judge_price_follow(
            competitor_new_price=float(new_price),
            my_current_price=my.current_price,
            my_break_even_price=float(be_price),
            my_unit_profit_now=my.current_price * (my.target_margin_rate or 0.2),
            my_daily_units=my.daily_sales_avg or 0,
            competitor_is_clearance=bool(args.get("competitor_is_clearance")),
            market_players=int(args.get("market_players") or 3),
            my_dos=assessment.days_of_supply,
        )
        data = res.to_dict()
        data["ok"] = True
        data["inputs"] = {
            "competitor_new_price": new_price,
            "my_current_price": my.current_price,
            "my_break_even_price": be_price,
            "my_dos": assessment.days_of_supply,
        }
        return data


def tool_judge_clearance(args: dict) -> dict:
    asin = (args.get("asin") or "").strip().upper()

    with get_session() as s:
        comp = s.execute(
            select(Competitor).where(
                Competitor.tenant_id == settings.tenant_id, Competitor.asin == asin
            )
        ).scalar_one_or_none()
        if comp is None:
            return {"ok": False, "error": f"{asin} 不在池中"}

        snaps = list(
            s.execute(
                select(Snapshot)
                .where(Snapshot.competitor_id == comp.id)
                .order_by(desc(Snapshot.captured_at))
                .limit(20)
            ).scalars()
        )

    if len(snaps) < 2:
        return {
            "ok": False,
            "error": "历史数据不足两条，无法判断趋势。建议先观察几天。",
        }

    prices = [x.net_price or x.price for x in snaps if (x.net_price or x.price)]
    price_drop_pct = None
    if len(prices) >= 2 and prices[-1]:
        peak = max(prices)
        price_drop_pct = round((1 - prices[0] / peak) * 100, 1) if peak else None

    # 库存单调下降的判断：用 stock_hint / in_stock 序列
    hints = [(x.stock_hint or "").upper() for x in snaps if x.stock_hint]
    stock_declining = None
    if len(hints) >= 3:
        order = {"IN_STOCK": 2, "LOW_STOCK": 1, "OOS": 0}
        vals = [order.get(h) for h in hints if h in order]
        if len(vals) >= 3:
            stock_declining = all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1)) and vals[0] > vals[-1]

    # Listing 是否停维护（用标题与主图是否长期不变粗略判断）
    titles = {x.title for x in snaps if x.title}
    listing_frozen = len(titles) == 1 and len(snaps) >= 5

    res = judge_clearance(
        price_drop_pct=price_drop_pct,
        stock_declining=stock_declining,
        no_restock=None,                       # 补货行为从公开数据看不出来
        ads_stopped=args.get("ads_stopped"),
        listing_frozen=listing_frozen,
        brand_shrinking=args.get("brand_shrinking"),
        historical_seasonal=None,
        deep_coupon=None,
        variants_delisted=None,
    )

    data = res.to_dict()
    data["ok"] = True
    data["auto_extracted"] = {
        "price_drop_pct": price_drop_pct,
        "stock_declining": stock_declining,
        "listing_frozen": listing_frozen,
        "snapshots_used": len(snaps),
    }
    data["unknown_signals"] = [
        "是否停止补货（公开数据看不到）",
        "广告位是否消失（需人工观察或关键词广告位数据）",
        "品牌其他 ASIN 是否同步收缩",
        "变体是否逐步下架",
    ]
    data["advice"] = (
        "清仓判断里最硬的一条是「库存只降不补」——这个从公开数据拿不到全貌，"
        "但连续几次采集看到 LOW_STOCK 一直不回到 IN_STOCK，基本就能定性。"
    )
    return data


def tool_collect_now(args: dict) -> dict:
    asin = (args.get("asin") or "").strip().upper()

    if asin:
        with get_session() as s:
            comp = s.execute(
                select(Competitor).where(
                    Competitor.tenant_id == settings.tenant_id, Competitor.asin == asin
                )
            ).scalar_one_or_none()
            if comp is None:
                return {"ok": False, "error": f"{asin} 不在池中"}
            cid = comp.id
        res = collector.collect_competitor(cid)
        return {
            "ok": res.ok,
            "asin": res.asin,
            "data_quality": res.data_quality,
            "signal_count": len(res.signals),
            "signals": [s.to_row() for s in res.signals],
            "errors": res.errors,
        }

    # 同步上下文里跑异步采集
    import asyncio

    total = asyncio.run(collector.collect_all(settings.tenant_id))
    return {
        "ok": True,
        "competitors": len(total),
        "success": sum(1 for r in total if r.ok),
        "signals": sum(len(r.signals) for r in total),
        "p0": sum(r.p0_count for r in total),
        "details": [
            {
                "asin": r.asin,
                "ok": r.ok,
                "quality": r.data_quality,
                "signals": len(r.signals),
                "errors": r.errors[:3],
            }
            for r in total
        ],
    }


def tool_set_season_phase(args: dict) -> dict:
    with get_session() as s:
        row = SeasonCalendar(
            tenant_id=settings.tenant_id,
            category=args.get("category", "default"),
            phase=args.get("phase", "blast"),
            start_md=args.get("start_md", "11-01"),
            end_md=args.get("end_md", "12-31"),
            interval="15m" if args.get("phase") == "blast" else "1h",
        )
        s.add(row)
        s.flush()
        return {
            "ok": True,
            "category": row.category,
            "phase": row.phase,
            "window": f"{row.start_md} ~ {row.end_md}",
            "effect": (
                "旺季期间采集间隔缩短到 15 分钟，信号阈值收紧 20%"
                if row.phase == "blast"
                else "该时段按常规频率采集"
            ),
        }


def tool_get_my_status(args: dict) -> dict:
    from app.models import MyProduct

    with get_session() as s:
        my = s.execute(
            select(MyProduct).where(MyProduct.tenant_id == settings.tenant_id).limit(1)
        ).scalar_one_or_none()
        if my is None:
            return {"ok": True, "anchor_exists": False, "message": "尚未设置自家锚点"}

        a = baseline_mod.assess(my)
        comps = list(
            s.execute(
                select(Competitor).where(
                    Competitor.tenant_id == settings.tenant_id,
                    Competitor.status == "active",
                )
            ).scalars()
        )
        pool_counts: dict[str, int] = {}
        for c in comps:
            pool_counts[c.tier] = pool_counts.get(c.tier, 0) + 1

        from datetime import timedelta

        since = datetime.now() - timedelta(hours=24)
        recent = s.execute(
            select(Signal).where(
                Signal.tenant_id == settings.tenant_id, Signal.detected_at >= since
            )
        ).scalars()
        sig_list = list(recent)

        return {
            "ok": True,
            "anchor_exists": True,
            "anchor": a.to_dict(),
            "portfolio": pool_counts,
            "last_24h": {
                "p0": sum(1 for x in sig_list if x.level == "P0"),
                "p1": sum(1 for x in sig_list if x.level == "P1"),
                "p2": sum(1 for x in sig_list if x.level == "P2"),
            },
        }


# ============================================================
# 注册表
# ============================================================

_REGISTRY: dict[str, Callable[[dict], dict]] = {
    "get_daily_brief": tool_get_daily_brief,
    "list_competitors": tool_list_competitors,
    "get_competitor_detail": tool_get_competitor_detail,
    "add_competitor": tool_add_competitor,
    "archive_competitor": tool_archive_competitor,
    "check_anchor": tool_check_anchor,
    "set_baseline": tool_set_baseline,
    "judge_price_follow": tool_judge_price_follow,
    "judge_clearance": tool_judge_clearance,
    "collect_now": tool_collect_now,
    "set_season_phase": tool_set_season_phase,
    "get_my_status": tool_get_my_status,
}


def execute_tool(name: str, args: dict | None = None) -> dict:
    """统一入口。任何异常都被收成 ok=false，绝不把堆栈丢给模型。"""
    fn = _REGISTRY.get(name)
    if fn is None:
        return {"ok": False, "error": f"未知工具：{name}", "available": list(_REGISTRY)}

    try:
        result = fn(args or {})
        if not isinstance(result, dict):
            return {"ok": True, "result": result}
        return result
    except Exception as exc:  # noqa: BLE001
        logger.exception("工具 %s 执行失败", name)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def tool_names() -> list[str]:
    return list(_REGISTRY)
