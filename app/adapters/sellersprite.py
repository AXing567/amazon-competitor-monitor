"""卖家精灵转接桥适配器。

依赖本地已启动的 sellersprite-api-bridge（默认 http://127.0.0.1:8000）。
接口契约见 bridge 的 README「能力清单」。

设计要点：
- 单个接口失败不影响其他接口（分段容错）
- 任何字段拿不到就留 None，并把失败原因写进 errors
- data_quality 按实际拿到的字段量计算，不假报 full
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Optional

import httpx

from app.adapters.base import DataAdapter
from app.adapters.unified import UnifiedSnapshot
from app.config import settings

logger = logging.getLogger(__name__)


class SellerSpriteAdapter(DataAdapter):
    name = "sellersprite"

    def __init__(self, base_url: Optional[str] = None, timeout: Optional[int] = None):
        self.base_url = (base_url or settings.sellersprite_bridge_url).rstrip("/")
        self.timeout = timeout or settings.sellersprite_timeout

    # ------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict) -> tuple[dict, Optional[str]]:
        """请求单个接口，返回 (data, error)。永远不抛异常。"""
        try:
            resp = await client.get(f"{self.base_url}{path}", params=params)
            if resp.status_code != 200:
                return {}, f"{path} HTTP {resp.status_code}"
            payload = resp.json()
            if isinstance(payload, dict) and payload.get("detail"):
                return {}, f"{path} detail: {payload['detail']}"
            return payload if isinstance(payload, dict) else {}, None
        except Exception as exc:  # noqa: BLE001
            return {}, f"{path} 异常: {type(exc).__name__}: {exc}"

    # ------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------

    async def fetch_snapshot(self, asin: str, marketplace: str = "US") -> UnifiedSnapshot:
        snap = UnifiedSnapshot(asin=asin, marketplace=marketplace, source=self.name)
        params = {"asin": asin, "marketplace": marketplace}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            # 并发拉 5 个分段接口；每段失败独立，不影响其他段
            core_t, rank_t, promo_t, review_t, traffic_t = await asyncio.gather(
                self._get(client, "/api/v1/competitor/core-info", params),
                self._get(client, "/api/v1/competitor/sales-rank", params),
                self._get(client, "/api/v1/competitor/promotions", params),
                self._get(client, "/api/v1/competitor/reviews", params),
                self._get(client, "/api/v1/competitor/traffic-keywords", params),
            )

        core, e1 = core_t
        rank, e2 = rank_t
        promo, e3 = promo_t
        review, e4 = review_t
        traffic, e5 = traffic_t

        for err in (e1, e2, e3, e4, e5):
            if err:
                snap.errors.append(err)

        # ---- 核心画像 ----
        if core:
            snap.title = core.get("title")
            snap.brand = core.get("brand")
            snap.image_url = core.get("image_url")
            snap.release_date = core.get("release_date")
            snap.category_path = core.get("category_path") or []
            snap.buybox_owner = core.get("seller_name")
            snap.fulfillment_type = core.get("fulfillment_type")
            snap.original_price = _to_float(core.get("original_price"))
            stock_status = (core.get("stock_status") or "").upper()
            snap.stock_hint = stock_status or None
            if stock_status:
                snap.in_stock = stock_status == "IN_STOCK"
            if snap.price is None:
                snap.price = _to_float(core.get("current_price"))

        # ---- 排名与销量 ----
        if rank:
            snap.rank_main = _to_int(rank.get("bsr_main_rank"))
            snap.rank_main_category = rank.get("bsr_main_category")
            snap.rank_sub = _to_int(rank.get("bsr_sub_rank"))
            snap.rank_sub_category = rank.get("bsr_sub_category")
            snap.monthly_units = _to_int(rank.get("est_monthly_sales"))
            snap.monthly_revenue = _to_float(rank.get("est_monthly_revenue"))
            snap.daily_units = _to_int(rank.get("est_daily_sales"))

        # ---- 促销 ----
        if promo:
            snap.price = _to_float(promo.get("current_price")) or snap.price
            snap.coupon_value = _to_float(promo.get("coupon_value"))
            snap.coupon_type = promo.get("coupon_type")
            snap.deal_type = promo.get("deal_type")
            snap.net_price = _to_float(promo.get("net_price"))

        # ---- 口碑 ----
        if review:
            snap.rating = _to_float(review.get("rating"))
            snap.review_count = _to_int(review.get("ratings_count"))
            snap.monthly_review_increment = _to_int(review.get("monthly_ratings_increment"))

        # ---- 流量 ----
        if traffic:
            snap.traffic_keywords_total = _to_int(traffic.get("traffic_keywords_total"))
            snap.organic_ratio = _to_float(traffic.get("organic_traffic_ratio"))
            snap.sponsored_ratio = _to_float(traffic.get("sponsored_traffic_ratio"))
            snap.keyword_source = traffic.get("source")
            snap.keyword_notice = traffic.get("notice")
            snap.keywords = [
                {
                    "keyword": kw.get("keyword"),
                    "search_volume": kw.get("search_volume"),
                    "organic_rank": kw.get("organic_rank"),
                    "sponsored_rank": kw.get("sponsored_rank"),
                    "traffic_share": kw.get("traffic_share"),
                }
                for kw in (traffic.get("keywords_list") or [])
                if kw.get("keyword")
            ]

        snap.data_quality = _judge_quality(snap)
        snap.captured_at = datetime.now()
        return snap

    async def fetch_keywords(self, asin: str, marketplace: str = "US") -> list[dict]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            data, err = await self._get(
                client,
                "/api/v1/competitor/traffic-keywords",
                {"asin": asin, "marketplace": marketplace},
            )
        if err:
            logger.warning("拉取关键词失败 %s: %s", asin, err)
            return []
        return [
            {
                "keyword": kw.get("keyword"),
                "search_volume": kw.get("search_volume"),
                "organic_rank": kw.get("organic_rank"),
                "sponsored_rank": kw.get("sponsored_rank"),
                "traffic_share": kw.get("traffic_share"),
            }
            for kw in (data.get("keywords_list") or [])
            if kw.get("keyword")
        ]

    async def health(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self.base_url}/health")
                return {
                    "adapter": self.name,
                    "status": "ok" if resp.status_code == 200 else f"http_{resp.status_code}",
                    "bridge": self.base_url,
                }
        except Exception as exc:  # noqa: BLE001
            return {"adapter": self.name, "status": "unreachable", "error": str(exc)}


# ------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------


def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> Optional[int]:
    f = _to_float(v)
    return int(f) if f is not None else None


def _judge_quality(snap: UnifiedSnapshot) -> str:
    """按实际拿到多少关键字段来判定数据质量，不假报。"""
    key_fields = [
        snap.price,
        snap.rank_sub or snap.rank_main,
        snap.rating,
        snap.review_count,
        snap.title,
    ]
    got = sum(1 for f in key_fields if f is not None)
    if got >= 5:
        return "full"
    if got >= 3:
        return "partial"
    if got >= 1:
        return "partial"
    return "failed"
