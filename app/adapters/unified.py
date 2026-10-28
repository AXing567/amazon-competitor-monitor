"""统一领域模型。

所有平台的数据都映射到这里。字段缺失一律留 None，绝不用估算值冒充真实值。
`data_quality` 必须上报，下游据此决定话术与决策分支。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class UnifiedSnapshot(BaseModel):
    """一次采集的统一快照。"""

    platform: str = "amazon"
    marketplace: str = "US"
    asin: str

    # ---- 价格 ----
    price: Optional[float] = None
    original_price: Optional[float] = None
    coupon_value: Optional[float] = None
    coupon_type: Optional[str] = None
    net_price: Optional[float] = None
    deal_type: Optional[str] = None
    # 价格的可信来源。只有 "promotion" 才代表 net_price 可信；缺失代表本轮价格不可知。
    # 这个字段存在的唯一目的：阻止下游拿标价去顶替到手价。
    price_source: Optional[str] = None

    # ---- 排名 ----
    rank_main: Optional[int] = None
    rank_main_category: Optional[str] = None
    rank_sub: Optional[int] = None
    rank_sub_category: Optional[str] = None
    rank_label: Optional[str] = "BSR"

    # ---- 销量 ----
    monthly_units: Optional[int] = None
    monthly_revenue: Optional[float] = None
    daily_units: Optional[int] = None

    # ---- 口碑 ----
    rating: Optional[float] = None
    review_count: Optional[int] = None
    monthly_review_increment: Optional[int] = None

    # ---- 供给 ----
    in_stock: Optional[bool] = None
    stock_hint: Optional[str] = None          # IN_STOCK / LOW_STOCK / OOS
    buybox_owner: Optional[str] = None
    fulfillment_type: Optional[str] = None

    # ---- 内容 ----
    title: Optional[str] = None
    brand: Optional[str] = None
    image_url: Optional[str] = None
    release_date: Optional[str] = None
    category_path: list[str] = Field(default_factory=list)

    # ---- 流量 ----
    traffic_keywords_total: Optional[int] = None
    organic_ratio: Optional[float] = None
    sponsored_ratio: Optional[float] = None
    # [{keyword, search_volume, organic_rank, sponsored_rank, traffic_share}]
    keywords: list[dict] = Field(default_factory=list)
    keyword_source: Optional[str] = None
    keyword_notice: Optional[str] = None

    # ---- 元信息 ----
    data_quality: str = "partial"    # full / partial / estimated / failed
    source: str = "unknown"
    captured_at: datetime = Field(default_factory=datetime.now)
    errors: list[str] = Field(default_factory=list)

    def core_keyword_weights(self, coverage: float = 0.8) -> dict[str, float]:
        """截取累计贡献 `coverage` 流量的头部词，返回 {keyword: weight}。

        这是加权词重合度算法的输入。没有流量占比数据的词会被跳过。
        """
        weighted = [
            (kw["keyword"], float(kw.get("traffic_share") or 0))
            for kw in self.keywords
            if kw.get("keyword") and kw.get("traffic_share")
        ]
        if not weighted:
            return {}

        weighted.sort(key=lambda x: x[1], reverse=True)
        total = sum(w for _, w in weighted)
        if total <= 0:
            return {}

        picked: dict[str, float] = {}
        acc = 0.0
        for kw, w in weighted:
            picked[kw] = w
            acc += w
            if acc / total >= coverage:
                break
        return picked


class TieringResult(BaseModel):
    """四同法则 + 加权重合度的判定结果。"""

    passed: bool = False
    tier: str = "C"                  # S / A / B / C
    tier_reasons: dict = Field(default_factory=dict)

    price_ratio: Optional[float] = None
    keyword_overlap: Optional[float] = None
    functional_match: Optional[bool] = None
    node_match: Optional[bool] = None

    reject_reason: Optional[str] = None
