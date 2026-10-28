"""日报/预警的内容组装。

两条输出路径共用同一份数据：
- 飞书卡片（结构化）
- 纯文本（卡片发不出去时的兜底，也方便 Agent 直接读）

一条设计原则：**每条信号后面都必须跟一个「然后呢」**。
只报「对手降价了」而不给动作，等于把活又推回给老板。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.domain import baseline as baseline_mod
from app.domain.decision import (
    DecisionResult,
    build_out_of_stock_decision,
    judge_price_follow,
)
from app.models import Competitor, MyProduct, Signal, Snapshot

# ============================================================
# 结构
# ============================================================


@dataclass
class BriefItem:
    asin: str
    tier: str
    title: str
    signal_type: str
    level: str
    description: str
    decision: str = "no_action"          # attack / defend / learn / no_action
    headline: str = ""
    actions: list[str] = field(default_factory=list)
    confidence: Optional[float] = None
    competitor_id: Optional[int] = None


@dataclass
class DailyBrief:
    date: str
    anchor: dict = field(default_factory=dict)
    p0: list[BriefItem] = field(default_factory=list)
    p1: list[BriefItem] = field(default_factory=list)
    p2_count: int = 0
    pool: dict = field(default_factory=dict)
    data_health: dict = field(default_factory=dict)
    quiet: bool = False

    # --------------------------------------------------------
    # 文字版
    # --------------------------------------------------------

    def to_text(self) -> str:
        lines: list[str] = []
        lines.append(f"竞品日报 · {self.date}")
        lines.append("")

        # 锚点
        a = self.anchor or {}
        dos = a.get("days_of_supply")
        inv = {
            "alert": "偏紧",
            "normal": "正常",
            "surplus": "充裕",
            "unknown": "未知",
        }.get(a.get("inventory_status", "unknown"), "未知")
        lines.append(
            f"自家锚点：就绪度 {a.get('readiness', 0)}%（{a.get('grade', 'D')} 级）"
            f" · 可售 {f'{dos:.0f} 天' if dos else '未知'}（{inv}）"
        )
        for w in (a.get("warnings") or [])[:2]:
            lines.append(f"  ⚠ {w}")
        lines.append("")

        if self.quiet:
            lines.append("今日无 P0/P1 级变化。")
        else:
            if self.p0:
                lines.append(f"【P0 立即处理】{len(self.p0)} 条")
                for i, it in enumerate(self.p0, 1):
                    lines += _item_lines(i, it)
                lines.append("")

            if self.p1:
                lines.append(f"【P1 今日关注】{len(self.p1)} 条")
                for i, it in enumerate(self.p1, 1):
                    lines += _item_lines(i, it)
                lines.append("")

        if self.p2_count:
            lines.append(f"另有 {self.p2_count} 条 P2 变化已归档（内容改动、排名微调等），不进日报。")
            lines.append("")

        pool = self.pool or {}
        if pool:
            lines.append(
                "竞品池："
                + " · ".join(f"{k} 级 {pool.get(k, 0)} 个" for k in ("S", "A", "B", "C"))
            )

        dh = self.data_health or {}
        if dh.get("failed") or dh.get("partial"):
            lines.append("")
            if dh.get("failed"):
                lines.append(f"⚠ 采集失败：{', '.join(dh['failed'])}（这些竞品今日无数据）")
            if dh.get("partial"):
                lines.append(f"数据不全：{', '.join(dh['partial'])}（结论仅供参考）")

        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "anchor": self.anchor,
            "p0": [it.__dict__ for it in self.p0],
            "p1": [it.__dict__ for it in self.p1],
            "p2_count": self.p2_count,
            "pool": self.pool,
            "data_health": self.data_health,
            "quiet": self.quiet,
        }


def _item_lines(idx: int, it: BriefItem) -> list[str]:
    out = [f"{idx}. [{it.tier}] {it.asin} {_short(it.title)}"]
    out.append(f"   {it.description}")
    if it.headline:
        out.append(f"   → {it.headline}")
    for a in it.actions[:3]:
        out.append(f"   · {a}")
    return out


def _short(text: str, limit: int = 28) -> str:
    text = (text or "").strip()
    return text[:limit] + ("…" if len(text) > limit else "")


# ============================================================
# 生成日报
# ============================================================


def build_daily_brief(
    tenant_id: Optional[str] = None,
    *,
    hours: int = 24,
    include_p1: bool = True,
) -> DailyBrief:
    tenant_id = tenant_id or settings.tenant_id
    since = datetime.now() - timedelta(hours=hours)

    with get_session() as s:
        my = s.execute(
            select(MyProduct).where(MyProduct.tenant_id == tenant_id).limit(1)
        ).scalar_one_or_none()

        assessment = baseline_mod.assess(my) if my else None

        rows = list(
            s.execute(
                select(Signal, Competitor)
                .join(Competitor, Signal.competitor_id == Competitor.id)
                .where(Signal.tenant_id == tenant_id, Signal.detected_at >= since)
                .order_by(desc(Signal.detected_at))
            ).all()
        )

        pool: dict[str, int] = {}
        for c in s.execute(
            select(Competitor).where(
                Competitor.tenant_id == tenant_id, Competitor.status == "active"
            )
        ).scalars():
            pool[c.tier] = pool.get(c.tier, 0) + 1

        health = _data_health(s, tenant_id)

    brief = DailyBrief(
        date=datetime.now().strftime("%Y-%m-%d"),
        anchor=assessment.to_dict() if assessment else {},
        pool=pool,
        data_health=health,
    )

    # 同竞品同类信号只保留最新一条，避免同一件事报三次
    oos_comps = {c.id for sig, c in rows if sig.signal_type in ("out_of_stock", "low_stock")}
    seen: set[tuple[int, str]] = set()
    for sig, comp in rows:
        key = (comp.id, sig.signal_type)
        if key in seen:
            continue
        seen.add(key)

        if sig.level == "P2":
            brief.p2_count += 1
            continue

        item = BriefItem(
            asin=comp.asin,
            tier=comp.tier,
            title=comp.title or "",
            signal_type=sig.signal_type,
            level=sig.level,
            description=sig.description,
            competitor_id=comp.id,
        )
        _attach_action(item, sig, comp, my, oos_comps)

        if sig.level == "P0":
            brief.p0.append(item)
        elif include_p1:
            brief.p1.append(item)
        else:
            brief.p2_count += 1

    brief.quiet = not brief.p0 and not brief.p1
    return brief


# ============================================================
# 给每条信号接上决策
# ============================================================


def _attach_action(
    item: BriefItem,
    sig: Signal,
    comp: Competitor,
    my: Optional[MyProduct],
    oos_comps: Optional[set[int]] = None,
) -> None:
    """把信号交给决策引擎，产出「然后呢」。"""
    dos = None
    if my:
        a = baseline_mod.assess(my)
        dos = a.days_of_supply

    # ---- 最高优先级：他断货了，价格信号全部让路 ----
    # 业务洞察：对手没货可卖时，他标多低都成交不了。这时候跟价是纯粹的自损，
    # 正确动作是抢流量，而不是把利润也降下去。
    if (
        sig.signal_type in ("price_drop", "coupon_increase", "deal_started", "price_band_break")
        and oos_comps
        and comp.id in oos_comps
    ):
        item.decision = "attack"
        item.headline = "他同时断货了——价格已经不重要，直接抢流量"
        item.actions = [
            "不要跟价。他没货，降价成交不了，你跟价只是白丢利润",
            "优先 PAT 商品定位广告打他详情页，接住跳走的买家",
            "他断货变体的核心出单词加竞价，抢自然位",
            "观察他补货时间点，补货前是你的窗口",
        ]
        item.confidence = 0.85
        return

    # ---- 降价 / 深券 / Deal → 走跟价判断 ----
    if sig.signal_type in ("price_drop", "coupon_increase", "deal_started", "price_band_break"):
        if not my or not my.current_price or not my.break_even_price or not my.daily_sales_avg:
            item.decision = "no_action"
            item.headline = "判断不了该不该跟价——自家锚点缺数据"
            item.actions = [
                "补三样东西就能自动判断：当前售价、保本价、日均销量",
                "在补齐之前，先按「不跟」处理，跟价是最贵的错误",
            ]
            return

        try:
            new_price = float(sig.after_value) if sig.after_value else my.current_price
        except (TypeError, ValueError):
            new_price = my.current_price

        unit_profit = my.current_price * (my.target_margin_rate or 0.2)
        res = judge_price_follow(
            competitor_new_price=new_price,
            my_current_price=my.current_price,
            my_break_even_price=my.break_even_price,
            my_unit_profit_now=unit_profit,
            my_daily_units=my.daily_sales_avg,
            competitor_is_clearance=False,
            my_dos=dos,
        )
        _apply(item, res)
        return

    # ---- 断货 / 缺货 → 进攻窗口 ----
    if sig.signal_type in ("out_of_stock", "low_stock", "rank_crash", "buybox_changed"):
        res = build_out_of_stock_decision(
            competitor_asin=comp.asin,
            my_dos=dos,
            is_low_stock=sig.signal_type == "low_stock",
        )
        if sig.signal_type != "out_of_stock":
            # 非断货场景复用动作清单，但把标题改准确
            res.reasons = [sig.description] + res.reasons
        _apply(item, res)
        return

    # ---- 涨价 / 活动结束 → 提价窗口 ----
    if sig.signal_type in ("price_rise", "deal_ended"):
        item.decision = "no_action"
        item.headline = "他在退出价格战，是你的提价窗口"
        item.actions = [
            "先不动，观察 3 天确认他不再回降",
            "确认后小幅提价试探（每次 ≤3%），盯转化率与 BSR",
            "同步下调你的 Coupon 力度，把利润拿回来",
        ]
        return

    # ---- 口碑裂缝 → 差评进攻 ----
    if sig.signal_type in ("rating_drop", "rating_below_floor"):
        item.decision = "attack"
        item.headline = "他口碑出裂缝，可从差评里挖卖点"
        item.actions = [
            "把他近 30 天差评全拉出来，按「可解决 / 不可解决」分类",
            "可解决的那类，直接写进你的图片与五点",
            "不可解决的（如尺寸、材质），在主图区先做预期管理",
        ]
        return

    # ---- 他在加速 → 学习 ----
    if sig.signal_type in ("rank_climb", "review_surge", "main_image_changed", "back_in_stock"):
        item.decision = "learn"
        item.headline = "他在做有效动作，先抄作业再谈对抗"
        item.actions = [
            "拆他的新主图/新标题，找出他主打的那个词",
            "查这个词的搜索量与竞争度，判断值不值得跟",
            "7 天后回看他的 BSR，验证这招是否真有效",
        ]
        return

    # ---- 兜底 ----
    item.decision = "no_action"
    item.headline = "已记录，无标准动作"
    item.actions = ["在 Agent 里问「这条该怎么处理」可获得针对性建议"]


def _apply(item: BriefItem, res: DecisionResult) -> None:
    item.decision = res.decision
    item.headline = res.headline
    item.actions = list(res.actions)
    item.confidence = res.confidence


# ============================================================
# 数据健康度
# ============================================================


def _data_health(s: Session, tenant_id: str) -> dict:
    """最近一次采集的质量分布，让老板知道「今天的结论有多可信」。"""
    failed: list[str] = []
    partial: list[str] = []

    comps = list(
        s.execute(
            select(Competitor).where(
                Competitor.tenant_id == tenant_id, Competitor.status == "active"
            )
        ).scalars()
    )
    for c in comps:
        last = s.execute(
            select(Snapshot)
            .where(Snapshot.competitor_id == c.id)
            .order_by(desc(Snapshot.captured_at))
            .limit(1)
        ).scalar_one_or_none()
        if last is None:
            failed.append(c.asin)
        elif last.data_quality == "failed":
            failed.append(c.asin)
        elif last.data_quality == "partial":
            partial.append(c.asin)
        # 超过 36 小时没更新也算异常
        elif last.captured_at and (datetime.now() - last.captured_at) > timedelta(hours=36):
            partial.append(f"{c.asin}(数据过期)")

    return {"failed": failed, "partial": partial}


# ============================================================
# 单条预警（P0 实时推送用）
# ============================================================


def build_alerts_since(tenant_id: str, minutes: int = 90) -> list[BriefItem]:
    """取出最近 N 分钟内新产生的 P0 信号，带上决策与动作。

    与日报共用同一套 `_attach_action`，保证「实时预警」和「日报」
    对同一件事给出的建议不会打架。
    """
    since = datetime.now() - timedelta(minutes=minutes)
    out: list[BriefItem] = []

    with get_session() as s:
        my = s.execute(
            select(MyProduct).where(MyProduct.tenant_id == tenant_id).limit(1)
        ).scalar_one_or_none()

        rows = list(
            s.execute(
                select(Signal, Competitor)
                .join(Competitor, Signal.competitor_id == Competitor.id)
                .where(
                    Signal.tenant_id == tenant_id,
                    Signal.level == "P0",
                    Signal.detected_at >= since,
                    Signal.pushed_at.is_(None),
                )
                .order_by(desc(Signal.detected_at))
            ).all()
        )

        seen: set[tuple[int, str]] = set()
        oos_comps = {c.id for sg, c in rows if sg.signal_type in ("out_of_stock", "low_stock")}
        for sig, comp in rows:
            key = (comp.id, sig.signal_type)
            if key in seen:
                continue
            seen.add(key)

            item = BriefItem(
                asin=comp.asin,
                tier=comp.tier,
                title=comp.title or "",
                signal_type=sig.signal_type,
                level=sig.level,
                description=sig.description,
                competitor_id=comp.id,
            )
            _attach_action(item, sig, comp, my, oos_comps)
            out.append(item)
            sig.pushed_at = datetime.now()

    return out


def build_alert_text(item: BriefItem) -> str:
    lines = [
        f"⚡ {item.level} · {item.tier} 级竞品 {item.asin}",
        "",
        item.description,
    ]
    if item.headline:
        lines += ["", f"→ {item.headline}"]
    if item.actions:
        lines.append("")
        lines += [f"· {a}" for a in item.actions[:4]]
    if item.confidence is not None:
        lines += ["", f"（判断确定性 {item.confidence:.0%}）"]
    return "\n".join(lines)
