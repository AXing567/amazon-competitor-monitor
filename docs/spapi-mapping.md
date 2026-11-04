# SP-API 字段映射表（自家数据）

> 这张表是这个任务最实用的产出。将来有人问「为什么 `unit_cost` 是空的」，
> 答案在这里，不在某个人的记忆里。
>
> 对应代码：`app/adapters/self_snapshot.py`（类型）、`app/adapters/spapi.py`（取数）。
>
> **核实状态**一列的含义：
> - ✅ **已核对**：字段名/嵌套层级是照官方 swagger 核对过的（仓库 `amzn/selling-partner-api-models`
>   里的 JSON 模型文件，比文档站的渲染页面可靠）。表里标 ✅ 的路径可以直接照抄。
> - ⚠️ **待复核**：按记忆或按文档片段写的，等真凭据到手要对着真实返回再确认一遍。
> - ❌ **不提供**：SP-API 没有这个数据源。换真凭据也一样是空，这不是"还没实现"。

---

## 一、总览：字段 → 接口

| `SelfSnapshot` 字段 | SP-API 接口 | 方法 + 路径 | 取值路径 | 拿不到时 | 状态 |
|---|---|---|---|---|---|
| `title` | Catalog Items | `GET /catalog/2022-04-01/items/{asin}` | `summaries[].itemName` | `None` | ✅ |
| `brand` | Catalog Items | 同上 | `summaries[].brand` | `None` | ✅ |
| `image_url` | Catalog Items | 同上（`includedData` 要带 `images`） | `images[].images[]` 里 `variant=MAIN` 那项的 `link` | 取第一张；再没有则 `None` | ✅ |
| `category_path` | Catalog Items | 同上（要带 `classifications`） | `classifications[].classifications[].displayName` | `[]` | ✅ 路径<br>⚠️ 见下方「类目路径」说明 |
| `bsr_rank` | Catalog Items | 同上（要带 `salesRanks`） | `salesRanks[].displayGroupRanks[].rank` | `None` | ✅ 路径<br>⚠️ 取第一个排名组，取哪个类目的名次未定 |
| `available_units` | FBA Inventory | `GET /fba/inventory/v1/summaries` | `payload.inventorySummaries[].inventoryDetails.fulfillableQuantity` | `None` + `errors` | ✅ |
| `inbound_units` | FBA Inventory | 同上 | `inboundWorkingQuantity` + `inboundShippedQuantity` + `inboundReceivingQuantity` **求和** | `None` | ✅（求和是我们做的 → provenance=`derived`） |
| `reserved_units` | FBA Inventory | 同上 | `inventoryDetails.reservedQuantity.totalReservedQuantity` | `None` | ✅ |
| `daily_units_7d` | Sales | `GET /sales/v1/orderMetrics` | `payload[].unitCount` 合计 ÷ 7 | `None` | ✅ 路径<br>⚠️ `interval` 边界行为待复核 |
| `daily_units_30d` | Sales | 同上 | `payload[].unitCount` 合计 ÷ 30 | `None` | 同上 |
| `buybox_price` | Product Pricing | `GET /products/pricing/v0/items/{Asin}/offers` | `payload.Summary.BuyBoxPrices[].LandedPrice.Amount` | `None` | ✅ |
| `listing_price` | Product Pricing | 同上 | `payload.Offers[]` 里 `MyOffer=true` 那条的 `ListingPrice.Amount` | `None` + `errors` | ✅ |
| `has_buybox` | Product Pricing | 同上 | `BuyBoxPrices[].sellerId` 与本店 `sellerId` 比对 | `None`（拿不到 sellerId） | ✅（比对是我们做的 → `derived`） |
| `fba_fee` | Product Fees | `POST /products/fees/v0/items/{Asin}/feesEstimate` | `payload.FeesEstimateResult.FeesEstimate.FeeDetailList[]` 里 `FeeType=FBAPerUnitFulfillmentFee` 的 `FeeAmount.Amount` | `None` + `errors` | ✅ 路径<br>⚠️ `FeeType` 字符串取值待复核 |
| `referral_fee` | Product Fees | 同上 | 同上，`FeeType=ReferralFee` 的 `FeeAmount.Amount` | `None` + `errors` | ✅ 路径<br>⚠️ 同上 |
| `sku` | Listings Items | `GET /listings/2021-08-01/items/{sellerId}?identifiers={asin}&identifiersType=ASIN` | `items[].sku` | `None`，**并导致库存整段跳过** | ✅ |
| `coupon` | —— | —— | —— | 永远 `None` | ❌ 不提供，见下 |
| `buybox_win_rate` | —— | —— | —— | 永远 `None` | ❌ 不提供，见下 |
| `return_rate` | （Reports，未实现） | —— | —— | 永远 `None` | ❌ 不提供，见下 |
| `unit_cost` | —— | —— | —— | 永远 `None` | ❌ 不提供，见下 |
| `rating` | —— | —— | —— | 永远 `None` | ❌ 不提供，见下 |
| `review_count` | —— | —— | —— | 永远 `None` | ❌ 不提供，见下 |

补充说明两条不属于任何映射、但决定了能不能跑通的事：

| 用途 | 接口 | 路径 | 状态 |
|---|---|---|---|
| 换 LWA access token | LWA | `POST https://api.amazon.com/auth/o2/token`（`grant_type=refresh_token`） | ✅ |
| 取本店 `sellerId`（Listings 的路径参数） | Sellers | `GET /sellers/v1/marketplaceParticipations` → `payload[].participation.sellerId` | ⚠️ 路径与字段名是照记忆写的，待复核 |

---

## 二、SP-API **确实不提供**的字段

这一节要写清楚"依据"。区分三种情况，**不要把"我没找到"写成"不提供"**。

### 1. `unit_cost`（单件成本）— ❌ 不提供

**依据**：SP-API 没有"卖家成本"这个资源。翻遍各 API 的模型文件，没有任何接口的响应里
带买地/采购/单件成本字段。成本是卖家自己的经营数据，只存在于 Seller Central 后台的
「成本管理」（部分站点）/ 第三方 ERP / 自己的账里。Product Fees API 能算的是**平台抽多少**，
不是**你花了多少**。

**结论**：`None`，`provenance=unavailable`。这不是"还没实现"——实现了也不会有值。

### 2. `rating` / `review_count`（星级 / 评论数）— ❌ 不提供（有保留）

**依据**：SP-API 面向的是"卖家自己的经营数据"与"目录基础信息"。商品页上的星级和评论数
属于**商品详情页的公开展示数据**，Catalog Items 返回的 `summaries`
只有 `itemName` / `brand` / `manufacturer` / `browseClassification` 这类目录属性，
没有星级与评论数。

**保留意见（待复核）**：Seller Central 的 Brand Analytics 后台有「客户评论（Customer Reviews）」
视图，里面确实按 ASIN 给了星级与评论数。**但我没有核实它是否作为 SP-API 的
`reportType` 暴露出来**。所以我写的是"当前不提供"，不是"永久不可能"。
拿到真凭据、确认了品牌备案之后，这一条要回来重看。

### 3. `buybox_win_rate`（购物车成交占比）— ❌ 不提供

**依据**：Product Pricing API 给的是**此刻**的报价结构（谁在购物车里、最低价多少），
不给"这段时间内购物车成交了多少比例"。后者是 Seller Central 「购物车」报告/品牌分析的产物，
没有对应的 SP-API 接口。

### 4. `return_rate`（退货率）— ❌ 不提供现成比率

**依据**：SP-API 有退货**明细**报告（逐笔退货），但没有**比率**。要得到退货率，
得自己拉报告、按 ASIN 汇总、再除以同期销量——而且分母用"下单量"还是"发货量"会得出不同结论，
这个口径必须卖家自己定。所以这里留 `None`，而不是拿一个口径不明的数糊上去。

### 5. `coupon`（券面额）— ❌ 不提供（与我们原先的假设不同）

**依据**：核对 `productPricingV0.json` 的 `Summary` / `Offers` 定义，
**没有任何券相关字段**：`LowestPrices` / `BuyBoxPrices` / `NumberOfOffers` /
`BuyBoxEligibleOffers` / `Offers[]`（`SellerId` / `ListingPrice` / `Shipping` /
`ShippingTime` / `SellerFeedbackRating` / `ShipsFrom` / `IsFulfilledByAmazon` / `MyOffer`）
都只有价格与报价信息。

> ⚠️ **这一条纠正了任务书里的一个假设**：「`coupon` 来自 Pricing `getItemOffers`」是**不成立的**。
> 券是促销/优惠券模块的东西，Pricing API 只是"报价快照"，它反映的是**已经被券影响之后**的
> 展示价（`LandedPrice`），但不会告诉你"这里面有多少是券"。
> 是否存在能覆盖自家券的报告类型，**我没有核实 → 待复核**。

---

## 三、几条容易写错的细节

### 1. FBA Inventory 的响应键是 `inventorySummaries`，不是 `summaries`

很多二手资料（和最初的记忆）会写成 `payload.summaries`。核对 `fbaInventory.json` 后确认是：

```
GetInventorySummariesResult = { granularity, inventorySummaries: InventorySummaries }
InventorySummary            = { inventoryDetails, totalQuantity }
InventoryDetails            = { fulfillableQuantity, inboundReceivingQuantity,
                                inboundShippedQuantity, inboundWorkingQuantity,
                                researchingQuantity, reservedQuantity, unfulfillableQuantity }
```

代码里两个键都兜住了（`inventorySummaries` 优先），但**以 `inventorySummaries` 为准**。

### 2. `details=true` 是必须的

不带这个参数，`inventoryDetails` 整个不返回，只剩一个 `totalQuantity`。
而"可售"恰恰在 `inventoryDetails.fulfillableQuantity` 里。代码里写死了 `details=true`，
并有用例钉住（`test_inventory_request_carries_details_true_and_resolved_sku`）。

### 3. 库存接口只认 SKU，不认 ASIN

`getInventorySummaries` 的查询参数里只有 `sellerSku` / `sellerSkus`，**没有 asin**。
所以必须先用 Listings Items 把 ASIN 翻成 SKU，而 Listings 又要 `sellerId`（路径参数），
`sellerId` 得从 Sellers API 拿。这条链断了，库存就整段拿不到——代码里会把
"因为缺 SKU 所以没查库存"写进 `errors`，而不是留一个看起来像"库存为 0"的 `None`。

### 4. BuyBox 价 ≠ 标价，而且都不是"到手价"的完整答案

- `BuyBoxPrices[].LandedPrice` = 购物车赢家的**含运费**总价；
- `Offers[].ListingPrice`（`MyOffer=true` 那条）= **本店自己的挂牌价**，不含运费。

本项目已经因为"拿标价顶替到手价"出过一次事故（见 `UnifiedSnapshot.price_source` 的注释）。
这里同样分开存两个字段，且 `feesEstimate` 的输入价**只用本店标价**——
用例 `test_fees_input_price_comes_from_listing_price` 专门断言请求体里是 18.99 而不是 19.99。

### 5. `feesEstimate` 是**估算**，不是实收

它要求你传一个 `PriceToEstimateFees.ListingPrice` 进去，然后告诉你"在这个价上，
平台会抽多少"。所以：

- 它的输入依赖定价那一段（拿不到标价 → 费用整段跳过，并在 `errors` 里说明原因）；
- 它的结果是估算值 → `provenance=derived`，不是 `spapi` 那种"观测值"。
- 另外 `MoneyType` 在 Product Fees 里**只有 `Amount`，没有 `CurrencyCode`**
  （这点和 Pricing/Sales 不同），所以别指望从这里拿币种。

### 6. `orderMetrics` 统计的是**下单件数**，不是销量

`unitCount` = 区间内下单的商品件数（ordered units）。它既不是"发货件数"，
也不是"扣除退货后的净销量"。代码里的字段名因此是 `daily_units_*`，
注释里也写明"是下单量"。要净销量得再减退货报告。

用 `granularity=Total` 而不是 `Day` 的原因：让服务端把整个区间汇总成一条，
避免自己按 `interval` 字符串切日期、猜时区边界——那是"差一天"这类错误最爱出现的地方。
⚠️ `Total` 粒度对区间长度是否有上限、`interval` 的时区归属，**待复核**。

### 7. 类目路径不是完整链路

`classifications[].classifications[].displayName` 返回的是"这个 ASIN 被归类到的浏览节点名"，
按组罗列，**不保证是根 → 叶的完整链**。代码按返回顺序填进 `category_path`。
下游如果拿 `len(category_path)` 当"层级深度"用，会得出错误结论——
`node_match` 这类判断在 `category_path` 只有一个元素时应该保守处理。⚠️ 待复核。

### 8. 站点与 region 必须匹配

`SPAPI_REGION=na|eu|fe` 决定主机，`marketplaceId` 决定站点。二者错配的表現是 403/404，
看起来像"权限没开"。代码会在 `errors` 里先记一条配置提示
（`test_marketplace_region_mismatch_is_reported_in_errors`）。

### 9. 限流

按 swagger 的 usage plan：

| 接口 | 速率 |
|---|---|
| `getItemOffers` | 0.5 次/秒，burst 1 |
| `getCatalogItem` | 2 次/秒，burst 2 |
| `searchListingsItems` | 5 次/秒，burst 5 |

一次 `fetch_self_snapshot` 大约打 8 个请求（token 换完就缓存）。
真正的批量采集必须自己排队——本适配器**没有**做限流，
因为它现在只服务"自家几个 ASIN"这个量级。⚠️ 上量前要补限流。

---

## 四、等真凭据到手，第一件事做什么

按这个顺序验，能最快把上面所有 ⚠️ 变成 ✅：

1. 只调 `GET /sellers/v1/marketplaceParticipations` → 确认 `sellerId` 字段路径。
2. 再调 Listings，确认能按 `identifiers`+`identifiersType=ASIN` 查到 SKU。
3. 调 FBA Inventory，**对比后台库存页**的"可售/在库/在途"三个数，
   确认 `fulfillableQuantity` / 在途三项 / `totalReservedQuantity` 与我们理解的一致。
4. 调 `orderMetrics`，用后台"业务报告"里同区间的销量对一遍，
   确认 `Total` 粒度和你以为的区间口径一致（尤其月初/月末）。
5. 调 Product Fees，随便改个价看费用怎么变——顺便确认 `FeeType` 字符串的确切拼写。
6. 最后才轮到 ⚠️ 待复核的那三条（Brand Analytics 评论报告、券报告、类目链路）。
