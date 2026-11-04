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

    # ---- 结论完备性（本轮新增；只加字段，不改上面任何字段的含义）----
    # decidable_dims: 价格带 / 流量池 / 节点 三个维度里，实际能判定的有几个
    # insufficient:   decidable_dims < 2，即证据不足以下结论
    # 为什么需要它：缺数据时各维度都是 None，按 False 处理会得到一个
    # 「沉默的错误判定」——系统不知道自己不知道。而拿不到证据 ≠ 证据表明不成立。
    decidable_dims: int = 0
    insufficient: bool = False

    # ---- 暂定档（本轮新增；同样只加字段，不改上面任何字段的含义）----
    # `insufficient` 说的是「维度少到不配下结论」，`provisional` 说的是
    # 「结论下得出来，但定档所依赖的关键维度里有一维压根没测」——
    # 有一个字母级，但只是暂定。两件事必须分开：
    #   insufficient → effective_tier 是 "unknown"，连入池都不该定；
    #   provisional  → effective_tier 仍是字母级，但面板必须写明「暂定 + 缺什么」。
    # 为什么需要它：`keyword_overlap is None` 的含义是「词维度没测出来」，
    # 原来的 `_decide_letter_tier` 直接返回 "B"（对外语义「轻度重合」），
    # 于是「没测」被写成了「测了、不重合」——一句不报错、面板上看不出来的假话。
    #: 结论是「暂定」的：通过的门槛不足以区分 S/A/B/C 之间的差别
    provisional: bool = False
    #: 哪些维度没测出来（英文键：price_ratio / keyword_overlap / node_match）
    missing_dims: list[str] = Field(default_factory=list)
    #: 一句人话，直接给面板用
    provisional_reason: Optional[str] = None

    @property
    def effective_tier(self) -> str:
        """消费方应该读这个，而不是直接读 `tier`。

        证据不足时返回 `"unknown"`（待判定，缺数据），而不是把 `tier` 里的
        兜底 `C` 当成结论下发。`tier` 保留原含义只为兼容老调用点。

        **注意**：`provisional` 不改变这里的返回值。暂定档仍然有字母级，
        把它压成 `unknown` 会让「暂定 B」和「什么都没测」混成一件事，
        而这两者对使用者的含义完全不同（前者可以先用起来、后者不能用）。
        暂定的表达方式是在 `tier` 之外带上 `provisional` / `missing_dims`，
        由面板文案（`queries._tier_label`）与 `basis_summary` 说出口。
        """
        return "unknown" if self.insufficient else self.tier
