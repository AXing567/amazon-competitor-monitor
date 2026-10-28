"""竞品池管理（步骤 1 的落地）。

加一个竞品不是「插一条记录」那么简单，完整动作是：
拉公开数据 → 跑四同法则 → 判分层 → 写库 → 建第一条基线快照。

所有入口都返回 dict，方便 Agent 工具层直接吐给模型，不用二次转换。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import desc, select

from app.adapters.sellersprite import SellerSpriteAdapter
from app.config import settings
from app.db import get_session
from app.domain import baseline as baseline_mod
from app.domain.tiering import judge_tier
from app.models import Competitor, MyProduct, Signal, Snapshot

logger = logging.getLogger(__name__)


# ============================================================
# 自家产品
# ============================================================


def get_my_product(tenant_id: Optional[str] = None) -> Optional[dict]:
    tenant_id = tenant_id or settings.tenant_id
    with get_session() as s:
        my = s.execute(
            select(MyProduct).where(MyProduct.tenant_id == tenant_id).limit(1)
        ).scalar_one_or_none()
        if my is None:
            return None
        return _my_to_dict(my)


def ensure_my_product(
    tenant_id: Optional[str] = None,
    *,
    asin: str = "",
    auto_fetch: bool = True,
) -> dict:
    """确保存在自家产品记录。没有就建，并且能自动填的尽量填上。"""
    tenant_id = tenant_id or settings.tenant_id

    with get_session() as s:
        my = s.execute(
            select(MyProduct).where(MyProduct.tenant_id == tenant_id).limit(1)
        ).scalar_one_or_none()
        if my is None:
            my = MyProduct(tenant_id=tenant_id, asin=asin, marketplace="US")
            s.add(my)
            s.flush()
        my_id = my.id

    filled: list[str] = []
    if auto_fetch:
        filled = _autofill(my_id)

    with get_session() as s:
        my = s.get(MyProduct, my_id)
        out = _my_to_dict(my)
        out["auto_filled"] = filled
        return out


def _autofill(my_id: int) -> list[str]:
    """拉自家 ASIN 的公开数据，补齐锚点。网络失败静默跳过。"""
    with get_session() as s:
        my = s.get(MyProduct, my_id)
        if my is None or not my.asin:
            return []
        asin = my.asin
        marketplace = my.marketplace or "US"

    try:
        snap = asyncio.run(SellerSpriteAdapter().fetch_snapshot(asin, marketplace))
    except Exception as exc:  # noqa: BLE001
        logger.warning("自动补全锚点失败: %s", exc)
        return []

    if snap.data_quality == "failed":
        return []

    with get_session() as s:
        my = s.get(MyProduct, my_id)
        filled = baseline_mod.prefill_from_snapshot(my, snap)
        # 顺手把日均销量从月销换算过来（仅在原本为空时）
        if not my.daily_sales_avg and snap.monthly_units:
            my.daily_sales_avg = round(snap.monthly_units / 30, 1)
            filled.append("daily_sales_avg(按 月销/30 估算)")
        return filled


def update_baseline(tenant_id: Optional[str], **fields) -> dict:
    """更新自家锚点。只接受白名单字段。"""
    tenant_id = tenant_id or settings.tenant_id

    allowed = {
        "asin",
        "fba_available_units",
        "daily_sales_avg",
        "lead_time_days",
        "current_price",
        "break_even_price",
        "target_margin_rate",
        "sub_category_rank",
        "rating",
        "review_count",
        "title",
        "image_url",
        "core_keywords",
    }

    updated: list[str] = []
    with get_session() as s:
        my = s.execute(
            select(MyProduct).where(MyProduct.tenant_id == tenant_id).limit(1)
        ).scalar_one_or_none()
        if my is None:
            my = MyProduct(tenant_id=tenant_id, marketplace="US")
            s.add(my)
            s.flush()

        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            setattr(my, k, v)
            updated.append(k)

        # 填了售价与毛利率但没填保本价 → 自动推算一个，并明确标注是推算值
        inferred = None
        if "break_even_price" not in updated and my.current_price and my.target_margin_rate:
            inferred = baseline_mod.estimate_break_even_price(
                price=my.current_price, margin_rate=my.target_margin_rate
            )
            if inferred and not my.break_even_price:
                my.break_even_price = inferred
                updated.append("break_even_price(按毛利率推算)")

        out = _my_to_dict(my)
        out["updated_fields"] = updated
        out["inferred"] = {"break_even_price": inferred} if inferred else {}
        return out


# ============================================================
# 竞品
# ============================================================


def add_competitor(
    tenant_id: Optional[str],
    asin: str,
    *,
    functional_match: Optional[bool] = None,
    force: bool = False,
) -> dict:
    """加一个竞品进池。会先拉数据跑四同法则，再决定进不进、进哪一级。"""
    tenant_id = tenant_id or settings.tenant_id
    asin = (asin or "").strip().upper()
    if not asin:
        return {"ok": False, "error": "ASIN 不能为空"}

    with get_session() as s:
        my = s.execute(
            select(MyProduct).where(MyProduct.tenant_id == tenant_id).limit(1)
        ).scalar_one_or_none()
        if my is None:
            return {
                "ok": False,
                "error": "还没有自家产品锚点，请先设置（至少要有当前售价）",
            }
        my_id = my.id
        my_price = my.current_price
        my_keywords = my.core_keywords or {}
        my_daily = my.daily_sales_avg
        my_path = None

        exists = s.execute(
            select(Competitor).where(
                Competitor.tenant_id == tenant_id, Competitor.asin == asin
            )
        ).scalar_one_or_none()
        if exists:
            return {
                "ok": False,
                "error": f"{asin} 已在池中（{exists.tier} 级）",
                "competitor_id": exists.id,
            }

    # ---- 拉公开数据 ----
    try:
        snap = asyncio.run(SellerSpriteAdapter().fetch_snapshot(asin, "US"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"采集失败：{type(exc).__name__}: {exc}"}

    if snap.data_quality == "failed":
        return {
            "ok": False,
            "error": "没取到任何有效数据，无法判定是否同战场",
            "detail": snap.errors[:5],
        }

    # ---- 跑四同法则 ----
    res = judge_tier(
        my_price=my_price,
        my_core_keywords=my_keywords,
        my_category_path=my_path,
        my_daily_units=my_daily,
        comp=snap,
        functional_match=functional_match,
    )

    if not res.passed and not force:
        return {
            "ok": False,
            "error": "未通过四同法则，不放进池子",
            "reject_reason": res.reject_reason,
            "price_ratio": res.price_ratio,
            "keyword_overlap": res.keyword_overlap,
            "node_match": res.node_match,
            "hint": "如果你确信它是竞品，可以 force=true 强制加入，标记为观察级（C）",
            "snapshot": _brief_snapshot(snap),
        }

    tier = res.tier if res.passed else "C"

    # ---- 写库 ----
    with get_session() as s:
        comp = Competitor(
            tenant_id=tenant_id,
            my_product_id=my_id,
            platform="amazon",
            marketplace="US",
            asin=asin,
            tier=tier,
            tier_reasons=res.tier_reasons,
            tier_updated_at=datetime.now(),
            price_ratio=res.price_ratio,
            keyword_overlap=res.keyword_overlap,
            functional_match=res.functional_match,
            node_match=res.node_match,
            title=snap.title,
            image_url=snap.image_url,
            brand=snap.brand,
            status="active" if res.passed else "observing",
        )
        s.add(comp)
        s.flush()

        # 建第一条基线快照，后续比差异都靠它
        s.add(
            Snapshot(
                competitor_id=comp.id,
                captured_at=datetime.now(),
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
                content_hash=snap.title,
                data_quality=snap.data_quality,
                source=snap.source,
            )
        )
        comp_id = comp.id

    return {
        "ok": True,
        "competitor_id": comp_id,
        "asin": asin,
        "tier": tier,
        "status": "active" if res.passed else "observing",
        "price_ratio": res.price_ratio,
        "keyword_overlap": res.keyword_overlap,
        "tier_reasons": res.tier_reasons,
        "snapshot": _brief_snapshot(snap),
        "note": (
            "已建立基线，明天开始能收到他的变化"
            if snap.data_quality == "full"
            else f"数据不全（{snap.data_quality}），部分信号可能缺失"
        ),
    }


def list_competitors(tenant_id: Optional[str], tier: Optional[str] = None) -> list[dict]:
    tenant_id = tenant_id or settings.tenant_id
    with get_session() as s:
        stmt = select(Competitor).where(
            Competitor.tenant_id == tenant_id, Competitor.status != "archived"
        )
        if tier:
            stmt = stmt.where(Competitor.tier == tier.upper())
        comps = list(s.execute(stmt.order_by(Competitor.tier, Competitor.asin)).scalars())

        out = []
        for c in comps:
            last = s.execute(
                select(Snapshot)
                .where(Snapshot.competitor_id == c.id)
                .order_by(desc(Snapshot.captured_at))
                .limit(1)
            ).scalar_one_or_none()
            out.append(
                {
                    "id": c.id,
                    "asin": c.asin,
                    "tier": c.tier,
                    "status": c.status,
                    "title": _short(c.title, 40),
                    "price": last.price if last else None,
                    "net_price": last.net_price if last else None,
                    "rank_sub": last.rank_sub if last else None,
                    "rating": last.rating if last else None,
                    "review_count": last.review_count if last else None,
                    "in_stock": last.in_stock if last else None,
                    "price_ratio": c.price_ratio,
                    "keyword_overlap": c.keyword_overlap,
                    "last_checked_at": c.last_checked_at.isoformat() if c.last_checked_at else None,
                    "data_quality": last.data_quality if last else "none",
                }
            )
        return out


def get_competitor_detail(tenant_id: Optional[str], asin: str, history: int = 14) -> dict:
    """单个竞品的完整画像：当前状态 + 历史趋势 + 最近信号。"""
    tenant_id = tenant_id or settings.tenant_id
    asin = (asin or "").strip().upper()

    with get_session() as s:
        comp = s.execute(
            select(Competitor).where(
                Competitor.tenant_id == tenant_id, Competitor.asin == asin
            )
        ).scalar_one_or_none()
        if comp is None:
            return {"ok": False, "error": f"{asin} 不在池中"}

        snaps = list(
            s.execute(
                select(Snapshot)
                .where(Snapshot.competitor_id == comp.id)
                .order_by(desc(Snapshot.captured_at))
                .limit(history)
            ).scalars()
        )
        signals = list(
            s.execute(
                select(Signal)
                .where(Signal.competitor_id == comp.id)
                .order_by(desc(Signal.detected_at))
                .limit(15)
            ).scalars()
        )

        latest = snaps[0] if snaps else None

        # 价格底线
        prices = [x.net_price or x.price for x in snaps if (x.net_price or x.price)]
        all_time_low = min(prices) if prices else comp.all_time_low_price

        return {
            "ok": True,
            "asin": comp.asin,
            "tier": comp.tier,
            "status": comp.status,
            "title": comp.title,
            "brand": comp.brand,
            "tier_reasons": comp.tier_reasons,
            "price_ratio": comp.price_ratio,
            "keyword_overlap": comp.keyword_overlap,
            "all_time_low_price": all_time_low,
            "current": _brief_snapshot_row(latest) if latest else None,
            "history": [
                {
                    "at": (x.captured_at or datetime.now()).strftime("%m-%d %H:%M"),
                    "price": x.net_price or x.price,
                    "rank": x.rank_sub or x.rank_main,
                    "rating": x.rating,
                    "reviews": x.review_count,
                    "in_stock": x.in_stock,
                }
                for x in snaps[:history]
            ],
            "recent_signals": [
                {
                    "at": x.detected_at.strftime("%m-%d %H:%M"),
                    "type": x.signal_type,
                    "level": x.level,
                    "desc": x.description,
                }
                for x in signals
            ],
        }


def archive_competitor(tenant_id: Optional[str], asin: str, reason: str = "") -> dict:
    tenant_id = tenant_id or settings.tenant_id
    with get_session() as s:
        comp = s.execute(
            select(Competitor).where(
                Competitor.tenant_id == tenant_id, Competitor.asin == asin.strip().upper()
            )
        ).scalar_one_or_none()
        if comp is None:
            return {"ok": False, "error": "不在池中"}
        comp.status = "archived"
        return {"ok": True, "asin": comp.asin, "note": reason or "已归档，不再采集但仍保留历史"}


# ============================================================
# 工具
# ============================================================


def _my_to_dict(my: MyProduct) -> dict:
    a = baseline_mod.assess(my)
    return {
        "id": my.id,
        "asin": my.asin,
        "current_price": my.current_price,
        "break_even_price": my.break_even_price,
        "target_margin_rate": my.target_margin_rate,
        "daily_sales_avg": my.daily_sales_avg,
        "fba_available_units": my.fba_available_units,
        "lead_time_days": my.lead_time_days,
        "days_of_supply": a.days_of_supply,
        "inventory_status": a.inventory_status,
        "sub_category_rank": my.sub_category_rank,
        "rating": my.rating,
        "review_count": my.review_count,
        "core_keywords_count": len(my.core_keywords or {}),
        "readiness": a.readiness,
        "grade": a.grade,
        "warnings": a.warnings,
    }


def _brief_snapshot(snap) -> dict:
    return {
        "asin": snap.asin,
        "title": _short(snap.title, 60),
        "price": snap.price,
        "net_price": snap.net_price,
        "coupon_value": snap.coupon_value,
        "rank_sub": snap.rank_sub,
        "monthly_units": snap.monthly_units,
        "rating": snap.rating,
        "review_count": snap.review_count,
        "in_stock": snap.in_stock,
        "stock_hint": snap.stock_hint,
        "data_quality": snap.data_quality,
    }


def _brief_snapshot_row(row) -> dict:
    return {
        "captured_at": row.captured_at.strftime("%Y-%m-%d %H:%M") if row.captured_at else None,
        "price": row.price,
        "net_price": row.net_price,
        "coupon_value": row.coupon_value,
        "rank_sub": row.rank_sub,
        "monthly_units": row.monthly_units,
        "rating": row.rating,
        "review_count": row.review_count,
        "in_stock": row.in_stock,
        "stock_hint": row.stock_hint,
        "data_quality": row.data_quality,
    }


def _short(text: Optional[str], limit: int = 60) -> str:
    text = (text or "").strip()
    return text[:limit] + ("…" if len(text) > limit else "")
