"""采集服务：把「拉数据 → 落快照 → 比差异 → 落信号」串成一条链。

一条铁律：采集失败绝不产生信号。数据源挂了就如实记一笔 errors，
宁可不报，也不能拿 None 去比出一个假信号——误报比漏报更能摧毁信任。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.adapters.sellersprite import SellerSpriteAdapter
from app.adapters.unified import UnifiedSnapshot
from app.db import get_session
from app.domain import diff as diff_mod
from app.domain.diff import DiffEngine, SignalDraft
from app.models import Competitor, Decision, MyProduct, SeasonCalendar, Signal, Snapshot

logger = logging.getLogger(__name__)


# ============================================================
# 返回结构
# ============================================================


@dataclass
class CollectResult:
    competitor_id: int
    asin: str
    ok: bool
    data_quality: str = "failed"
    signals: list[SignalDraft] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    is_first_snapshot: bool = False

    @property
    def p0_count(self) -> int:
        return sum(1 for s in self.signals if s.level == "P0")

    @property
    def p1_count(self) -> int:
        return sum(1 for s in self.signals if s.level == "P1")


# ============================================================
# 对外入口（同步 / 异步双版本）
# ============================================================


def collect_competitor(competitor_id: int) -> CollectResult:
    """同步入口，给脚本和 Celery 风格调用用。"""
    return asyncio.run(acollect_competitor(competitor_id))


async def acollect_competitor(competitor_id: int) -> CollectResult:
    """采集单个竞品。内部自己开会话，不依赖外部事务。"""
    # 先把需要的元信息读出来，避免网络请求期间占着会话
    with get_session() as s:
        comp = s.get(Competitor, competitor_id)
        if comp is None:
            return CollectResult(competitor_id=competitor_id, asin="", ok=False, errors=["竞品不存在"])
        asin = comp.asin
        marketplace = comp.marketplace or "US"
        tier = comp.tier
        my = s.get(MyProduct, comp.my_product_id)
        my_price = my.current_price if my else None
        my_asin = my.asin if my else None
        phase = resolve_season_phase(s, my)

    # ---- 拉数据（网络，不持锁） ----
    adapter = SellerSpriteAdapter()
    snap = await adapter.fetch_snapshot(asin, marketplace)

    if snap.data_quality == "failed":
        logger.warning("采集失败 %s: %s", asin, snap.errors)
        _touch_checked(competitor_id)
        return CollectResult(
            competitor_id=competitor_id,
            asin=asin,
            ok=False,
            data_quality="failed",
            errors=snap.errors or ["未取到任何有效字段"],
        )

    # ---- 落库 + 比对 ----
    with get_session() as s:
        comp = s.get(Competitor, competitor_id)
        if comp is None:
            return CollectResult(competitor_id=competitor_id, asin=asin, ok=False, errors=["竞品已被删除"])

        prev_row = s.execute(
            select(Snapshot)
            .where(Snapshot.competitor_id == competitor_id)
            .order_by(desc(Snapshot.captured_at))
            .limit(1)
        ).scalar_one_or_none()

        new_row = _save_snapshot(s, competitor_id, snap)
        comp.last_checked_at = datetime.now()

        if prev_row is None:
            # 第一条快照没有可比对象，只建立基线，不报信号
            return CollectResult(
                competitor_id=competitor_id,
                asin=asin,
                ok=True,
                data_quality=snap.data_quality,
                is_first_snapshot=True,
                errors=snap.errors,
            )

        prev_snap = row_to_unified(prev_row, marketplace)
        engine = DiffEngine(phase=phase)

        oos_confirmed = await _confirm_oos(prev_snap, snap)
        drafts = engine.compare(
            prev_snap,
            snap,
            tier=tier,
            my_price=my_price,
            oos_confirmed=oos_confirmed,
        )

        # 信号去重：同一竞品同一类型在冷却窗口内只落一条
        drafts = [d for d in drafts if not _is_cooling_down(s, competitor_id, d)]

        _save_signals(s, comp.tenant_id, competitor_id, drafts)

        logger.info(
            "采集 %s | quality=%s | 信号 P0=%d P1=%d P2=%d",
            asin,
            snap.data_quality,
            sum(1 for d in drafts if d.level == "P0"),
            sum(1 for d in drafts if d.level == "P1"),
            sum(1 for d in drafts if d.level == "P2"),
        )

        return CollectResult(
            competitor_id=competitor_id,
            asin=asin,
            ok=True,
            data_quality=snap.data_quality,
            signals=drafts,
            errors=snap.errors,
        )


async def acollect_competitor_by_asin(tenant_id: str, asin: str) -> dict:
    """按 ASIN 采集单个竞品，返回 JSON 友好的结构（给 HTTP / 工具层用）。"""
    asin = (asin or "").strip().upper()
    with get_session() as s:
        comp = s.execute(
            select(Competitor).where(
                Competitor.tenant_id == tenant_id, Competitor.asin == asin
            )
        ).scalar_one_or_none()
        if comp is None:
            return {"ok": False, "error": f"{asin} 不在池中"}
        cid = comp.id

    res = await acollect_competitor(cid)
    return {
        "ok": res.ok,
        "asin": res.asin,
        "data_quality": res.data_quality,
        "is_first_snapshot": res.is_first_snapshot,
        "signal_count": len(res.signals),
        "signals": [d.to_row() for d in res.signals],
        "errors": res.errors,
    }


async def collect_all(tenant_id: str) -> list[CollectResult]:
    """采集该租户下所有 active 竞品。并发 3 个，别把 bridge 打挂。"""
    with get_session() as s:
        ids = [
            c.id
            for c in s.execute(
                select(Competitor).where(
                    Competitor.tenant_id == tenant_id,
                    Competitor.status == "active",
                )
            ).scalars()
        ]

    sem = asyncio.Semaphore(3)

    async def _one(cid: int) -> CollectResult:
        async with sem:
            try:
                return await acollect_competitor(cid)
            except Exception as exc:  # noqa: BLE001
                logger.exception("采集异常 competitor_id=%s", cid)
                return CollectResult(
                    competitor_id=cid, asin="", ok=False, errors=[f"{type(exc).__name__}: {exc}"]
                )

    return await asyncio.gather(*(_one(i) for i in ids))


# ============================================================
# 落库
# ============================================================


def _save_snapshot(s: Session, competitor_id: int, snap: UnifiedSnapshot) -> Snapshot:
    row = Snapshot(
        competitor_id=competitor_id,
        captured_at=snap.captured_at,
        price=snap.price,
        coupon_value=snap.coupon_value,
        net_price=snap.net_price,
        deal_type=snap.deal_type,
        rank_main=snap.rank_main,
        rank_sub=snap.rank_sub,
        rank_label=snap.rank_label,
        monthly_units=snap.monthly_units,
        monthly_revenue=snap.monthly_revenue,
        rating=snap.rating,
        review_count=snap.review_count,
        in_stock=snap.in_stock,
        stock_hint=snap.stock_hint,
        buybox_owner=snap.buybox_owner,
        title=snap.title,
        image_url=snap.image_url,
        content_hash=snap.title,          # 用标题做轻量指纹，够用
        data_quality=snap.data_quality,
        source=snap.source,
    )
    s.add(row)
    s.flush()
    return row


def _save_signals(s: Session, tenant_id: str, competitor_id: int, drafts: list[SignalDraft]) -> None:
    for d in drafts:
        s.add(
            Signal(
                tenant_id=tenant_id,
                competitor_id=competitor_id,
                category=d.category,
                signal_type=d.signal_type,
                level=d.level,
                before_value=d.before_value,
                after_value=d.after_value,
                change_pct=d.change_pct,
                description=d.description,
                detected_at=datetime.now(),
            )
        )


# ============================================================
# 冷却去重
# ============================================================

# 同一竞品同一信号类型，多久之内只算一次（防止 15 分钟一轮刷屏）
COOLDOWN_HOURS = {"P0": 4, "P1": 12, "P2": 48}


def _is_cooling_down(s: Session, competitor_id: int, draft: SignalDraft) -> bool:
    window = COOLDOWN_HOURS.get(draft.level, 24)
    since = datetime.now() - timedelta(hours=window)
    exists = s.execute(
        select(Signal.id)
        .where(
            Signal.competitor_id == competitor_id,
            Signal.signal_type == draft.signal_type,
            Signal.detected_at >= since,
        )
        .limit(1)
    ).scalar_one_or_none()
    return exists is not None


# ============================================================
# 辅助
# ============================================================


def _touch_checked(competitor_id: int) -> None:
    with get_session() as s:
        comp = s.get(Competitor, competitor_id)
        if comp:
            comp.last_checked_at = datetime.now()


async def _confirm_oos(prev: UnifiedSnapshot, curr: UnifiedSnapshot) -> bool:
    """断货二次确认：上一次是否也不在售。

    单次采集到 OOS 可能只是数据源抖动，或者 listing 短暂下架。
    只有在售状态连续两次为负，才按「真断货」处理（P0）。
    """
    return prev.in_stock is False or (prev.stock_hint or "").upper() == "OOS"


def row_to_unified(row: Snapshot, marketplace: str = "US") -> UnifiedSnapshot:
    """把数据库快照还原成统一模型，用于和最新采集做对比。"""
    return UnifiedSnapshot(
        asin="",
        marketplace=marketplace,
        price=row.price,
        coupon_value=row.coupon_value,
        net_price=row.net_price,
        deal_type=row.deal_type,
        rank_main=row.rank_main,
        rank_sub=row.rank_sub,
        rank_label=row.rank_label,
        monthly_units=row.monthly_units,
        monthly_revenue=row.monthly_revenue,
        rating=row.rating,
        review_count=row.review_count,
        in_stock=row.in_stock,
        stock_hint=row.stock_hint,
        buybox_owner=row.buybox_owner,
        title=row.title,
        image_url=row.image_url,
        data_quality=row.data_quality,
        source=row.source,
        captured_at=row.captured_at,
    )


def resolve_season_phase(s: Session, my_product: Optional[MyProduct]) -> str:
    """当前处于哪个季节阶段：warmup / blast / cooldown，默认 warmup。"""
    if my_product is None:
        return "warmup"
    today_md = datetime.now().strftime("%m-%d")
    rows = list(
        s.execute(
            select(SeasonCalendar).where(SeasonCalendar.tenant_id == my_product.tenant_id)
        ).scalars()
    )
    for r in rows:
        if r.start_md <= today_md <= r.end_md:
            return r.phase
    return "warmup"
