"""L4 · 查询类能力（14 个）。

这一层的职责只有四件事：**参数校验（在 `__init__.call` 里统一做）→ 组装上下文
→ 调下游服务 → 包装返回**。任何「该不该判 S 级」「该不该报警」的判断都不写在这里，
否则面板和 Agent 两条路早晚会各判一次、各判得不一样。

## 关于 `tier`：一律走 effective_tier 语义

`Competitor.tier` 这一列有两种特殊值：

- 老数据可能是 `NULL`（加列之前入池的竞品）；
- 止血补丁之后可能是字符串 `"unknown"`——意思是**证据不足，暂不判定**，
  而不是「最差的一级」。

如果这里直接把 `c.tier` 透给面板，`NULL` 会被前端渲染成空白、
排序时又会被当成「比 C 还低」扫到列表末尾，于是「待判定」的竞品
看起来像「已经死掉的竞品」。所以本模块所有读 `tier` 的地方统一过 `_eff_tier()`，
并且单独给一个 `tier_label`（`unknown` → 「待判定」），让面板不需要知道这些约定。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import desc, select

from app.capabilities.types import (
    CapContext,
    Capability,
    GROUP_QUERY,
    ParamSpec,
    SCOPE_SHARED,
    T_BOOL,
    T_ENUM,
    T_INT,
)
from app.db import get_session
from app.domain import baseline as baseline_mod
from app.domain.thresholds import GROUPS, GROUP_TITLES, spec_groups
from app.models import Action, Competitor, MyProduct, Signal, Snapshot, User
from app.services import (
    action_log,
    binding,
    briefer,
    health,
    identity,
    pool,
    settings_store,
)

logger = logging.getLogger(__name__)

#: 合法分层（与 `judge_tier` 的输出一致）
TIER_LETTERS: tuple[str, ...] = ("S", "A", "B", "C")
TIER_UNKNOWN = "unknown"

TIER_LABELS: dict[str, str] = {
    "S": "S 级 · 主战场",
    "A": "A 级 · 强相关",
    "B": "B 级 · 弱相关",
    "C": "C 级 · 观察",
    TIER_UNKNOWN: "待判定（证据不足）",
}

STATUS_VALUES: tuple[str, ...] = ("active", "paused", "archived")


def effective_tier(raw: Any) -> str:
    """把库里存的 tier 归一成对外语义。

    `NULL` / 空串 / 历史脏值一律当 `unknown`（待判定），**绝不当 C 级**：
    把「没判」显示成「最差」会让老板把还在观察期的竞品直接归档掉。
    """
    text = str(raw or "").strip()
    if not text:
        return TIER_UNKNOWN
    upper = text.upper()
    if upper in TIER_LETTERS:
        return upper
    return TIER_UNKNOWN


def _tier_label(tier: str, tier_reasons: Optional[dict] = None) -> str:
    """面板显示的层级标签。

    `tier_reasons` 是**可选**的（默认 `None`）：老调用点只传 `tier`，
    行为与以前完全一致。传入判定依据时，暂定档会带上「（暂定：缺XX）」。

    为什么要看 `tier_reasons`：词重合没测出来时，`judge_tier` 给出的仍是一个
    字母级，只在 `tier_reasons` 里标了 `provisional`。标签若照旧写「B 级 · 弱相关」，
    用户读到的是「重合度中等」——一句我们没有任何证据支持的话。
    """
    base = TIER_LABELS.get(tier, tier)
    if tier == TIER_UNKNOWN:
        # `unknown` 自有「待判定（证据不足）」这句话，不许再叠一层「暂定」：
        # 「暂定」的前提是有字母级，`unknown` 连字母级都还没有。
        return base

    reasons = dict(tier_reasons or {})
    if not reasons.get("provisional"):
        return base

    dims = [d for d in _DIM_ORDER if d in (reasons.get("missing_dims") or [])]
    short = [_DIM_SHORT_LABELS.get(d, d) for d in dims] or ["关键维度"]
    return f"{base}（暂定：缺{'、'.join(short)}）"


#: 三个维度的固定顺序，与 `domain/tiering.py` 的 `DIM_ORDER` 一致。
#: 本模块不 import 它，是因为 L4 只想依赖判据的**结果**，不想顺手把判据的实现
#: 也拖进来（那会让「这里只做映射」这条边界失守）。顺序不一致会让面板的
#: 「缺什么」在两次刷新间换顺序，看起来像数据在抖。
_DIM_ORDER: tuple[str, ...] = ("price_ratio", "keyword_overlap", "node_match")

#: 标签里用的维度短名（一行内要说清缺什么，所以只留最短的辨识词）。
_DIM_SHORT_LABELS: dict[str, str] = {
    "price_ratio": "价格带",
    "keyword_overlap": "词重合",
    "node_match": "类目节点",
}

#: `tier_missing` 那一栏用的维度说明。`unknown` 与「暂定」**共用这一套措辞**：
#: 同一个缺失在两处被写成两种说法，用户会以为是两件不同的事。
_MISSING_DIM_TEXT: dict[str, str] = {
    "price_ratio": "缺对手净价",
    "keyword_overlap": "缺流量词（快照里没有 keywords）",
    # 措辞要同时覆盖两种原因：竞品快照里没有、或自家锚点没配。
    # 之前这里写死了"自家产品未落 category_path"，于是锚点明明配好了、
    # 只是竞品没采到，面板也会指着用户说"你没配"——指向错误的方向。
    "node_match": "缺类目路径（竞品快照或自家锚点缺 category_path）",
}


def _missing_dims(item: dict) -> list[str]:
    """`unknown` 级到底缺哪几维证据。

    这是**映射**不是判断：判据在 `domain/tiering.py`（`decidable_dims < 2` → 证据不足），
    这里只把 `None` 的维度翻成中文，好让面板能说清楚"为什么它是待判定"。
    不翻译的后果是面板上只显示「待判定」三个字，用户第一反应是"系统坏了"。

    优先读判定当时写进 `tier_reasons["missing_dims"]` 的那一份快照（暂定档要用它
    才知道「当初缺的是哪一维」）；没有这个键的老数据，退回按行上的三个值推。
    """
    reasons = item.get("tier_reasons") or {}
    persisted = reasons.get("missing_dims")
    if isinstance(persisted, (list, tuple)) and persisted:
        dims = [d for d in _DIM_ORDER if d in persisted]
    else:
        dims = [d for d in _DIM_ORDER if item.get(d) is None]
    return [_MISSING_DIM_TEXT.get(d, d) for d in dims]


def _is_provisional(eff: str, item: dict) -> bool:
    """这一行是不是「暂定档」：有字母级，但定档所依赖的维度里缺了东西。

    判据只有一处（`tier_reasons.provisional`），这里不加新条件——
    在展示层再推一次「算不算暂定」等于留了第二份判据，早晚会和判定层不一致。
    """
    if eff == TIER_UNKNOWN:
        return False
    return bool((item.get("tier_reasons") or {}).get("provisional"))


# ============================================================
# 内部小工具
# ============================================================


def _my_product_row(tenant_id: str):
    """按 tenant 取自家产品 ORM 对象（调用方必须自己持有 session）。

    为什么不用 `pool.get_my_product()`：它返回 dict，够看汇总，但锚点体检要逐维度
    看 `missing` 列表，而 `baseline.assess()` 需要属性访问。这是 L4 → L0 的只读穿透，
    属已知分层债：等 `pool` 拆分后下沉成 `pool.assess_my_product()`（记在交付报告的遗留里）。
    """
    with get_session() as s:
        return s.execute(
            select(MyProduct).where(MyProduct.tenant_id == tenant_id).limit(1)
        ).scalar_one_or_none()


def _render_anchor_text(a) -> str:
    """锚点体检的文字版（发给飞书时用，面板用结构化字段）。"""
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
        "当前能做：" + ("、".join(k for k, v in caps.items() if v) or "当前什么都做不了")
    )
    missing_caps = [k for k, v in caps.items() if not v]
    if missing_caps:
        lines.append("暂时做不了：" + "、".join(missing_caps))
    return "\n".join(lines)


# ============================================================
# ① list_competitors
# ============================================================


def list_competitors(
    ctx: CapContext,
    tier: Optional[str] = None,
    status: Optional[str] = None,
    include_archived: bool = False,
) -> dict:
    """竞品池清单（补 `battlefield`：这个竞品对我哪个自家产品、依据是什么）。

    `include_archived` 是给竞品池管理那一屏的：归档的竞品要能被看见并恢复。
    默认 False——Agent 回答「我在盯什么」时不该把已归档的算进去。
    """
    want_tier = (tier or "").strip().lower() or None
    if want_tier is not None and want_tier not in {t.lower() for t in TIER_LETTERS} | {TIER_UNKNOWN}:
        return {
            "ok": False,
            "error": f"tier 只能是 S/A/B/C/unknown，收到 {tier!r}",
            "hint": "传 unknown 可以看「证据不足、暂未判定」的竞品",
        }
    want_status = (status or "").strip().lower() or None
    if want_status is not None and want_status not in STATUS_VALUES:
        return {
            "ok": False,
            "error": f"status 只能是 {'/'.join(STATUS_VALUES)}，收到 {status!r}",
        }

    raw_items = pool.list_competitors(ctx.tenant_id, include_archived=include_archived)
    mine = pool.get_my_product(ctx.tenant_id)
    bindings = {
        b["competitor_id"]: b
        for b in binding.list_for(ctx.tenant_id, user_id=ctx.user_id)
    }

    items: list[dict] = []
    for row in raw_items:
        eff = effective_tier(row.get("tier"))
        if want_tier is not None and eff.lower() != want_tier:
            continue
        if want_status is not None and (row.get("status") or "").lower() != want_status:
            continue
        b = bindings.get(row.get("id"))
        item = dict(row)
        item["tier"] = eff
        reasons = row.get("tier_reasons") or {}
        provisional = _is_provisional(eff, row)
        item["tier_label"] = _tier_label(eff, reasons)
        item["tier_stored"] = row.get("tier")
        # `tier_provisional`：给面板一个明确的布尔位，别让前端去解析标签里的括号。
        item["tier_provisional"] = provisional
        # 待判定要说清缺什么；**暂定档也要说**——否则面板只剩一个字母级，
        # 用户会把它当成「测过了、轻度重合」。S/A/B/C 的正常结论才不需要这一栏。
        item["tier_missing"] = (
            _missing_dims(row) if (eff == TIER_UNKNOWN or provisional) else []
        )
        item["battlefield"] = {
            "tier": eff,
            "my_asin": (mine or {}).get("asin"),
            "binding_id": b.get("id") if b else None,
            "basis_summary": b.get("basis_summary") if b else None,
            "binding_source": b.get("source") if b else None,
            "confidence": b.get("confidence") if b else None,
        }
        items.append(item)

    by_tier: dict[str, int] = {}
    for it in items:
        by_tier[it["tier"]] = by_tier.get(it["tier"], 0) + 1

    return {
        "ok": True,
        "count": len(items),
        "by_tier": by_tier,
        "undecided": by_tier.get(TIER_UNKNOWN, 0),
        "items": items,
    }


# ============================================================
# ② get_competitor_detail
# ============================================================


def get_competitor_detail(
    ctx: CapContext,
    asin: str,
    history: int = 14,
) -> dict:
    res = pool.get_competitor_detail(ctx.tenant_id, asin, history=history)
    if not res.get("ok"):
        return res

    stored = res.get("tier")
    eff = effective_tier(stored)
    reasons = res.get("tier_reasons") or {}
    res["tier"] = eff
    res["tier_label"] = _tier_label(eff, reasons)
    res["tier_stored"] = stored
    res["tier_provisional"] = _is_provisional(eff, res)
    res["tier_missing"] = (
        _missing_dims(res) if (eff == TIER_UNKNOWN or res["tier_provisional"]) else []
    )

    comp_id = _competitor_id_by_asin(ctx.tenant_id, asin)
    b = binding.resolve_binding(ctx.tenant_id, ctx.user_id, comp_id) if comp_id else None
    mine = pool.get_my_product(ctx.tenant_id)
    res["battlefield"] = {
        "tier": eff,
        "my_asin": (mine or {}).get("asin"),
        "binding_id": b.get("id") if b else None,
        "basis_summary": b.get("basis_summary") if b else None,
        "basis": (b or {}).get("basis") or {},
    }
    return res


def _competitor_id_by_asin(tenant_id: str, asin: str) -> Optional[int]:
    asin = (asin or "").strip().upper()
    if not asin:
        return None
    with get_session() as s:
        row = s.execute(
            select(Competitor.id).where(
                Competitor.tenant_id == tenant_id, Competitor.asin == asin
            )
        ).scalar_one_or_none()
    return int(row) if row is not None else None


# ============================================================
# ③ get_daily_brief
# ============================================================


def get_daily_brief(
    ctx: CapContext,
    hours: int = 24,
    include_text: bool = True,
) -> dict:
    brief = briefer.build_daily_brief(
        ctx.tenant_id, hours=hours, user_id=ctx.user_id
    )
    data = brief.to_dict()
    if include_text:
        data["text"] = brief.to_text()
    return {"ok": True, **data}


# ============================================================
# ④ get_my_status
# ============================================================


def get_my_status(ctx: CapContext) -> dict:
    with get_session() as s:
        my = _my_product_row(ctx.tenant_id)
        if my is None:
            return {
                "ok": True,
                "anchor_exists": False,
                "my_product_id": None,
                "my_asin": None,
                "message": "尚未设置自家锚点",
                "hint": "先告诉我三个数：售价 / 保本价 / 日均销量",
            }

        # 在会话内取出来：后面 `with` 一关，再访问 ORM 对象属性就可能
        # DetachedInstanceError（且只在有数据、又刚好 commit 过时复现）。
        my_id = my.id
        my_asin = my.asin
        a = baseline_mod.assess(my)
        comps = list(
            s.execute(
                select(Competitor).where(
                    Competitor.tenant_id == ctx.tenant_id,
                    Competitor.status == "active",
                )
            ).scalars()
        )
        portfolio: dict[str, int] = {}
        for c in comps:
            eff = effective_tier(c.tier)
            portfolio[eff] = portfolio.get(eff, 0) + 1

        since = datetime.now() - timedelta(hours=24)
        signals = list(
            s.execute(
                select(Signal).where(
                    Signal.tenant_id == ctx.tenant_id, Signal.detected_at >= since
                )
            ).scalars()
        )

    return {
        "ok": True,
        "anchor_exists": True,
        # 面板的产品层阈值要拿它当 scope_id；没有它，"产品覆盖"这一层就没法打开
        "my_product_id": my_id,
        "my_asin": my_asin,
        "anchor": a.to_dict(),
        "portfolio": portfolio,
        "undecided": portfolio.get(TIER_UNKNOWN, 0),
        "last_24h": {
            "p0": sum(1 for x in signals if x.level == "P0"),
            "p1": sum(1 for x in signals if x.level == "P1"),
            "p2": sum(1 for x in signals if x.level == "P2"),
        },
    }


# ============================================================
# ⑤ check_anchor
# ============================================================


def check_anchor(ctx: CapContext) -> dict:
    with get_session() as s:
        my = _my_product_row(ctx.tenant_id)
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


# ============================================================
# ⑥ get_change_history
# ============================================================


def get_change_history(
    ctx: CapContext,
    asin: str,
    days: int = 30,
    limit: int = 200,
) -> dict:
    """单一竞品的三条时间线：快照 / 信号 / 动作。

    「他昨天是不是改过价」这类问题必须能一眼看到**原始快照**，
    只看信号是不够的——信号是 L2 判出来的，判据错了就没有痕迹可查。
    """
    asin = (asin or "").strip().upper()
    if not asin:
        return {"ok": False, "error": "asin 不能为空"}

    since = datetime.now() - timedelta(days=max(1, int(days)))
    page = max(1, int(limit))

    with get_session() as s:
        comp = s.execute(
            select(Competitor).where(
                Competitor.tenant_id == ctx.tenant_id, Competitor.asin == asin
            )
        ).scalar_one_or_none()
        if comp is None:
            return {"ok": False, "error": f"{asin} 不在池中"}

        snaps = list(
            s.execute(
                select(Snapshot)
                .where(Snapshot.competitor_id == comp.id, Snapshot.captured_at >= since)
                .order_by(desc(Snapshot.captured_at))
                .limit(page)
            ).scalars()
        )
        sigs = list(
            s.execute(
                select(Signal)
                .where(Signal.competitor_id == comp.id, Signal.detected_at >= since)
                .order_by(desc(Signal.detected_at))
                .limit(page)
            ).scalars()
        )
        acts = list(
            s.execute(
                select(Action)
                .where(Action.tenant_id == ctx.tenant_id, Action.competitor_id == comp.id)
                .where(Action.executed_at >= since)
                .order_by(desc(Action.executed_at))
                .limit(page)
            ).scalars()
        )

        users: dict[int, str] = {}
        for a in acts:
            if a.user_id is not None and a.user_id not in users:
                u = s.get(User, a.user_id)
                users[a.user_id] = u.name if u else None

        return {
            "ok": True,
            "asin": comp.asin,
            "tier": effective_tier(comp.tier),
            "tier_label": _tier_label(effective_tier(comp.tier), comp.tier_reasons),
            "status": comp.status,
            "days": days,
            "snapshots": [
                {
                    "at": x.captured_at.isoformat() if x.captured_at else None,
                    "price": x.price,
                    "net_price": x.net_price,
                    "coupon": x.coupon_value,
                    "rank": x.rank_sub or x.rank_main,
                    "rating": x.rating,
                    "reviews": x.review_count,
                    "in_stock": x.in_stock,
                    "data_quality": x.data_quality,
                }
                for x in snaps
            ],
            "signals": [
                {
                    "id": x.id,
                    "at": x.detected_at.isoformat() if x.detected_at else None,
                    "level": x.level,
                    "category": x.category,
                    "signal_type": x.signal_type,
                    "description": x.description,
                    # 列名是 before_value / after_value，不是 payload：
                    # 别照着"信号都带一个 payload"的直觉写，这里没有那种列。
                    "before": x.before_value,
                    "after": x.after_value,
                    "change_pct": x.change_pct,
                    "pushed_at": x.pushed_at.isoformat() if x.pushed_at else None,
                }
                for x in sigs
            ],
            "actions": [
                {
                    "id": a.id,
                    "at": a.executed_at.isoformat() if a.executed_at else None,
                    "user": users.get(a.user_id) if a.user_id else None,
                    "user_id": a.user_id,
                    "action_type": a.action_type,
                    "description": a.description,
                    "reason": a.reason,
                    "source": a.source,
                    "verdict": a.verdict,
                    "verify_due_at": a.verify_due_at.isoformat() if a.verify_due_at else None,
                    "signal_id": a.signal_id,
                }
                for a in acts
            ],
        }


# ============================================================
# ⑦ get_data_health
# ============================================================


def get_data_health(ctx: CapContext, days: int = 7) -> dict:
    return health.collect_health(ctx.tenant_id, days=days)


# ============================================================
# ⑧ list_actions
# ============================================================


def list_actions(
    ctx: CapContext,
    days: int = 30,
    asin: Optional[str] = None,
    user_id: Optional[int] = None,
    limit: int = 200,
) -> dict:
    """动作记录。

    `user_id` **显式传**（不自动回落到 `ctx.user_id`）：面板视图要能看到
    全团队的动作，默认只看自己的会把「别人已经跟过价了」藏起来，导致重复动作。
    要看自己的，前端传 `user_id=<当前用户>` 即可。
    """
    items = action_log.list_actions(
        ctx.tenant_id,
        days=days,
        asin=asin,
        user_id=user_id,
        limit=limit,
    )
    return {"ok": True, "count": len(items), "items": items}


# ============================================================
# ⑨ get_threshold_schema
# ============================================================


def get_threshold_schema(ctx: CapContext) -> dict:
    return {"ok": True, "groups": spec_groups()}


# ============================================================
# ⑩ get_effective_thresholds
# ============================================================


def get_effective_thresholds(
    ctx: CapContext,
    my_product_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> dict:
    """三层合并后的全量阈值 + 每个键的**来源层**。

    `user_id` 未显式给时用 `ctx.user_id`：这是「身份显式携带」最直接的体现——
    同一个请求换个 `X-User-Id`，这里解析出的阈值就不同，且不需要任何全局变量。
    """
    uid = user_id if user_id is not None else ctx.user_id
    values, sources = settings_store.resolve(
        ctx.tenant_id, user_id=uid, my_product_id=my_product_id
    )
    layers = settings_store.describe_layers(
        ctx.tenant_id, user_id=uid, my_product_id=my_product_id
    )
    versions = {
        group: settings_store.effective_version(
            ctx.tenant_id, group_name=group, user_id=uid, my_product_id=my_product_id
        )
        for group in GROUPS
    }
    return {
        "ok": True,
        "user_id": uid,
        "my_product_id": my_product_id,
        "effective": values,
        "sources": sources,
        "layers": layers,
        "versions": versions,
    }


# ============================================================
# ⑪ list_threshold_versions
# ============================================================


def list_threshold_versions(
    ctx: CapContext,
    scope: str = "company",
    group_name: str = "diff",
    scope_id: Optional[str] = None,
    limit: int = 20,
) -> dict:
    if scope not in settings_store.SCOPE_ORDER:
        return {
            "ok": False,
            "error": f"scope 只能是 {'/'.join(settings_store.SCOPE_ORDER)}，收到 {scope!r}",
        }
    if group_name not in GROUPS:
        return {"ok": False, "error": f"group_name 只能是 {'/'.join(GROUPS)}，收到 {group_name!r}"}
    if scope != settings_store.SCOPE_COMPANY and not scope_id:
        # 不拦的话会拿 scope_id="None" 去查，返回空列表——用户会以为"没有历史"，
        # 而真相是"你没告诉我要看谁的历史"。
        return {
            "ok": False,
            "error": f"scope={scope} 时必须提供 scope_id",
            "hint": "个人层填用户 id，产品层填自家产品 id；看公司默认层可以不传",
        }

    items = settings_store.list_versions(
        scope=scope,
        group_name=group_name,
        scope_id=scope_id,
        tenant_id=ctx.tenant_id,
        limit=limit,
    )
    return {
        "ok": True,
        "scope": scope,
        "scope_id": scope_id,
        "group_name": group_name,
        "group_title": GROUP_TITLES.get(group_name, group_name),
        "count": len(items),
        "items": items,
    }


# ============================================================
# ⑫ list_users
# ============================================================


def list_users(ctx: CapContext, include_identities: bool = True) -> dict:
    items = identity.list_users(ctx.tenant_id, include_identities=include_identities)
    return {
        "ok": True,
        "count": len(items),
        "items": items,
        "default_user_id": identity.default_user_id(ctx.tenant_id),
    }


# ============================================================
# ⑬ list_bindings
# ============================================================


def list_bindings(
    ctx: CapContext,
    competitor_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> dict:
    """绑定列表。`user_id=None` 返回全部用户（面板池子视图需要看到全团队），
    但每条都标 `is_mine`，这样「谁的绑定」一眼可见，不用前端再比一次 id。
    """
    items = binding.list_for(ctx.tenant_id, competitor_id=competitor_id, user_id=user_id)
    for it in items:
        it["is_mine"] = ctx.user_id is not None and it.get("user_id") == ctx.user_id
    return {"ok": True, "count": len(items), "items": items, "my_user_id": ctx.user_id}


# ============================================================
# ⑭ list_capabilities
# ============================================================


def list_capabilities(ctx: CapContext, scope: Optional[str] = None) -> dict:
    """自描述：面板靠这一条渲染出整张能力表，不需要前端硬编码任何能力名。"""
    from app.capabilities import list_capabilities as _list  # 延迟导入，避免循环引用
    from app.capabilities.types import SCOPES

    if scope is not None and scope not in SCOPES:
        return {"ok": False, "error": f"scope 只能是 {'/'.join(SCOPES)}，收到 {scope!r}"}

    items = [cap.to_schema() for cap in _list(scope=scope)]
    return {"ok": True, "count": len(items), "items": items}


# ============================================================
# 注册表
# ============================================================


def _p(*args, **kwargs) -> ParamSpec:
    return ParamSpec(*args, **kwargs)


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        name="list_competitors",
        scope=SCOPE_SHARED,
        group=GROUP_QUERY,
        title="竞品池清单",
        description="列出当前监控的竞品及其最新状态（价格/排名/星级/库存/分层/战场绑定）。",
        params=(
            _p("tier", "只看某一层", T_ENUM, enum=["S", "A", "B", "C", TIER_UNKNOWN],
               help="unknown = 证据不足、暂未判定；不传看全部"),
            _p("status", "只看某种状态", T_ENUM, enum=list(STATUS_VALUES),
               help="active=在盯 / paused=暂停采集 / archived=归档"),
            _p("include_archived", "含已归档", T_BOOL, default=False,
               help="默认不含归档；竞品池管理页要能看见并恢复归档的竞品"),
        ),
        handler=list_competitors,
    ),
    Capability(
        name="get_competitor_detail",
        scope=SCOPE_SHARED,
        group=GROUP_QUERY,
        title="单个竞品画像",
        description="单个竞品的完整档案：当前状态、历史价格与排名趋势、最近信号、历史最低价。",
        params=(
            _p("asin", "竞品 ASIN", "str", required=True, help="要查的竞品 ASIN"),
            _p("history", "回溯快照条数", T_INT, default=14, minimum=1, maximum=200),
        ),
        handler=get_competitor_detail,
    ),
    Capability(
        name="get_daily_brief",
        scope=SCOPE_SHARED,
        group=GROUP_QUERY,
        title="竞品日报",
        description="过去 N 小时所有 P0/P1 级变化，每条带建议动作；含锚点与数据健康摘要。",
        params=(
            _p("hours", "回看小时数", T_INT, default=24, minimum=1, maximum=720,
               help="问『这周』就传 168"),
            _p("include_text", "附带文字版", T_BOOL, default=True,
               help="关掉可以省一点带宽，面板用结构化字段渲染"),
        ),
        handler=get_daily_brief,
    ),
    Capability(
        name="get_my_status",
        scope=SCOPE_SHARED,
        group=GROUP_QUERY,
        title="自家状态汇总",
        description="锚点就绪度、可售天数、库存状态、池子规模、最近 24 小时信号量。",
        params=(),
        handler=get_my_status,
    ),
    Capability(
        name="check_anchor",
        scope=SCOPE_SHARED,
        group=GROUP_QUERY,
        title="锚点体检",
        description="五大维度缺了什么、缺了导致哪个判断失灵、现在能做哪些判断。",
        params=(),
        handler=check_anchor,
    ),
    Capability(
        name="get_change_history",
        scope=SCOPE_SHARED,
        group=GROUP_QUERY,
        title="变化历史",
        description="单个竞品的三条时间线：原始快照、判定出的信号、人做过的动作。",
        params=(
            _p("asin", "竞品 ASIN", "str", required=True),
            _p("days", "回溯天数", T_INT, default=30, minimum=1, maximum=365),
            _p("limit", "每类最多几条", T_INT, default=200, minimum=1, maximum=1000),
        ),
        handler=get_change_history,
    ),
    Capability(
        name="get_data_health",
        scope=SCOPE_SHARED,
        group=GROUP_QUERY,
        title="数据健康",
        description="采集失败轮次、每日价格可信度、快照新鲜度、未映射身份。",
        params=(
            _p("days", "统计窗口天数", T_INT, default=7, minimum=1, maximum=90),
        ),
        handler=get_data_health,
    ),
    Capability(
        name="list_actions",
        scope=SCOPE_SHARED,
        group=GROUP_QUERY,
        title="动作记录",
        description="谁在什么时候对哪个竞品做了什么、依据是什么、验证结论如何。",
        params=(
            _p("days", "回溯天数", T_INT, default=30, minimum=1, maximum=365),
            _p("asin", "只看某个竞品", "str", help="不传看全部"),
            _p("user_id", "只看某个用户", T_INT, minimum=1,
               help="不传看全团队——避免重复跟价"),
            _p("limit", "最多几条", T_INT, default=200, minimum=1, maximum=1000),
        ),
        handler=list_actions,
    ),
    Capability(
        name="get_threshold_schema",
        scope=SCOPE_SHARED,
        group=GROUP_QUERY,
        title="阈值参数字典",
        description="全部可调阈值的中文标签、类型、默认值与范围；面板表单由它驱动。",
        params=(),
        handler=get_threshold_schema,
    ),
    Capability(
        name="get_effective_thresholds",
        scope=SCOPE_SHARED,
        group=GROUP_QUERY,
        title="当前生效阈值",
        description="公司→用户→产品三层合并后的全量值，以及每个键来自哪一层。",
        params=(
            _p("my_product_id", "自家产品 ID", T_INT, minimum=1,
               help="不传则跳过产品覆盖层"),
            _p("user_id", "按哪个用户解析", T_INT, minimum=1,
               help="不传则用本次调用携带的身份"),
        ),
        handler=get_effective_thresholds,
    ),
    Capability(
        name="list_threshold_versions",
        scope=SCOPE_SHARED,
        group=GROUP_QUERY,
        title="阈值版本历史",
        description="某作用域某组阈值的历次变更（新→旧），回滚时用来挑版本号。",
        params=(
            _p("scope", "作用域", T_ENUM, required=True,
               enum=list(settings_store.SCOPE_ORDER)),
            _p("group_name", "阈值分组", T_ENUM, required=True, enum=list(GROUPS)),
            _p("scope_id", "作用域 ID", "str",
               help="company 层留空自动取 tenant；user 填用户 id；product 填产品 id"),
            _p("limit", "最多几条", T_INT, default=20, minimum=1, maximum=100),
        ),
        handler=list_threshold_versions,
    ),
    Capability(
        name="list_users",
        scope=SCOPE_SHARED,
        group=GROUP_QUERY,
        title="用户列表",
        description="本租户的用户及其平台身份映射（飞书 open_id / 企微 userid 等）。",
        params=(
            _p("include_identities", "是否附带身份映射", T_BOOL, default=True),
        ),
        handler=list_users,
    ),
    Capability(
        name="list_bindings",
        scope=SCOPE_SHARED,
        group=GROUP_QUERY,
        title="对标绑定列表",
        description="谁认为哪个竞品与哪个自家产品在同一条战线，依据是什么。",
        params=(
            _p("competitor_id", "只看某个竞品", T_INT, minimum=1),
            _p("user_id", "只看某个用户", T_INT, minimum=1,
               help="不传看全部用户"),
        ),
        handler=list_bindings,
    ),
    Capability(
        name="list_capabilities",
        scope=SCOPE_SHARED,
        group=GROUP_QUERY,
        title="能力清单",
        description="系统当前提供哪些能力、各自参数是什么（面板与文档的自描述来源）。",
        params=(
            _p("scope", "只看某个作用域", T_ENUM, enum=["shared", "agent_only"],
               help="不传看全部"),
        ),
        handler=list_capabilities,
    ),
)
