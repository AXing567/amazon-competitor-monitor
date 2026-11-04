"""自家产品的采集快照 `SelfSnapshot`。

## 为什么是独立类型，而不是复用 `UnifiedSnapshot`

`UnifiedSnapshot` 描述**竞品**：它是从商品页/第三方数据源看到的「别人长什么样」。
`SelfSnapshot` 描述**自家**：它是从后台接口读到的「我自己什么状态、什么成本、卖了多少」。
两者的字段几乎不重叠——库存明细、采购成本、费用结构、退货率，这些竞品侧根本没有；
而词流量、竞品到手价、BSR 名次这些自家侧也拿不到。

硬合并的代价不是"多几个空字段"，而是**空值语义被混掉**：

- 竞品 `net_price=None` 的意思是「本轮价格不可知（接口挂了或页面没报价）」；
- 自家 `unit_cost=None` 的意思是「这个数据源根本不提供」（成本不存在于任何 Amazon 接口里）。

同一个 `None` 在两条链上要做完全不同的事。共用一个结构，必定有人在某一侧写错，
而错法不会报错，只会让结论悄悄偏掉——这正是本项目最贵的一类 bug。

所以：本类与 `app/adapters/unified.py:UnifiedSnapshot` **平级、互不继承**，
两边只在 L2 认知层通过显式参数相遇（`my_price=` / `my_dos=`）。
这条是 `ARCHITECTURE.md` 六/2 的红线，不是风格偏好。

## 字段设计原则：按「SP-API 到底存不存在这个字段」设计

每个字段都对应一个真实的 SP-API 数据来源（见 `docs/spapi-mapping.md` 的对照表）。
**不存在**的字段一律 `None`，并在 `provenance` 里标成 `unavailable` —— 绝不编值：

| 字段 | SP-API 有吗 | 说明 |
|---|---|---|
| `unit_cost` | 没有 | 卖家自己的采购成本，只在后台「成本管理」/ERP 里 |
| `rating` / `review_count` | 没有 | 商品页公开数据，不是后台接口 |
| `buybox_win_rate` | 没有 | 需 Brand Analytics 或报表，且不一定对试用账号开放 |
| `return_rate` | 没有（只有逐笔退货报告） | 要用报表自算，本适配器未实现 |
| `coupon` | 没有（Pricing 响应无券字段） | 已核对 swagger，见映射表 |

## `provenance`：分清「真的拿不到」和「代码没写」

`provenance[field]` 取值：

- ``spapi`` —— 值直接来自 SP-API 某个接口。**值仍可能是 None**，那表示
  「接口返回里没有这一项」（例如该 ASIN 今天没有 BuyBox 报价），不是「不提供」。
- ``derived`` —— 由 SP-API 的返回值聚合/计算而来（如 7 日均量、进仓在途合计、
  `feesEstimate` 按传入报价估算的费用）。
- ``unavailable`` —— SP-API 不提供这个数据源。换一套真凭据也一样是空。
- ``error`` —— 本可以做，但**这一轮**对应接口调用失败；原因在 `errors` 里。
- ``unknown`` —— 没有任何代码路径标注过来源。**出现它就是 bug**：
  说明某条新加的字段忘了标来源，下游会把「没人管」误读成「不适用」。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ------------------------------------------------------------------
# provenance 取值（下游请用常量，不要写字面量）
# ------------------------------------------------------------------
PROVENANCE_SPAPI = "spapi"
PROVENANCE_DERIVED = "derived"
PROVENANCE_UNAVAILABLE = "unavailable"
PROVENANCE_ERROR = "error"
PROVENANCE_UNKNOWN = "unknown"

PROVENANCE_VALUES: tuple[str, ...] = (
    PROVENANCE_SPAPI,
    PROVENANCE_DERIVED,
    PROVENANCE_UNAVAILABLE,
    PROVENANCE_ERROR,
    PROVENANCE_UNKNOWN,
)

#: 「SP-API 确实不提供」的字段 → 原因。构造时自动标成 ``unavailable``，
#: 适配器**不要**去覆盖这些字段（覆盖了就是编值）。
FIELDS_NEVER_FROM_SPAPI: dict[str, str] = {
    "unit_cost": (
        "SP-API 没有任何接口返回卖家自己的单件成本；"
        "成本只存在于卖家后台「成本管理」或 ERP 里。不是没实现，是这个 API 不提供。"
    ),
    "rating": (
        "SP-API 不返回商品页星级；星级是商品页公开数据，不是后台接口。"
        "（Brand Analytics 后台有「客户评论」视图，但对应的 reportType 尚未核实 → 待复核）"
    ),
    "review_count": (
        "SP-API 不返回商品页评论数；同 rating，属商品页公开数据。"
    ),
    "buybox_win_rate": (
        "没有接口直接返回「购物车成交占比」；需要 Brand Analytics 或专门的报表，"
        "且不一定对试用账号开放。"
    ),
    "return_rate": (
        "没有接口直接返回退货率；只能用退货报告逐笔自算，本适配器未实现（只有原始报告，没有现成比率）。"
    ),
    "coupon": (
        "Pricing API 的响应结构里没有券字段（已核对 productPricingV0 swagger 的 "
        "Summary/Offers 定义）；是否存在别的能覆盖券的报告类型尚未核实 → 待复核。"
    ),
}

#: 元信息字段，不参与 provenance 覆盖（它们本身就是元信息）。
_META_FIELDS: frozenset[str] = frozenset(
    {"asin", "marketplace", "source", "captured_at", "errors", "provenance"}
)

#: 判定 `data_quality` 用的关键字段。少一个就降一档，不假报 full。
_QUALITY_FIELDS: tuple[str, ...] = (
    "available_units",
    "buybox_price",
    "daily_units_30d",
    "title",
)


class SelfSnapshot(BaseModel):
    """自家产品的一次采集结果。

    `extra="forbid"`：写错字段名要**立刻报错**，而不是被静默丢掉。
    静默丢字段在本项目里等价于「这个维度永远是空的」，而没人会去查为什么。
    """

    model_config = ConfigDict(extra="forbid")

    # ---- 身份 ----
    asin: str
    marketplace: str = "US"
    #: 卖家的 SKU（自家 listing 的内部编号）。SP-API 的库存接口只认 SKU 不认 ASIN，
    #: 所以它不是一个"顺手带上的字段"，而是库存那一段能不能跑起来的前提。
    sku: Optional[str] = None

    # ---- 供给（FBA Inventory: getInventorySummaries）----
    #: 可售（可用）库存件数，来自 summaries[].inventoryDetails.fulfillableQuantity。
    #: 注意：这是 FBA 网络里的可用量，不含在途。
    available_units: Optional[int] = None
    #: 在途/待入仓合计 = inboundWorkingQuantity + inboundShippedQuantity
    #: + inboundReceivingQuantity。接口把它们拆成三个字段，这里合计（derived）。
    inbound_units: Optional[int] = None
    #: 已预留件数，来自 inventoryDetails.reservedQuantity.totalReservedQuantity。
    reserved_units: Optional[int] = None

    # ---- 销量（Sales: getOrderMetrics）----
    #: 近 7 日**日均**下单件数（unitCount 合计 / 7）。是"下单量"，不是"发货量"。
    daily_units_7d: Optional[float] = None
    #: 近 30 日日均下单件数（unitCount 合计 / 30）。
    daily_units_30d: Optional[float] = None

    # ---- 价格（Pricing: getItemOffers）----
    #: 购物车价（BuyBox 的 LandedPrice，含运费）。
    #: 与本类的 `listing_price` **不是一回事**：项目里已经因为「拿标价顶替到手价」出过事故，
    #: 所以这两个字段分开存，谁也不许顶替谁。
    buybox_price: Optional[float] = None
    #: 本店自己的标价（Offers[] 里 MyOffer=true 那条的 ListingPrice），不含运费。
    listing_price: Optional[float] = None
    #: 本店是否持有购物车。由 BuyBoxPrices[].sellerId 与本店 sellerId 比对得出（derived）。
    #: None 表示无法判定（拿不到 sellerId），False 表示本轮确实没有本店的 BuyBox 报价。
    has_buybox: Optional[bool] = None
    #: 券面额。SP-API 无对应字段 → 永远 None，provenance=unavailable。见 FIELDS_NEVER_FROM_SPAPI。
    coupon: Optional[float] = None

    # ---- 购物车占比 ----
    #: 购物车成交占比（0~1）。无接口 → 永远 None。
    buybox_win_rate: Optional[float] = None

    # ---- 费用（Product Fees: getMyFeesEstimateForASIN）----
    #: FBA 单件配送费。来自 FeeDetailList 中 FBAPerUnitFulfillmentFee。
    #: 注意：这是 Amazon 按**我们传入的报价**估算的费用，不是已成交订单的实收 → derived。
    fba_fee: Optional[float] = None
    #: 佣金。来自 FeeDetailList 中 ReferralFee。同上为估算值。
    referral_fee: Optional[float] = None

    # ---- 成本（SP-API 不提供）----
    #: 单件成本。SP-API 没有这个接口 → 永远 None，provenance=unavailable。
    unit_cost: Optional[float] = None

    # ---- 退货（需报表）----
    #: 退货率。只有逐笔退货报告，没有现成比率 → 永远 None。
    return_rate: Optional[float] = None

    # ---- 口碑（SP-API 不提供）----
    #: 星级。商品页公开数据 → 永远 None。
    rating: Optional[float] = None
    #: 评论数。商品页公开数据 → 永远 None。
    review_count: Optional[int] = None

    # ---- 排位 ----
    #: BSR 名次。来自 Catalog Items 的 salesRanks[].displayGroupRanks[].rank
    #: （需 includedData 带 salesRanks）。取第一个排名组的 rank。
    bsr_rank: Optional[int] = None

    # ---- 内容（Catalog Items: getCatalogItem）----
    #: 类目路径。来自 classifications[].classifications[].displayName。
    #: 注意：Catalog 返回的是「被归类到的浏览节点名」，**不保证**是从根到叶的完整链。
    category_path: list[str] = Field(default_factory=list)
    title: Optional[str] = None
    brand: Optional[str] = None
    image_url: Optional[str] = None

    # ---- 元信息 ----
    #: 每个字段的来源，见模块 docstring 的取值说明。
    provenance: dict[str, str] = Field(default_factory=dict)
    #: 本轮的失败原因（逐段记录，不合并成一句）。
    errors: list[str] = Field(default_factory=list)
    source: str = "spapi"
    captured_at: datetime = Field(default_factory=datetime.now)

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------

    @field_validator("asin")
    @classmethod
    def _normalize_asin(cls, v: str) -> str:
        """ASIN 统一大写去空格。空值直接拒绝——没有 ASIN 的快照没有任何意义。"""
        if not isinstance(v, str) or not v.strip():
            raise ValueError("asin 不能为空")
        return v.strip().upper()

    @field_validator("marketplace")
    @classmethod
    def _normalize_marketplace(cls, v: str) -> str:
        return (v or "US").strip().upper()

    def model_post_init(self, __context: object) -> None:
        """补齐没被标注来源的字段。

        - `FIELDS_NEVER_FROM_SPAPI` 里的字段 → `unavailable`（结构性缺失）；
        - 其余数据字段 → `unknown`（有人忘了标注，应视为 bug）。

        这样下游永远能三选一：真的拿不到 / 这轮失败 / 代码没写。
        """
        for name in type(self).model_fields:
            if name in _META_FIELDS:
                continue
            if name in FIELDS_NEVER_FROM_SPAPI:
                self.provenance.setdefault(name, PROVENANCE_UNAVAILABLE)
            else:
                self.provenance.setdefault(name, PROVENANCE_UNKNOWN)

    # ------------------------------------------------------------------
    # 下游读取用的辅助方法
    # ------------------------------------------------------------------

    def provenance_of(self, field_name: str) -> str:
        """读某个字段的来源；没标注过返回 `unknown`（= 代码漏标，不是"不适用"）。"""
        return self.provenance.get(field_name, PROVENANCE_UNKNOWN)

    def unavailable_reason(self, field_name: str) -> Optional[str]:
        """该字段为什么永远拿不到（只有结构性缺失才有原因，其余返回 None）。"""
        return FIELDS_NEVER_FROM_SPAPI.get(field_name)

    @property
    def data_quality(self) -> str:
        """按实际拿到多少关键字段判定，不假报 `full`。

        关键字段：可用库存 / 购物车价 / 30 日均量 / 标题。
        四个全有 → full；有一个以上 → partial；一个都没有 → failed。
        """
        got = sum(1 for name in _QUALITY_FIELDS if getattr(self, name) is not None)
        if got >= len(_QUALITY_FIELDS):
            return "full"
        if got >= 1:
            return "partial"
        return "failed"

    @property
    def days_of_supply(self) -> Optional[float]:
        """可售天数 DOS。任一输入缺失就返回 None，绝不瞎算。

        与 `MyProduct.days_of_supply` 同规则：数据不全时返回 None，不用 0 或均值冒充。
        30 日均量为 0 时也不做除法（除零没有业务含义）。
        """
        if self.available_units is None:
            return None
        if not self.daily_units_30d or self.daily_units_30d <= 0:
            return None
        return self.available_units / self.daily_units_30d


__all__ = [
    "SelfSnapshot",
    "PROVENANCE_SPAPI",
    "PROVENANCE_DERIVED",
    "PROVENANCE_UNAVAILABLE",
    "PROVENANCE_ERROR",
    "PROVENANCE_UNKNOWN",
    "PROVENANCE_VALUES",
    "FIELDS_NEVER_FROM_SPAPI",
]
