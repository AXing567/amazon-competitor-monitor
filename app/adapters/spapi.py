"""Amazon SP-API 适配器：抓**自家**数据。

## 和 `SellerSpriteAdapter` 的区别

那个抓竞品（商品页/第三方数据），产出 `UnifiedSnapshot`；
这个抓自家后台，产出 `SelfSnapshot`。两者都在 L1 取数层，但**产出不同的类型**。

刻意**不**继承 `app/adapters/base.py:DataAdapter`：
`DataAdapter` 的抽象方法是 `fetch_snapshot() -> UnifiedSnapshot`，
让自家数据去满足这个签名，等于承认「自家快照可以当竞品快照用」——
而这正是 `ARCHITECTURE.md` 六/2 那条红线要防的事。少一层抽象，多一层保护。

## 容错策略（与 `SellerSpriteAdapter.fetch_snapshot` 一致）

- 每一段接口**独立失败**：定价挂了，库存和销量照样返回；
- 失败原因逐条进 `SelfSnapshot.errors`，不合并成一句「采集失败」；
- 失败段的字段留 `None`，`provenance` 标成 `error`（区别于「SP-API 不提供」的 `unavailable`）；
- 但**凭据/鉴权失败**会抛异常，不返回空快照：那是配置问题，
  返回空对象会让人以为"接口暂时没数据"，实际是 token 根本换不出来。

## 分段依赖（不能乱序）

::

    Sellers(取 sellerId) ──► Listings(ASIN→SKU) ──► FBA Inventory(要 SKU)
    Catalog ─┐
    Pricing ─┼─► 本店标价 ──► Product Fees(要报价做估算输入)
    Sales ───┘

所以：`sellerId` 拿不到 → 库存整段跳过；`listing_price` 拿不到 → 费用整段跳过。
这两种跳过都会在 `errors` 里写明「因为缺什么而没跑」，不会被误读成"没有库存/没有费用"。

## 不联网

本模块只组装请求，不做隐式网络调用。`httpx.AsyncClient` 可从构造函数注入，
测试用 `httpx.MockTransport` 打桩即可，无需打外网。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from app.adapters.self_snapshot import (
    PROVENANCE_DERIVED,
    PROVENANCE_ERROR,
    PROVENANCE_SPAPI,
    SelfSnapshot,
)
from app.config import settings

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 常量
# ------------------------------------------------------------------

#: LWA（Login with Amazon）换 token 的端点。全局唯一，与 region 无关。
LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

#: region → SP-API 主机。地区选错时 SP-API 会返回 403/404，所以这里做成显式映射。
REGION_HOSTS: dict[str, str] = {
    "na": "https://sellingpartnerapi-na.amazon.com",
    "eu": "https://sellingpartnerapi-eu.amazon.com",
    "fe": "https://sellingpartnerapi-fe.amazon.com",
}

#: 站点代码 → marketplaceId。只收录常用站点；不在表里也可以直接传 marketplaceId。
MARKETPLACE_IDS: dict[str, str] = {
    "US": "ATVPDKIKX0DER",
    "CA": "A2EUQ1WTGCTBG2",
    "MX": "A1AM78C64UM0Y8",
    "BR": "A2Q3Y263D00KWC",
    "UK": "A1F83G8C2ARO7P",
    "GB": "A1F83G8C2ARO7P",
    "DE": "A1PA6795UKMFR9",
    "FR": "A13V1IB3VIYZZH",
    "IT": "APJ6JRA9NG5V4",
    "ES": "A1RKKUPIHCS9HS",
    "NL": "A1805IZSGTT6HS",
    "SE": "A2NODRKZP88ZB9",
    "PL": "A1C3SOZRARQ6R3",
    "TR": "A33AVAJ2PDY3EV",
    "AE": "A2VIGQ35RCS4UG",
    "SA": "A17E79C6D8DWNP",
    "EG": "ARBP9OOSHTCHU",
    "IN": "A21TJRUUN4KGV",
    "JP": "A1VC38T7YXB528",
    "AU": "A39IBJ37TRP1C6",
    "SG": "A19VAU5U5O7RUS",
}

#: 各 marketplaceId 所属 region。用来自查「站点和 SPAPI_REGION 是否自相矛盾」。
_MARKETPLACE_REGION: dict[str, str] = {
    **{MARKETPLACE_IDS[c]: "na" for c in ("US", "CA", "MX", "BR")},
    **{
        MARKETPLACE_IDS[c]: "eu"
        for c in ("UK", "GB", "DE", "FR", "IT", "ES", "NL", "SE", "PL", "TR", "AE", "SA", "EG", "IN")
    },
    **{MARKETPLACE_IDS[c]: "fe" for c in ("JP", "AU", "SG")},
}

#: marketplaceId 形如 ATVPDKIKX0DER：A 开头 + 大写字母数字，长度 11~16。
_MARKETPLACE_ID_RE = re.compile(r"^A[A-Z0-9]{10,15}$")

#: token 提前多少秒换新。避免"刚好过期"的那一瞬间打出去一个 401。
TOKEN_REFRESH_MARGIN_SECONDS = 120

#: 视为「FBA 单件配送费」的 FeeType。官方枚举未在 swagger 里固定，这里放宽一点。
_FBA_FEE_TYPES: frozenset[str] = frozenset({"FBAPerUnitFulfillmentFee", "FBAFulfillmentFee"})
#: 视为「佣金」的 FeeType。
_REFERRAL_FEE_TYPES: frozenset[str] = frozenset({"ReferralFee"})

#: 各分段字段 → 用于批量标 provenance / 批量记失败，避免字段漏标。
_SKU_FIELDS: tuple[str, ...] = ("sku",)
_CATALOG_FIELDS: tuple[str, ...] = ("title", "brand", "image_url", "category_path", "bsr_rank")
_PRICING_FIELDS: tuple[str, ...] = ("buybox_price", "listing_price", "has_buybox")
_SALES_FIELDS: tuple[str, ...] = ("daily_units_7d", "daily_units_30d")
_INVENTORY_FIELDS: tuple[str, ...] = ("available_units", "inbound_units", "reserved_units")
_FEES_FIELDS: tuple[str, ...] = ("fba_fee", "referral_fee")


# ------------------------------------------------------------------
# 异常
# ------------------------------------------------------------------


class SpApiError(RuntimeError):
    """SP-API 适配器的基类异常。"""

    code = "spapi_error"


class SpApiAuthError(SpApiError):
    """LWA 换 token 失败（凭据错、应用未授权、网络不通）。

    单列一个类型：这种情况返回空快照是**误导**——所有段都会是 None，
    看起来像"这个 ASIN 什么都没有"，实际是 token 根本换不出来。
    """

    code = "spapi_auth_failed"


class SpApiNotConfigured(SpApiError):
    """凭据没配齐。消息里必须写清"缺哪个变量、去哪拿、填到哪"。"""

    code = "spapi_not_configured"

    #: 配置字段 → (环境变量名, 获取方式)
    _HINTS: dict[str, tuple[str, str]] = {
        "refresh_token": (
            "SPAPI_REFRESH_TOKEN",
            "Seller Central → 设置 → 用户权限 → 开发者中心，创建一个应用并勾选需要的角色"
            "（库存/订单/定价/商品信息），审核通过后在该应用页点「授权」拿到 refresh token",
        ),
        "client_id": (
            "SPAPI_CLIENT_ID",
            "同一个开发者中心应用详情页里的「LWA client ID」（形如 amzn1.application-oa2-client.xxx）",
        ),
        "client_secret": (
            "SPAPI_CLIENT_SECRET",
            "同一个应用详情页里的「LWA client secret」，点「显示」后复制",
        ),
    }

    def __init__(self, missing: list[str], detail: str = "") -> None:
        self.missing: list[str] = list(missing)
        self.detail = detail
        super().__init__(self._build_message())

    def _build_message(self) -> str:
        lines = ["SP-API 未配置：缺少 " + "、".join(self.missing) + "，无法取自家数据。"]
        if self.detail:
            lines.append(f"补充：{self.detail}")
        lines.append("补法：")
        for i, name in enumerate(self.missing, start=1):
            env, how = self._HINTS[name]
            lines.append(f"  {i}. {how}")
            lines.append(f"     然后填到 .env：{env}=...（参考 .env.example）")
        lines.append("三项齐了之后 `SpApiAdapter.configured` 才会是 True。")
        return "\n".join(lines)


# ------------------------------------------------------------------
# 适配器
# ------------------------------------------------------------------


def env_name(field: str) -> str:
    """配置字段名 → 环境变量名。

    面板上要说「缺 SPAPI_REFRESH_TOKEN」而不是「缺 refresh_token」——后者让人
    不知道去哪填。映射只存在于 `_HINTS` 一处，这里只是读它，不另抄一份。
    """
    return SpApiNotConfigured._HINTS.get(field, (field.upper(), ""))[0]


class SpApiAdapter:
    """自家数据适配器。一次 `fetch_self_snapshot` = 一组分段请求。

    参数全部可注入（`client` / 三项凭据 / region / marketplace_id），
    便于测试打桩，也便于将来多店铺并存时按店铺实例化。
    """

    name = "spapi"

    def __init__(
        self,
        *,
        client: Optional[httpx.AsyncClient] = None,
        refresh_token: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        region: Optional[str] = None,
        marketplace_id: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        self._client = client
        self._refresh_token = (
            refresh_token if refresh_token is not None else settings.spapi_refresh_token
        )
        self._client_id = client_id if client_id is not None else settings.spapi_client_id
        self._client_secret = (
            client_secret if client_secret is not None else settings.spapi_client_secret
        )
        self._region = (region if region is not None else settings.spapi_region) or "na"
        self._default_marketplace_id = (
            marketplace_id if marketplace_id is not None else settings.spapi_marketplace_id
        ) or "ATVPDKIKX0DER"
        self._timeout = timeout

        # LWA token 缓存。用 monotonic 计时，避开系统时钟回拨。
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

        # 卖家账号 ID：Listings Items 必须带它。取到后缓存在实例上，避免每次重查。
        self._seller_id: Optional[str] = None

    # ------------------------------------------------------------
    # 配置状态
    # ------------------------------------------------------------

    def missing_credentials(self) -> list[str]:
        """返回缺失的配置项（内部名）。空列表 = 齐全。"""
        missing: list[str] = []
        if not (self._refresh_token or "").strip():
            missing.append("refresh_token")
        if not (self._client_id or "").strip():
            missing.append("client_id")
        if not (self._client_secret or "").strip():
            missing.append("client_secret")
        return missing

    @property
    def configured(self) -> bool:
        """LWA 三项凭据齐了才算配置好。"""
        return not self.missing_credentials()

    @property
    def region(self) -> str:
        return (self._region or "").strip().lower()

    @property
    def host(self) -> str:
        """SP-API 主机。region 不认识时抛 `ValueError`（配置错误要吵，不要猜）。"""
        region = self.region
        if region not in REGION_HOSTS:
            raise ValueError(
                f"SPAPI_REGION={self._region!r} 不认识，可用值：{sorted(REGION_HOSTS)}。"
                f"北美站点用 na，欧洲/中东/印度用 eu，日本/澳洲用 fe。"
            )
        return REGION_HOSTS[region]

    def _ensure_configured(self) -> None:
        missing = self.missing_credentials()
        if missing:
            raise SpApiNotConfigured(missing)

    def _resolve_marketplace_id(self, marketplace: str) -> str:
        """站点代码（US/DE…）或 marketplaceId → marketplaceId。

        传空则用 `SPAPI_MARKETPLACE_ID`。不认识的代码直接报错，
        不静默回落到美国站——那会让欧洲站的卖家拿到一份美国站的空数据。
        """
        code = (marketplace or "").strip().upper()
        if not code:
            return self._default_marketplace_id
        if code in MARKETPLACE_IDS:
            return MARKETPLACE_IDS[code]
        if _MARKETPLACE_ID_RE.match(code):
            return code
        raise ValueError(
            f"不认识的 marketplace={marketplace!r}。可用站点代码：{sorted(MARKETPLACE_IDS)}；"
            f"也可以直接传 marketplaceId（如 ATVPDKIKX0DER）。"
        )

    # ------------------------------------------------------------
    # LWA token
    # ------------------------------------------------------------

    def invalidate_token(self) -> None:
        """丢掉缓存的 token（收到 401 时调用，下次请求会重新换）。"""
        self._access_token = None
        self._token_expires_at = 0.0

    async def _access_token_value(self, client: httpx.AsyncClient) -> str:
        """取 access token，命中缓存就直接用，过期前 2 分钟自动换新。

        失败抛 `SpApiAuthError`，不返回 None：没有 token 就没有任何一段能跑，
        这时候返回空快照等于把"配置错了"伪装成"没有数据"。
        """
        if self._access_token and time.monotonic() < self._token_expires_at:
            return self._access_token

        try:
            resp = await client.post(
                LWA_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"accept": "application/json"},
            )
        except Exception as exc:  # noqa: BLE001 —— 网络层异常统一包成可读信息
            raise SpApiAuthError(
                f"换取 LWA token 失败（网络异常）：{type(exc).__name__}: {exc}。"
                f"检查出口网络/代理，以及 SPAPI_REGION 是否与站点匹配。"
            ) from exc

        if resp.status_code != 200:
            raise SpApiAuthError(
                f"换取 LWA token 失败：HTTP {resp.status_code}，响应：{_snippet(resp)}。"
                f"常见原因：SPAPI_REFRESH_TOKEN 已失效（重新授权即可）、"
                f"SPAPI_CLIENT_ID/SECRET 与应用不匹配。"
            )

        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise SpApiAuthError(f"换取 LWA token 返回非 JSON：{_snippet(resp)}") from exc

        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not token:
            keys = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
            raise SpApiAuthError(
                f"LWA 响应里没有 access_token（拿到 {keys}）。检查 refresh token 是否已被撤销。"
            )

        try:
            expires_in = int(payload.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600

        self._access_token = str(token)
        self._token_expires_at = time.monotonic() + max(
            expires_in - TOKEN_REFRESH_MARGIN_SECONDS, 30
        )
        return self._access_token

    # ------------------------------------------------------------
    # 单次请求（业务失败只记错误，不抛异常）
    # ------------------------------------------------------------

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        label: str,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> tuple[Optional[dict], Optional[str]]:
        """请求一个 SP-API 端点。返回 `(payload_dict, error)`。

        401 会换一次 token 重试：token 可能在两次调用之间被服务端提前失效，
        重试一次能消掉这类偶发失败，又不会掩盖真正的权限问题。
        """
        url = f"{self.host}{path}"
        last_error: Optional[str] = None

        for attempt in (0, 1):
            token = await self._access_token_value(client)   # 失败直接抛 SpApiAuthError
            headers = {"x-amz-access-token": token, "accept": "application/json"}
            try:
                resp = await client.request(
                    method, url, params=params, json=json_body, headers=headers
                )
            except Exception as exc:  # noqa: BLE001
                return None, f"{label} 网络异常: {type(exc).__name__}: {exc}"

            if resp.status_code == 401 and attempt == 0:
                logger.info("SP-API %s 返回 401，换 token 重试一次", label)
                self.invalidate_token()
                last_error = f"{label} HTTP 401：换新 token 后仍然鉴权失败（检查应用角色权限）"
                continue

            if resp.status_code != 200:
                return None, f"{label} HTTP {resp.status_code}: {_snippet(resp)}"

            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                return None, f"{label} 返回非 JSON: {_snippet(resp)}"

            if not isinstance(data, dict):
                return None, f"{label} 返回结构不是对象: {_snippet(resp)}"

            # SP-API 的业务错误包在 {"errors": [...]} 里，HTTP 可能还是 200。
            errors = data.get("errors")
            if errors:
                return None, f"{label} 业务错误: {_summarize_errors(errors)}"

            return data, None

        return None, last_error or f"{label} 未知失败"

    # ------------------------------------------------------------
    # 各分段
    # ------------------------------------------------------------

    async def _get_seller_id(
        self, client: httpx.AsyncClient, marketplace_id: str
    ) -> tuple[Optional[str], Optional[str]]:
        """Sellers API：取本店 sellerId（Listings Items 的路径参数要用）。"""
        if self._seller_id:
            return self._seller_id, None

        data, err = await self._request(
            client,
            "GET",
            "/sellers/v1/marketplaceParticipations",
            label="sellers/marketplaceParticipations",
        )
        if err or data is None:
            return None, err or "sellers/marketplaceParticipations 无返回"

        participations = data.get("payload") or []
        if not isinstance(participations, list) or not participations:
            return None, "sellers/marketplaceParticipations 返回空列表（应用可能没有卖家角色）"

        # 优先取与目标站点一致的那条；没有就取第一条。
        chosen: Optional[dict] = None
        for item in participations:
            if not isinstance(item, dict):
                continue
            if ((item.get("marketplace") or {}).get("id")) == marketplace_id:
                chosen = item
                break
        if chosen is None and isinstance(participations[0], dict):
            chosen = participations[0]

        seller_id = ((chosen or {}).get("participation") or {}).get("sellerId")
        if not seller_id:
            return None, "marketplaceParticipations 里没有 participation.sellerId"

        self._seller_id = str(seller_id)
        return self._seller_id, None

    async def _get_sku(
        self, client: httpx.AsyncClient, seller_id: str, asin: str, marketplace_id: str
    ) -> tuple[Optional[str], Optional[str]]:
        """Listings Items：ASIN → 本店 SKU。

        为什么非要这道工序：FBA Inventory 只支持按 `sellerSkus` 查，
        没有「按 ASIN 查库存」的入口。拿不到 SKU，库存那一段就整段跑不了。
        """
        data, err = await self._request(
            client,
            "GET",
            f"/listings/2021-08-01/items/{seller_id}",
            label="listings/searchListingsItems",
            params={
                "marketplaceIds": marketplace_id,
                "identifiers": asin,
                "identifiersType": "ASIN",
                "pageSize": 1,
            },
        )
        if err or data is None:
            return None, err or "listings/searchListingsItems 无返回"

        for item in data.get("items") or []:
            if isinstance(item, dict) and item.get("sku"):
                return str(item["sku"]), None
        return None, f"listings 里没有 ASIN={asin} 对应的 SKU（该 ASIN 可能不是本店在售）"

    async def _get_catalog(
        self, client: httpx.AsyncClient, asin: str, marketplace_id: str
    ) -> tuple[Optional[dict], Optional[str]]:
        """Catalog Items：标题 / 品牌 / 图片 / 分类节点 / BSR。"""
        return await self._request(
            client,
            "GET",
            f"/catalog/2022-04-01/items/{asin}",
            label="catalog/getCatalogItem",
            params={
                "marketplaceIds": marketplace_id,
                "includedData": "summaries,images,classifications,salesRanks",
            },
        )

    async def _get_pricing(
        self, client: httpx.AsyncClient, asin: str, marketplace_id: str
    ) -> tuple[Optional[dict], Optional[str]]:
        """Pricing：BuyBox 价 + 本店标价。

        只查 New 品相（自家在售的通常是新品）。该接口限流 0.5 次/秒，
        一次 `fetch_self_snapshot` 只打一次。
        """
        return await self._request(
            client,
            "GET",
            f"/products/pricing/v0/items/{asin}/offers",
            label="pricing/getItemOffers",
            params={"MarketplaceId": marketplace_id, "ItemCondition": "New"},
        )

    async def _get_sales_window(
        self,
        client: httpx.AsyncClient,
        asin: str,
        marketplace_id: str,
        window_name: str,
        days: int,
        now: datetime,
    ) -> tuple[str, Optional[dict], Optional[str]]:
        """单个时间窗的下单量汇总（granularity=Total，服务端汇总成一条）。"""
        start = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        data, err = await self._request(
            client,
            "GET",
            "/sales/v1/orderMetrics",
            label=f"sales/getOrderMetrics[{window_name}]",
            params={
                "marketplaceIds": marketplace_id,
                "interval": f"{start}--{end}",
                "granularity": "Total",
                "asin": asin,
            },
        )
        return window_name, data, err

    async def _get_sales(
        self, client: httpx.AsyncClient, asin: str, marketplace_id: str
    ) -> tuple[dict[str, Optional[dict]], dict[str, Optional[str]]]:
        """Sales：近 7 日 / 近 30 日的下单件数合计。两个窗口互不影响。

        用 `granularity=Total` 让服务端把整个区间汇总成一条，
        这样就不用自己按 `interval` 字符串切日期、猜时区边界——
        那是「差一天」这类错误最爱出现的地方。
        """
        now = datetime.now(timezone.utc)
        windows = (("7d", 7), ("30d", 30))
        results = await asyncio.gather(
            *(self._get_sales_window(client, asin, marketplace_id, n, d, now) for n, d in windows)
        )
        payloads: dict[str, Optional[dict]] = {}
        errors: dict[str, Optional[str]] = {}
        for name, data, err in results:
            payloads[name] = data
            errors[name] = err
        return payloads, errors

    async def _get_inventory(
        self, client: httpx.AsyncClient, sku: str, marketplace_id: str
    ) -> tuple[Optional[dict], Optional[str]]:
        """FBA Inventory：可售 / 在途 / 预留件数。

        `details=true` 是必须的——不带它，响应里就没有 `inventoryDetails`，
        只剩一个 `totalQuantity`，那就拿不到「可售」这个真正要的数。
        """
        return await self._request(
            client,
            "GET",
            "/fba/inventory/v1/summaries",
            label="fba-inventory/getInventorySummaries",
            params={
                "details": "true",
                "granularityType": "Marketplace",
                "granularityId": marketplace_id,
                "marketplaceIds": marketplace_id,
                "sellerSkus": sku,
            },
        )

    async def _get_fees(
        self, client: httpx.AsyncClient, asin: str, marketplace_id: str, listing_price: float
    ) -> tuple[Optional[dict], Optional[str]]:
        """Product Fees：按给定报价估算佣金与 FBA 配送费。

        这个接口要**输入一个价格**才能算费用，所以它是「假设在这个价上卖，
        平台会抽多少」，不是成交订单的实收。`provenance` 因此标 `derived`。
        """
        body = {
            "FeesEstimateRequest": {
                "MarketplaceId": marketplace_id,
                "IsAmazonFulfilled": True,
                "Identifier": f"{asin}-{int(time.time())}",
                "PriceToEstimateFees": {"ListingPrice": {"Amount": float(listing_price)}},
            }
        }
        return await self._request(
            client,
            "POST",
            f"/products/fees/v0/items/{asin}/feesEstimate",
            label="product-fees/getMyFeesEstimateForASIN",
            json_body=body,
        )

    # ------------------------------------------------------------
    # 对外主入口
    # ------------------------------------------------------------

    async def fetch_self_snapshot(self, asin: str, marketplace: str = "US") -> SelfSnapshot:
        """抓一个自家 ASIN 的全维度快照。

        - 凭据缺失 → 抛 `SpApiNotConfigured`（带补法），不返回空对象；
        - 换 token 失败 → 抛 `SpApiAuthError`；
        - 某一段接口失败 → 该段字段留 None + provenance=error，其余段照常返回。
        """
        self._ensure_configured()

        clean_asin = (asin or "").strip().upper()
        if not clean_asin:
            raise ValueError("asin 不能为空")
        marketplace_id = self._resolve_marketplace_id(marketplace)

        snap = SelfSnapshot(asin=clean_asin, marketplace=(marketplace or "US").upper())

        # 站点与 region 自相矛盾时先记一条：这种配置错误的表现是 403/404，
        # 不提前说清楚，看日志的人会以为是权限没开。
        expected_region = _MARKETPLACE_REGION.get(marketplace_id)
        if expected_region and expected_region != self.region:
            snap.errors.append(
                f"配置可能不匹配：marketplaceId={marketplace_id} 属于 region={expected_region}，"
                f"但 SPAPI_REGION={self.region}。请求大概率返回 403/404。"
            )

        client, owned = self._open_client()
        try:
            # ---- 第一段：目录 / 定价 / 销量 / 卖家身份 ----
            catalog_t, pricing_t, sales_t, seller_t = await asyncio.gather(
                self._get_catalog(client, clean_asin, marketplace_id),
                self._get_pricing(client, clean_asin, marketplace_id),
                self._get_sales(client, clean_asin, marketplace_id),
                self._get_seller_id(client, marketplace_id),
            )
            catalog, catalog_err = catalog_t
            pricing, pricing_err = pricing_t
            sales_payloads, sales_errors = sales_t
            seller_id, seller_err = seller_t

            # ---- 第二段：SKU（库存的前提）----
            if seller_id:
                sku, sku_err = await self._get_sku(
                    client, seller_id, clean_asin, marketplace_id
                )
            else:
                sku = None
                sku_err = f"没有 sellerId，Listings 无法查询：{seller_err}"

            # ---- 第三段：库存（要 SKU）与费用（要报价），两路并发 ----
            listing_price = _pick_my_listing_price(pricing) if pricing is not None else None

            inventory_coro = (
                self._get_inventory(client, sku, marketplace_id)
                if sku
                else _not_attempted(f"FBA Inventory 需要 sellerSku，但 SKU 没解析出来：{sku_err}")
            )
            fees_coro = (
                self._get_fees(client, clean_asin, marketplace_id, listing_price)
                if listing_price is not None
                else _not_attempted(
                    "feesEstimate 必须输入报价（PriceToEstimateFees），"
                    "本轮本店标价未知，费用整段跳过"
                )
            )
            inventory_t, fees_t = await asyncio.gather(inventory_coro, fees_coro)
            inventory, inventory_err = inventory_t
            fees, fees_err = fees_t
        finally:
            if owned:
                await client.aclose()

        # ---- 映射：每段独立，失败不影响其他段 ----
        if sku:
            snap.sku = sku
            self._mark(snap, _SKU_FIELDS, PROVENANCE_SPAPI)
        else:
            self._fail(snap, _SKU_FIELDS, sku_err or "SKU 未解析出来")

        if catalog is not None:
            self._apply_catalog(snap, catalog, marketplace_id)
        else:
            self._fail(snap, _CATALOG_FIELDS, catalog_err or "catalog 无返回")

        if pricing is not None:
            self._apply_pricing(snap, pricing, seller_id)
        else:
            self._fail(snap, _PRICING_FIELDS, pricing_err or "pricing 无返回")

        self._apply_sales(snap, sales_payloads, sales_errors)

        if inventory is not None:
            self._apply_inventory(snap, inventory, clean_asin, sku)
        else:
            self._fail(snap, _INVENTORY_FIELDS, inventory_err or "inventory 无返回")

        if fees is not None:
            self._apply_fees(snap, fees)
        else:
            self._fail(snap, _FEES_FIELDS, fees_err or "fees 无返回")

        return snap

    async def health(self) -> dict:
        """不联网的体检：只看配置，不打接口（省得把限流额度花在健康检查上）。"""
        return {
            "adapter": self.name,
            "configured": self.configured,
            "missing": self.missing_credentials(),
            "region": self.region,
            "host": REGION_HOSTS.get(self.region, "unknown"),
            "marketplace_id": self._default_marketplace_id,
        }

    # ------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------

    def _open_client(self) -> tuple[httpx.AsyncClient, bool]:
        """返回 (client, 是否由本方法创建)。注入的 client 不负责关闭。"""
        if self._client is not None:
            return self._client, False
        return httpx.AsyncClient(timeout=self._timeout), True

    @staticmethod
    def _mark(snap: SelfSnapshot, fields: tuple[str, ...], provenance: str) -> None:
        """把一组字段的 provenance 标成同一个值。"""
        for name in fields:
            snap.provenance[name] = provenance

    def _fail(self, snap: SelfSnapshot, fields: tuple[str, ...], reason: str) -> None:
        """记一段失败：provenance=error（不是 unavailable）+ 原因进 errors。"""
        self._mark(snap, fields, PROVENANCE_ERROR)
        snap.errors.append(reason)

    # ---- 目录 ----

    def _apply_catalog(self, snap: SelfSnapshot, data: dict, marketplace_id: str) -> None:
        """Catalog Items → 标题/品牌/图片/类目路径/BSR。

        字段名全部按 catalogItems_2022-04-01 的 `Item` 结构：
        顶层是 `summaries` / `images` / `classifications` / `salesRanks`（不在 items[] 里）。
        """
        self._mark(snap, _CATALOG_FIELDS, PROVENANCE_SPAPI)

        summary = _pick_marketplace_entry(data.get("summaries"), marketplace_id)
        if summary:
            snap.title = _clean_str(summary.get("itemName"))
            snap.brand = _clean_str(summary.get("brand"))

        # 图片：优先 MAIN 变体，否则取第一张
        for group in data.get("images") or []:
            if not isinstance(group, dict):
                continue
            images = [img for img in (group.get("images") or []) if isinstance(img, dict)]
            if not images:
                continue
            main = next((i for i in images if (i.get("variant") or "").upper() == "MAIN"), images[0])
            link = _clean_str(main.get("link"))
            if link:
                snap.image_url = link
                break

        # 类目：Catalog 返回的是「被归类到的浏览节点名」，不保证是根→叶的完整链
        names: list[str] = []
        for group in data.get("classifications") or []:
            if not isinstance(group, dict):
                continue
            for node in group.get("classifications") or []:
                if isinstance(node, dict):
                    display = _clean_str(node.get("displayName"))
                    if display:
                        names.append(display)
        snap.category_path = names

        # BSR：取第一个排名组的 rank
        for group in data.get("salesRanks") or []:
            if not isinstance(group, dict):
                continue
            ranks = [r for r in (group.get("displayGroupRanks") or []) if isinstance(r, dict)]
            if not ranks:
                continue
            snap.bsr_rank = _to_int(ranks[0].get("rank"))
            if snap.bsr_rank is not None:
                break

    # ---- 定价 ----

    def _apply_pricing(
        self, snap: SelfSnapshot, data: dict, seller_id: Optional[str]
    ) -> None:
        """Pricing → BuyBox 价（LandedPrice）与本店标价（MyOffer 的 ListingPrice）。

        这两个数**必须分开存**：本项目已经因为「拿标价顶替到手价」出过事故，
        `UnifiedSnapshot.price_source` 那条注释就是那次事故留下的。
        这里同理——BuyBox 价含运费、可能来自别人；标价是本店自己的挂牌价。
        """
        self._mark(snap, _PRICING_FIELDS, PROVENANCE_SPAPI)

        payload = data.get("payload") or {}
        summary = payload.get("Summary") or {}

        buybox = _first_dict(summary.get("BuyBoxPrices"))
        if buybox is not None:
            snap.buybox_price = _money(buybox.get("LandedPrice"))
            bb_seller = _clean_str(buybox.get("sellerId"))
            if seller_id and bb_seller:
                snap.has_buybox = bb_seller == seller_id
            else:
                snap.has_buybox = None      # 无法判定，不猜
        else:
            # 响应里没有 BuyBoxPrices = 本 ASIN 当前没有购物车（合理状态，不是错误）
            snap.buybox_price = None
            snap.has_buybox = False

        my_offer = None
        for offer in payload.get("Offers") or []:
            if isinstance(offer, dict) and offer.get("MyOffer") is True:
                my_offer = offer
                break
        if my_offer is not None:
            snap.listing_price = _money(my_offer.get("ListingPrice"))
        else:
            # 接口成功但没有本店的 offer：可能是缺货/停售，也可能只是这次没返回。
            # 留空并说明，不拿 BuyBox 价去顶替。
            snap.errors.append(
                "pricing 返回里没有 MyOffer=true 的本店报价，listing_price 留空（不拿 BuyBox 价顶替）"
            )

    # ---- 销量 ----

    def _apply_sales(
        self,
        snap: SelfSnapshot,
        payloads: dict[str, Optional[dict]],
        errors: dict[str, Optional[str]],
    ) -> None:
        """orderMetrics → 日均下单件数。

        `unitCount` 是**下单件数**（ordered units），不是发货量，也不是净销量；
        退货要另外从退货报告里减。这里如实按「下单量」标注，不写成「销量」。
        """
        days = {"7d": 7, "30d": 30}
        field = {"7d": "daily_units_7d", "30d": "daily_units_30d"}
        derived_any = False

        for name, window_days in days.items():
            target = field[name]
            payload = payloads.get(name)
            err = errors.get(name)

            if payload is None:
                self._fail(snap, (target,), err or f"sales[{name}] 无返回")
                continue

            intervals = payload.get("payload")
            if not isinstance(intervals, list) or not intervals:
                # 空 payload 无法区分「真的零销量」和「没有数据」——不写 0，写 None。
                self._fail(
                    snap,
                    (target,),
                    f"sales[{name}] 返回空 payload，无法区分「零销量」与「无数据」，"
                    f"{target} 留空",
                )
                continue

            total_units = 0
            counted = 0
            for item in intervals:
                if not isinstance(item, dict):
                    continue
                units = _to_int(item.get("unitCount"))
                if units is not None:
                    total_units += units
                    counted += 1

            if counted == 0:
                self._fail(snap, (target,), f"sales[{name}] 返回的区间里没有 unitCount 字段")
                continue

            setattr(snap, target, round(total_units / window_days, 2))
            snap.provenance[target] = PROVENANCE_DERIVED
            derived_any = True

        if derived_any:
            logger.debug("SP-API %s 销量窗口映射完成：%s", snap.asin, snap.daily_units_30d)

    # ---- 库存 ----

    def _apply_inventory(
        self, snap: SelfSnapshot, data: dict, asin: str, sku: Optional[str]
    ) -> None:
        """FBA Inventory → 可售 / 在途合计 / 预留。

        响应路径是 `payload.inventorySummaries[]`（早期文档/记忆里常写成 `summaries`，
        两个都兜住，但以 `inventorySummaries` 为准）。
        """
        payload = data.get("payload") or {}
        rows = payload.get("inventorySummaries")
        if rows is None:
            rows = payload.get("summaries")
        rows = rows or []

        row = None
        for item in rows:
            if isinstance(item, dict) and sku and item.get("sellerSku") == sku:
                row = item
                break
        if row is None:
            for item in rows:
                if isinstance(item, dict) and item.get("asin") == asin:
                    row = item
                    break
        if row is None and rows and isinstance(rows[0], dict):
            row = rows[0]

        if row is None:
            self._fail(
                snap,
                _INVENTORY_FIELDS,
                f"fba-inventory 返回了 {len(rows)} 条汇总，但没有匹配 ASIN={asin}/SKU={sku} 的那条",
            )
            return

        details = row.get("inventoryDetails") or {}
        available = _to_int(details.get("fulfillableQuantity"))
        reserved = _to_int((details.get("reservedQuantity") or {}).get("totalReservedQuantity"))

        inbound_parts = [
            _to_int(details.get("inboundWorkingQuantity")),
            _to_int(details.get("inboundShippedQuantity")),
            _to_int(details.get("inboundReceivingQuantity")),
        ]
        inbound_known = [v for v in inbound_parts if v is not None]

        # 可售/预留是 direct mapping；在途是三项求和 → derived。
        snap.available_units = available
        snap.provenance["available_units"] = PROVENANCE_SPAPI
        snap.reserved_units = reserved
        snap.provenance["reserved_units"] = PROVENANCE_SPAPI

        if inbound_known:
            snap.inbound_units = sum(inbound_known)
            snap.provenance["inbound_units"] = PROVENANCE_DERIVED
        else:
            snap.inbound_units = None
            snap.provenance["inbound_units"] = PROVENANCE_SPAPI   # 三个字段都没有 = 接口没给

        if available is None:
            snap.errors.append(
                "fba-inventory 响应里没有 inventoryDetails.fulfillableQuantity"
                "（检查请求是否带了 details=true）"
            )

    # ---- 费用 ----

    def _apply_fees(self, snap: SelfSnapshot, data: dict) -> None:
        """Product Fees → 佣金 / FBA 单件配送费（估算值，标 derived）。"""
        result = (data.get("payload") or {}).get("FeesEstimateResult") or {}
        status = _clean_str(result.get("Status"))

        if status and status != "Success":
            err = result.get("Error") or {}
            self._fail(
                snap,
                _FEES_FIELDS,
                f"feesEstimate 返回 Status={status}"
                f"（{_clean_str(err.get('Code')) or '无 code'}: {_clean_str(err.get('Message')) or '无 message'}）",
            )
            return

        estimate = result.get("FeesEstimate") or {}
        details = estimate.get("FeeDetailList") or []
        if not details:
            self._fail(
                snap,
                _FEES_FIELDS,
                "feesEstimate 返回成功但没有 FeeDetailList，费用留空",
            )
            return

        seen: set[str] = set()
        for detail in details:
            if not isinstance(detail, dict):
                continue
            fee_type = _clean_str(detail.get("FeeType")) or ""
            amount = _money(detail.get("FeeAmount"))
            if fee_type in _REFERRAL_FEE_TYPES and snap.referral_fee is None:
                snap.referral_fee = amount
                snap.provenance["referral_fee"] = PROVENANCE_DERIVED
                seen.add("referral_fee")
            elif fee_type in _FBA_FEE_TYPES and snap.fba_fee is None:
                snap.fba_fee = amount
                snap.provenance["fba_fee"] = PROVENANCE_DERIVED
                seen.add("fba_fee")

        # 没见到的那个字段：接口这次没给 → error（不是 unavailable，它本可以给）
        for name in _FEES_FIELDS:
            if name not in seen:
                snap.provenance[name] = PROVENANCE_ERROR
        missing = [n for n in _FEES_FIELDS if n not in seen]
        if missing:
            snap.errors.append(
                f"feesEstimate 的 FeeDetailList 里没有 {missing} 对应的 FeeType"
                f"（实际返回的 FeeType：{sorted(_clean_str(d.get('FeeType')) or '?' for d in details if isinstance(d, dict))}）"
            )


# ------------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------------


async def _not_attempted(message: str) -> tuple[None, str]:
    """「这段缺前置条件，压根没发请求」的统一返回。

    做成协程而不是直接塞一个 `(None, msg)` 元组，是为了让 `asyncio.gather`
    的两个分支形态一致，同时让这个"跳过"在代码里显式可见——
    跳过的原因会原样进 `errors`，不会被静默吞掉。
    """
    return None, message


def _snippet(resp: httpx.Response, limit: int = 300) -> str:
    """响应体摘录，用于错误消息。截断避免把整页 HTML 灌进日志。"""
    try:
        text = resp.text or ""
    except Exception:  # noqa: BLE001
        return "<无法读取响应体>"
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _summarize_errors(errors: Any) -> str:
    """把 SP-API 的 `errors` 数组压成一行可读文本。"""
    if not isinstance(errors, list):
        return str(errors)[:300]
    parts: list[str] = []
    for err in errors:
        if isinstance(err, dict):
            code = err.get("code") or "?"
            message = " ".join(str(err.get("message") or "").split())
            parts.append(f"{code}: {message}" if message else str(code))
        else:
            parts.append(str(err))
    return "; ".join(parts)[:500]


def _first_dict(items: Any) -> Optional[dict]:
    """取列表里第一个 dict 元素；没有就 None。"""
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                return item
    return None


def _pick_marketplace_entry(items: Any, marketplace_id: str) -> Optional[dict]:
    """在 `summaries[]` / `images[]` 这类带 marketplaceId 的列表里挑本站那条。"""
    if not isinstance(items, list):
        return None
    fallback: Optional[dict] = None
    for item in items:
        if not isinstance(item, dict):
            continue
        if fallback is None:
            fallback = item
        if item.get("marketplaceId") == marketplace_id:
            return item
    return fallback


def _pick_my_listing_price(pricing: Optional[dict]) -> Optional[float]:
    """从 Pricing 响应里摘出本店标价（MyOffer=true 的 ListingPrice）。

    单独提出来，是因为费用估算要靠它做输入，而它和 BuyBox 价是两个数。
    与 `_apply_pricing` 同一规则：拿不到就是 None，绝不拿 BuyBox 价顶替。
    """
    if not isinstance(pricing, dict):
        return None
    payload = pricing.get("payload") or {}
    for offer in payload.get("Offers") or []:
        if isinstance(offer, dict) and offer.get("MyOffer") is True:
            return _money(offer.get("ListingPrice"))
    return None


def _money(value: Any) -> Optional[float]:
    """`{"Amount": 12.34, "CurrencyCode": "USD"}` → 12.34。兼容字符串金额。"""
    if isinstance(value, dict):
        return _to_float(value.get("Amount"))
    return _to_float(value)


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


def _clean_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    text = str(v).strip()
    return text or None


__all__ = [
    "SpApiAdapter",
    "SpApiError",
    "SpApiAuthError",
    "SpApiNotConfigured",
    "REGION_HOSTS",
    "MARKETPLACE_IDS",
    "LWA_TOKEN_URL",
]
