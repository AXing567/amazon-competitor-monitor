"""`SpApiAdapter` / `SelfSnapshot` 的行为测试。

## 这组用例守的是什么

自家数据这条链现在没有真凭据，所以测试的价值不在"跑通"，而在**把契约钉死**：

1. **没配置要吵**。凭据缺失时如果返回一个空快照，调用方看到的是"这个 ASIN 什么都没有"，
   而不是"你还没配 token"——这种沉默的错，排查成本极高。
2. **BuyBox 价 ≠ 标价**。本项目已经因为"拿标价顶替到手价"出过一次事故
   （见 `UnifiedSnapshot.price_source` 的注释）。自家侧同样要分开存，
   所以这里专门断言两个数**不相等**且各自落在各自字段上。
3. **一段挂掉不能拖垮全部**。定价返回 500 时，库存/销量仍要映射成功，
   失败原因进 `errors`，并且该段 provenance 标 `error` 而不是 `unavailable`——
   后者是"SP-API 根本不提供"，两者混起来下游就会把"今天接口挂了"当成"这维度不适用"。
4. **不编值**。`unit_cost` / `rating` / `review_count` 在完整成功路径下**仍然是 None**，
   且 provenance 是 `unavailable`。这条是硬约束：这些数据 SP-API 就是不提供。
5. **两个快照类型不许互相污染**。`SelfSnapshot` 不是 `UnifiedSnapshot`，
   也不许长出竞品专属字段。

## 关于 fixture：都是按文档构造的，不是真实返回

下面每个响应体都是照官方 swagger（`amzn/selling-partner-api-models`）的字段名手写的，
**不是**真实调用抓下来的样本。字段名与嵌套层级已核对：

- `fba-inventory-api-model/fbaInventory.json` → `payload.inventorySummaries[].inventoryDetails.*`
- `sales-api-model/sales.json` → `payload[]` + `unitCount`
- `product-pricing-api-model/productPricingV0.json` → `payload.Summary.BuyBoxPrices[].LandedPrice`
  / `payload.Offers[].MyOffer` + `ListingPrice`
- `product-fees-api-model/productFeesV0.json` → `payload.FeesEstimateResult.FeesEstimate.FeeDetailList[]`
- `catalog-items-api-model/catalogItems_2022-04-01.json` → `summaries` / `images` / `classifications` / `salesRanks`
- `listings-items-api-model/listingsItems_2021-08-01.json` → `items[].sku`

真实返回里可能多出未列出的字段；这里只需要覆盖被映射的那些。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters.self_snapshot import (          # noqa: E402
    FIELDS_NEVER_FROM_SPAPI,
    PROVENANCE_DERIVED,
    PROVENANCE_ERROR,
    PROVENANCE_SPAPI,
    PROVENANCE_UNAVAILABLE,
    SelfSnapshot,
)
from app.adapters.spapi import (                 # noqa: E402
    LWA_TOKEN_URL,
    SpApiAdapter,
    SpApiAuthError,
    SpApiNotConfigured,
)
from app.adapters.unified import UnifiedSnapshot  # noqa: E402

# 本模块不碰数据库，跳过 conftest 的建库夹具：
# 里面的用例全是纯 HTTP 打桩，建库只会引入无关的前置失败面。
__own_db__ = True

ASIN = "B0SELF0001"
SELLER_ID = "A1SELLER0001"
SKU = "SKU-SELF-001"
MARKETPLACE_ID = "ATVPDKIKX0DER"

_CREDS: dict[str, Any] = {
    "refresh_token": "Atzr|fake-refresh-token",
    "client_id": "amzn1.application-oa2-client.fake",
    "client_secret": "fake-client-secret",
    "region": "na",
    "marketplace_id": MARKETPLACE_ID,
}


# ============================================================
# 按文档构造的响应体
# ============================================================


def _token_body() -> dict:
    return {"access_token": "Atza|fake-access-token", "expires_in": 3600, "token_type": "bearer"}


def _sellers_body() -> dict:
    return {
        "payload": [
            {
                "marketplace": {"id": MARKETPLACE_ID, "name": "Amazon.com", "countryCode": "US"},
                "participation": {"isParticipating": True, "hasSuspendedListings": False,
                                  "sellerId": SELLER_ID},
            }
        ]
    }


def _listings_body(asin: str, items: Optional[list] = None) -> dict:
    if items is not None:
        return {"numberOfResults": len(items), "pagination": {}, "items": items}
    return {
        "numberOfResults": 1,
        "pagination": {},
        "items": [
            {
                "sku": SKU,
                "summaries": [
                    {
                        "marketplaceId": MARKETPLACE_ID,
                        "asin": asin,
                        "productType": "TOY_VEHICLE",
                        "conditionType": "new_item",
                        "status": ["BUYABLE"],
                        "itemName": "Diecast Truck",
                    }
                ],
            }
        ],
    }


def _catalog_body(asin: str) -> dict:
    return {
        "asin": asin,
        "summaries": [
            {
                "marketplaceId": MARKETPLACE_ID,
                "itemName": "Diecast Toy Truck, 1:24 Scale",
                "brand": "AcmeToys",
                "browseClassification": {"displayName": "Play Trucks", "classificationId": "166099011"},
            }
        ],
        "images": [
            {
                "marketplaceId": MARKETPLACE_ID,
                "images": [
                    {"variant": "PT01", "link": "https://m.media-amazon.com/images/I/other.jpg",
                     "height": 500, "width": 500},
                    {"variant": "MAIN", "link": "https://m.media-amazon.com/images/I/main.jpg",
                     "height": 1000, "width": 1000},
                ],
            }
        ],
        "classifications": [
            {
                "marketplaceId": MARKETPLACE_ID,
                "classifications": [
                    {"displayName": "Toys & Games", "classificationId": "165793011"},
                    {"displayName": "Toy Vehicles", "classificationId": "166099011"},
                ],
            }
        ],
        "salesRanks": [
            {
                "marketplaceId": MARKETPLACE_ID,
                "classificationRanks": [
                    {"classificationId": "166099011", "title": "Play Trucks", "rank": 4321}
                ],
                "displayGroupRanks": [
                    {"websiteDisplayGroup": "toys-and-games", "title": "Toys & Games", "rank": 12345}
                ],
            }
        ],
    }


def _pricing_body(asin: str, buybox_seller: str) -> dict:
    """BuyBox 价与标价**故意不同**：用来证明两个字段没有被互相顶替。"""
    return {
        "payload": {
            "ASIN": asin,
            "status": "Success",
            "ItemCondition": "New",
            "Identifier": {"MarketplaceId": MARKETPLACE_ID, "ItemCondition": "New", "ASIN": asin},
            "Summary": {
                "LowestPrices": [
                    {
                        "condition": "New",
                        "fulfillmentChannel": "Amazon",
                        "LandedPrice": {"CurrencyCode": "USD", "Amount": 17.50},
                        "ListingPrice": {"CurrencyCode": "USD", "Amount": 17.50},
                        "Shipping": {"CurrencyCode": "USD", "Amount": 0.00},
                    }
                ],
                "BuyBoxPrices": [
                    {
                        "condition": "New",
                        "LandedPrice": {"CurrencyCode": "USD", "Amount": 19.99},   # ← BuyBox 到手价
                        "ListingPrice": {"CurrencyCode": "USD", "Amount": 18.99},
                        "Shipping": {"CurrencyCode": "USD", "Amount": 0.00},
                        "sellerId": buybox_seller,
                    }
                ],
                "NumberOfOffers": [{"condition": "New", "fulfillmentChannel": "Amazon", "OfferCount": 1}],
                "BuyBoxEligibleOffers": [{"condition": "New", "fulfillmentChannel": "Amazon", "OfferCount": 1}],
                "TotalOfferCount": 1,
            },
            "Offers": [
                {
                    "SellerId": SELLER_ID,
                    "SubCondition": "new",
                    "IsFeaturedMerchant": buybox_seller == SELLER_ID,
                    "IsFulfilledByAmazon": True,
                    "MyOffer": True,
                    "ListingPrice": {"CurrencyCode": "USD", "Amount": 18.99},  # ← 本店标价
                    "Shipping": {"CurrencyCode": "USD", "Amount": 0.00},
                }
            ],
        }
    }


def _fees_body() -> dict:
    return {
        "payload": {
            "FeesEstimateResult": {
                "Status": "Success",
                "FeesEstimate": {
                    "TotalFeesEstimate": {"Amount": 6.42},
                    "FeeDetailList": [
                        {"FeeType": "ReferralFee", "FeeAmount": {"Amount": 2.70},
                         "FinalFee": {"Amount": 2.70}},
                        {"FeeType": "FBAPerUnitFulfillmentFee", "FeeAmount": {"Amount": 3.72},
                         "FinalFee": {"Amount": 3.72}},
                    ],
                },
                "FeesEstimateIdentifier": {
                    "MarketplaceId": MARKETPLACE_ID,
                    "IdType": "ASIN",
                    "IdValue": ASIN,
                    "IsAmazonFulfilled": True,
                    "PriceToEstimateFees": {"ListingPrice": {"Amount": 18.99}},
                },
            }
        }
    }


def _inventory_body(asin: str, sku: str) -> dict:
    return {
        "payload": {
            "granularity": {"granularityType": "Marketplace", "granularityId": MARKETPLACE_ID},
            "inventorySummaries": [
                {
                    "asin": asin,
                    "fnSku": "X00FAKEFNSKU",
                    "sellerSku": sku,
                    "condition": "NewItem",
                    "productName": "Diecast Toy Truck",
                    "totalQuantity": 302,
                    "inventoryDetails": {
                        "fulfillableQuantity": 180,
                        "inboundWorkingQuantity": 20,
                        "inboundShippedQuantity": 30,
                        "inboundReceivingQuantity": 40,
                        "reservedQuantity": {
                            "totalReservedQuantity": 12,
                            "pendingCustomerOrderQuantity": 8,
                            "pendingTransshipmentQuantity": 2,
                            "fcProcessingQuantity": 2,
                        },
                        "unfulfillableQuantity": {
                            "totalUnfulfillableQuantity": 5,
                            "customerDamagedQuantity": 3,
                            "warehouseDamagedQuantity": 2,
                        },
                        "researchingQuantity": {"totalResearchingQuantity": 7},
                    },
                }
            ],
        }
    }


def _sales_body(interval: str) -> dict:
    """按请求区间长度返回一个合计值，用来验证 7 日/30 日各用各的窗口。

    规则：短窗口（≤10 天）每天 30 件，长窗口每天 10 件。
    于是 7 日 → 210 件 → 日均 30.0；30 日 → 300 件 → 日均 10.0。
    两个值刻意不同：如果适配器把两个窗口搞混（或用同一个窗口算两次），断言会红。
    """
    start_s, end_s = interval.split("--")
    days = (date.fromisoformat(end_s[:10]) - date.fromisoformat(start_s[:10])).days
    units = days * 30 if days <= 10 else days * 10
    return {
        "payload": [
            {
                "interval": interval,
                "unitCount": units,
                "orderCount": units,
                "orderItemCount": units,
                "averageUnitPrice": {"amount": "18.99", "currencyCode": "USD"},
                "totalSales": {"amount": f"{units * 18.99:.2f}", "currencyCode": "USD"},
            }
        ]
    }


# ============================================================
# 打桩用的假 SP-API
# ============================================================


class FakeSpApi:
    """按路径路由的假服务端。可开关个别接口的失败，用来验证分段容错。"""

    def __init__(self) -> None:
        self.token_calls = 0
        self.fail_pricing = False
        self.fail_token = False
        self.no_listings = False
        self.buybox_seller = SELLER_ID
        self.requests: list[httpx.Request] = []
        #: 没带 access token 的请求。成功路径跑完这里必须为空。
        self.requests_without_token: list[str] = []

    # ---- 路由 ----

    def handler(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911 —— 路由表本就是多分支
        self.requests.append(request)
        path = request.url.path

        if str(request.url) == LWA_TOKEN_URL:
            self.token_calls += 1
            if self.fail_token:
                return httpx.Response(
                    400, json={"error": "invalid_grant", "error_description": "The refresh token is invalid."}
                )
            return httpx.Response(200, json=_token_body())

        # 注意这里返回 403 而不是 assert：断言写在打桩函数里会被适配器的
        # `except Exception` 吞成"网络异常"，失败信息会变得很难读。
        if request.headers.get("x-amz-access-token") != "Atza|fake-access-token":
            self.requests_without_token.append(str(request.url))
            return httpx.Response(
                403, json={"errors": [{"code": "Unauthorized", "message": "missing x-amz-access-token"}]}
            )

        if path == "/sellers/v1/marketplaceParticipations":
            return httpx.Response(200, json=_sellers_body())

        if path.startswith("/listings/2021-08-01/items/"):
            if self.no_listings:
                return httpx.Response(200, json=_listings_body(ASIN, items=[]))
            return httpx.Response(200, json=_listings_body(ASIN))

        if path.startswith("/catalog/2022-04-01/items/"):
            return httpx.Response(200, json=_catalog_body(ASIN))

        if path.startswith("/products/pricing/v0/items/"):
            if self.fail_pricing:
                return httpx.Response(
                    500, json={"errors": [{"code": "InternalFailure", "message": "pricing blew up"}]}
                )
            return httpx.Response(200, json=_pricing_body(ASIN, self.buybox_seller))

        if path.startswith("/products/fees/v0/items/"):
            return httpx.Response(200, json=_fees_body())

        if path == "/fba/inventory/v1/summaries":
            return httpx.Response(200, json=_inventory_body(ASIN, SKU))

        if path == "/sales/v1/orderMetrics":
            return httpx.Response(200, json=_sales_body(request.url.params["interval"]))

        return httpx.Response(404, json={"errors": [{"code": "NotFound", "message": path}]})

    # ---- 便捷方法 ----

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), timeout=5.0)

    def requests_to(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]


async def _fetch(fake: FakeSpApi, asin: str = ASIN, **overrides: Any) -> SelfSnapshot:
    async with fake.client() as client:
        adapter = SpApiAdapter(client=client, **{**_CREDS, **overrides})
        return await adapter.fetch_self_snapshot(asin)


def _run(fake: FakeSpApi, asin: str = ASIN, **overrides: Any) -> SelfSnapshot:
    return asyncio.run(_fetch(fake, asin, **overrides))


# ============================================================
# ① 未配置：必须吵，必须能照做
# ============================================================


def test_configured_is_false_when_no_credentials():
    adapter = SpApiAdapter(
        refresh_token="", client_id="", client_secret="", region="na", marketplace_id=MARKETPLACE_ID
    )
    assert adapter.configured is False
    assert set(adapter.missing_credentials()) == {"refresh_token", "client_id", "client_secret"}


def test_configured_is_true_when_all_three_present():
    adapter = SpApiAdapter(**{**_CREDS, "client": None})
    assert adapter.configured is True
    assert adapter.missing_credentials() == []


def test_unconfigured_raises_with_actionable_message():
    """不能返回空快照：空快照会被读成「这个 ASIN 没数据」，而真相是「你还没配 token」。"""
    fake = FakeSpApi()
    adapter = SpApiAdapter(
        refresh_token="", client_id="", client_secret="", region="na", marketplace_id=MARKETPLACE_ID
    )

    with pytest.raises(SpApiNotConfigured) as exc_info:
        asyncio.run(adapter.fetch_self_snapshot(ASIN))

    message = str(exc_info.value)
    # 三个变量都要点名
    for env in ("SPAPI_REFRESH_TOKEN", "SPAPI_CLIENT_ID", "SPAPI_CLIENT_SECRET"):
        assert env in message, f"错误消息里没提 {env}，拿到照做的人会卡住：\n{message}"
    # 要有"去哪拿"的下一步
    assert "开发者中心" in message and "Seller Central" in message
    # 而且不该有任何网络请求
    assert fake.token_calls == 0 and fake.requests == []


def test_auth_failure_raises_instead_of_returning_empty_snapshot():
    """换 token 失败要抛 `SpApiAuthError`。

    如果这里返回空快照，表现和一个"什么都没有的 ASIN"完全一样，
    排查的人会去查 ASIN，而问题其实在 refresh token。
    """
    fake = FakeSpApi()
    fake.fail_token = True

    with pytest.raises(SpApiAuthError) as exc_info:
        _run(fake)

    message = str(exc_info.value)
    assert "HTTP 400" in message
    assert "SPAPI_REFRESH_TOKEN" in message


# ============================================================
# ② 价格：BuyBox 价与标价必须分开
# ============================================================


def test_buybox_price_and_listing_price_are_not_swapped():
    """BuyBox 到手价（LandedPrice 19.99）与本店标价（ListingPrice 18.99）各归各位。

    这两个数在 fixture 里刻意不同：如果实现里用 `ListingPrice` 顶了 BuyBox，
    或者反过来，下面任意一条断言都会红。
    """
    snap = _run(FakeSpApi())

    assert snap.buybox_price == pytest.approx(19.99)
    assert snap.listing_price == pytest.approx(18.99)
    assert snap.buybox_price != snap.listing_price, (
        "两个价格字段相等，说明其中一个是拿另一个顶替的——这正是 price_source 那次事故的形态"
    )
    assert snap.provenance_of("buybox_price") == PROVENANCE_SPAPI
    assert snap.provenance_of("listing_price") == PROVENANCE_SPAPI


def test_has_buybox_true_when_buybox_belongs_to_us():
    snap = _run(FakeSpApi())
    assert snap.has_buybox is True


def test_buybox_price_kept_but_has_buybox_false_when_competitor_holds_it():
    """别人拿着购物车时：BuyBox 价照样要记录（那是市场实况），has_buybox 必须是 False。"""
    fake = FakeSpApi()
    fake.buybox_seller = "A2SOMEONEELSE"

    snap = _run(fake)

    assert snap.buybox_price == pytest.approx(19.99)
    assert snap.has_buybox is False
    assert snap.listing_price == pytest.approx(18.99)

    # 费用是按本店标价估的，与本店有没有购物车无关
    body = fake.requests_to(f"/products/fees/v0/items/{ASIN}/feesEstimate")[0].content
    assert b"18.99" in body, f"feesEstimate 应该拿本店标价 18.99 做输入，实际请求体：{body!r}"


# ============================================================
# ③ 库存：从 summaries 的 inventoryDetails 映射
# ============================================================


def test_available_units_from_inventory_details_fulfillable_quantity():
    fake = FakeSpApi()
    snap = _run(fake)

    assert snap.available_units == 180, "可售件数应取 inventoryDetails.fulfillableQuantity"

    # 在途 = working 20 + shipped 30 + receiving 40，是求和（derived），不是某一个字段
    assert snap.inbound_units == 90
    assert snap.reserved_units == 12, "预留件数应取 reservedQuantity.totalReservedQuantity"

    assert snap.provenance_of("available_units") == PROVENANCE_SPAPI
    assert snap.provenance_of("reserved_units") == PROVENANCE_SPAPI
    assert snap.provenance_of("inbound_units") == PROVENANCE_DERIVED


def test_inventory_request_carries_details_true_and_resolved_sku():
    """库存接口必须带 details=true（否则没有 inventoryDetails），且必须按 SKU 查。

    SKU 是从 Listings 反查出来的——这条断言把「ASIN → SKU」这道工序也钉住了。
    """
    fake = FakeSpApi()
    _run(fake)

    inventory_requests = fake.requests_to("/fba/inventory/v1/summaries")
    assert len(inventory_requests) == 1
    params = inventory_requests[0].url.params
    assert params["details"] == "true"
    assert params["sellerSkus"] == SKU
    assert params["marketplaceIds"] == MARKETPLACE_ID

    listings_requests = fake.requests_to(f"/listings/2021-08-01/items/{SELLER_ID}")
    assert listings_requests, "应该先走 Listings 把 ASIN 翻译成 SKU"
    assert listings_requests[0].url.params["identifiers"] == ASIN
    assert listings_requests[0].url.params["identifiersType"] == "ASIN"


# ============================================================
# ④ 销量：7 日 / 30 日各用各的窗口
# ============================================================


def test_daily_units_use_their_own_windows():
    """7 日窗口合计 210 件 → 日均 30.0；30 日窗口合计 300 件 → 日均 10.0。

    两个数不同，所以「两个窗口共用一次请求」这种错法一定会被抓到。
    """
    fake = FakeSpApi()
    snap = _run(fake)

    assert snap.daily_units_7d == pytest.approx(30.0)
    assert snap.daily_units_30d == pytest.approx(10.0)
    assert snap.provenance_of("daily_units_7d") == PROVENANCE_DERIVED
    assert snap.provenance_of("daily_units_30d") == PROVENANCE_DERIVED

    # 两个窗口两次请求，且区间长度分别是 7 天和 30 天
    sales_requests = fake.requests_to("/sales/v1/orderMetrics")
    assert len(sales_requests) == 2
    spans = sorted(
        (
            date.fromisoformat(r.url.params["interval"].split("--")[1][:10])
            - date.fromisoformat(r.url.params["interval"].split("--")[0][:10])
        ).days
        for r in sales_requests
    )
    assert spans == [7, 30]


# ============================================================
# ⑤ 分段容错：一个接口 500，其他字段照样回来
# ============================================================


def test_pricing_failure_does_not_break_other_segments():
    """定价 500 时：库存/销量/标题照常映射，错误逐条记录，provenance 标 error。

    注意 `fba_fee` / `referral_fee` 也会跟着失败——因为费用估算必须输入报价，
    报价来自定价。这条依赖是真实的，所以断言它，而不是假装费用还能算。
    """
    fake = FakeSpApi()
    fake.fail_pricing = True

    snap = _run(fake)

    # 其他段必须成功
    assert snap.available_units == 180
    assert snap.daily_units_30d == pytest.approx(10.0)
    assert snap.title == "Diecast Toy Truck, 1:24 Scale"
    assert snap.bsr_rank == 12345

    # 定价段失败：字段留空，provenance=error（不是 unavailable）
    assert snap.buybox_price is None
    assert snap.listing_price is None
    assert snap.provenance_of("buybox_price") == PROVENANCE_ERROR
    assert snap.provenance_of("listing_price") == PROVENANCE_ERROR

    # 错误要能定位到具体接口
    joined = " | ".join(snap.errors)
    assert "pricing/getItemOffers" in joined
    assert "500" in joined

    # 费用因缺报价而整段跳过，且原因说清楚了
    assert snap.fba_fee is None
    assert snap.provenance_of("fba_fee") == PROVENANCE_ERROR
    assert "feesEstimate" in joined


def test_unresolvable_sku_skips_inventory_with_explicit_reason():
    """Listings 没返回 SKU 时库存整段跳过，但目录/销量/定价不受影响。

    关键点：不能把「没查到 SKU」变成「库存为 0」。
    """
    fake = FakeSpApi()
    fake.no_listings = True

    snap = _run(fake)

    assert snap.available_units is None
    assert snap.provenance_of("available_units") == PROVENANCE_ERROR
    assert any("SKU" in e for e in snap.errors), snap.errors

    # 其余段照常
    assert snap.title == "Diecast Toy Truck, 1:24 Scale"
    assert snap.buybox_price == pytest.approx(19.99)
    assert snap.daily_units_7d == pytest.approx(30.0)


# ============================================================
# ⑥ 不编值：SP-API 不提供的字段必须一直是 None
# ============================================================


def test_fields_spapi_does_not_provide_stay_none_and_marked_unavailable():
    """完整成功路径下，这几项**仍然**是 None —— 证明没有为了"好看"编值。

    `unit_cost` 是卖家自己的成本账；`rating` / `review_count` 是商品页公开数据。
    SP-API 都不提供。`None` + provenance=unavailable 才是诚实的表达。
    """
    snap = _run(FakeSpApi())   # 全绿路径：所有打桩接口都返回 200

    assert snap.unit_cost is None
    assert snap.rating is None
    assert snap.review_count is None
    assert snap.buybox_win_rate is None
    assert snap.return_rate is None
    assert snap.coupon is None

    for field in ("unit_cost", "rating", "review_count", "buybox_win_rate", "return_rate", "coupon"):
        assert snap.provenance_of(field) == PROVENANCE_UNAVAILABLE, (
            f"{field} 的 provenance 应该是 unavailable（SP-API 不提供），"
            f"实际 {snap.provenance_of(field)}——被标成 error 会让人以为是接口挂了，白查一轮"
        )
        assert snap.unavailable_reason(field), f"{field} 标了 unavailable 却没写原因"

    # 成功路径下不该有错误
    assert snap.errors == [], snap.errors
    assert snap.data_quality == "full"


def test_no_field_is_left_with_unknown_provenance_on_success():
    """`unknown` 只应该出现在"有代码路径忘了标来源"的情况下。

    成功路径跑完如果还有 unknown，说明某段忘了标 provenance，
    下游会把「没人管」读成「不适用」。
    """
    snap = _run(FakeSpApi())

    unknown = [f for f, p in snap.provenance.items() if p not in (
        PROVENANCE_SPAPI, PROVENANCE_DERIVED, PROVENANCE_UNAVAILABLE, PROVENANCE_ERROR
    )]
    assert unknown == [], f"这些字段没标来源：{unknown}"


# ============================================================
# ⑦ 类型隔离：自家快照不许被当成竞品快照
# ============================================================


def test_self_snapshot_is_a_separate_type_from_unified_snapshot():
    """两个类型平级、互不继承（ARCHITECTURE.md 六/2 的红线）。"""
    assert not issubclass(SelfSnapshot, UnifiedSnapshot)
    assert not issubclass(UnifiedSnapshot, SelfSnapshot)

    # 适配器也不该去实现竞品的 DataAdapter 契约：
    # 一旦它有了 fetch_snapshot() -> UnifiedSnapshot，自家数据就能被当竞品喂给下游。
    from app.adapters.base import DataAdapter

    assert not issubclass(SpApiAdapter, DataAdapter), (
        "SpApiAdapter 不该继承 DataAdapter：那会把 SelfSnapshot 伪装成 UnifiedSnapshot"
    )


def test_self_snapshot_has_no_competitor_only_fields():
    """不许把竞品字段塞进自家类型。

    `keyword_overlap` / `net_price` / `traffic_keywords_total` 这些是竞品侧概念；
    它们出现在 `SelfSnapshot` 里，就意味着有人又想把两条链合并。
    """
    assert "keyword_overlap" not in SelfSnapshot.model_fields
    for competitor_only in (
        "net_price",
        "original_price",
        "rank_main",
        "rank_sub",
        "traffic_keywords_total",
        "organic_ratio",
        "keywords",
        "buybox_owner",
        "monthly_revenue",
    ):
        assert competitor_only not in SelfSnapshot.model_fields, (
            f"{competitor_only} 是竞品字段，不该出现在 SelfSnapshot 里"
        )

    # 反过来：自家专属字段也不在竞品类型里
    for self_only in ("available_units", "unit_cost", "fba_fee", "daily_units_7d"):
        assert self_only not in UnifiedSnapshot.model_fields


# ============================================================
# ⑧ token 缓存
# ============================================================


def test_access_token_is_cached_across_calls():
    """两次采集只换一次 token。每次调用都换 token 会白白触发 LWA 限流。"""
    fake = FakeSpApi()

    async def twice() -> tuple[SelfSnapshot, SelfSnapshot]:
        async with fake.client() as client:
            adapter = SpApiAdapter(client=client, **_CREDS)
            first = await adapter.fetch_self_snapshot(ASIN)
            second = await adapter.fetch_self_snapshot(ASIN)
            return first, second

    first, second = asyncio.run(twice())

    assert first.title == second.title == "Diecast Toy Truck, 1:24 Scale"
    assert fake.token_calls == 1, f"预期只换一次 token，实际换了 {fake.token_calls} 次"


# ============================================================
# ⑨ 输入规范化与配置自检
# ============================================================


def test_asin_is_normalized_and_empty_asin_rejected():
    snap = _run(FakeSpApi(), asin="b0self0001")
    assert snap.asin == "B0SELF0001"
    assert snap.marketplace == "US"

    adapter = SpApiAdapter(**{**_CREDS, "client": None})
    with pytest.raises(ValueError, match="asin"):
        asyncio.run(adapter.fetch_self_snapshot("   "))


def test_unknown_marketplace_code_fails_loudly():
    """不认识的市场代码要报错，不能静默回落到美国站。"""
    adapter = SpApiAdapter(**{**_CREDS, "client": None})
    with pytest.raises(ValueError, match="marketplace"):
        asyncio.run(adapter.fetch_self_snapshot(ASIN, marketplace="MARS"))


def test_marketplace_region_mismatch_is_reported_in_errors():
    """站点与 region 矛盾时先记一条——否则表现为 403/404，会被误判成权限没开。"""
    fake = FakeSpApi()
    snap = _run(fake, asin=ASIN, region="fe")      # 美国站却用了日本/澳洲的 region

    assert any("SPAPI_REGION" in e and "region" in e for e in snap.errors), snap.errors


def test_fees_input_price_comes_from_listing_price():
    """费用估算的输入价必须是本店标价（18.99），不是 BuyBox 价（19.99）。"""
    fake = FakeSpApi()
    _run(fake)

    fees_requests = fake.requests_to(f"/products/fees/v0/items/{ASIN}/feesEstimate")
    assert len(fees_requests) == 1
    body = fees_requests[0].content
    assert b"18.99" in body
    assert b"19.99" not in body


def test_snapshot_metadata_and_quality():
    fake = FakeSpApi()
    snap = _run(fake)
    assert snap.source == "spapi"
    assert snap.captured_at is not None
    assert snap.available_units is not None
    assert snap.days_of_supply == pytest.approx(18.0)   # 180 / 10.0
    assert fake.requests_without_token == [], (
        f"这些请求没带 x-amz-access-token：{fake.requests_without_token}"
    )


# ============================================================
# ⑩ 静态约定：永久 unavailable 字段的清单本身有据可依
# ============================================================


def test_never_from_spapi_registry_is_documented():
    """`FIELDS_NEVER_FROM_SPAPI` 每一项都必须带一句人话原因。

    这些字段是"不是没实现，是这个 API 不提供"的清单，
    将来有人问「为什么 unit_cost 是空的」，答案就应该能在这里读到。
    """
    assert set(FIELDS_NEVER_FROM_SPAPI) >= {"unit_cost", "rating", "review_count"}
    for field, reason in FIELDS_NEVER_FROM_SPAPI.items():
        assert len(reason) >= 10, f"{field} 的原因太短，等于没说"
        assert field in SelfSnapshot.model_fields, f"{field} 不是 SelfSnapshot 的字段"
