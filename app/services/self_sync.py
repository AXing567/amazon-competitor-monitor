"""自家锚点同步：SP-API → `MyProduct`。

## 这个模块补的是「适配器写完了但没人调」

`app/adapters/spapi.py` 有一千多行，逐字段 provenance、分段失败、凭据错误
都写得很细——然后全项目没有一处 import 它。于是「SP-API 已经适配好了」
这句话在代码里是真的，在产品里是假的：自家锚点仍然靠卖家精灵反推填。

## 只写 SP-API 真能给的字段

这张映射表是本模块最重要的东西，写错的代价是「看起来像后台数据」的假数据：

| 锚点字段 | 取自 | 说明 |
|---|---|---|
| `fba_available_units` | `available_units` | FBA 可售（不含在途） |
| `daily_sales_avg` | `daily_units_30d` | 近 30 日日均**下单**件数，订单级真值 |
| `current_price` | `buybox_price` | 购物车价。**不用 `listing_price` 顶替** |
| `title` / `image_url` | 同名 | Catalog Items |
| `category_path` | `category_path` | Catalog 的归类节点，不保证是完整链 |

刻意**不写**的四个：

- `sub_category_rank`：SP-API 给的是 `displayGroupRanks` 里的**第一个排名组**，
  通常是大类。锚点这一列要的是子类 BSR——写进去以后它会和竞品的子类排名
  摆在同一列里比较，而两者根本不是一回事。返回里会说明「拿到了大类排名 N，没写入」。
- `rating` / `review_count`：SP-API 结构上不提供（见 `FIELDS_NEVER_FROM_SPAPI`），
  只能手工填或从商品页看。
- `break_even_price` / `target_margin_rate` / `lead_time_days` / `core_keywords`：
  卖家自己的账，任何接口都给不出。

## `current_price` 为什么不回落到 `listing_price`

这个项目已经因为「拿标价顶替到手价」出过事故。标价 $24.99 挂 10% 券，
到手是 $22.49——跟价判断比的是到手价，用标价去比会得出「对手比我便宜」的
反向结论。所以：只有 BuyBox 价才写；只有标价时**不写**，返回一条 note
让人自己确认。宁可空着，也不给一个语义错误的数。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select

from app.adapters.spapi import (
    SpApiAdapter,
    SpApiAuthError,
    SpApiNotConfigured,
    env_name,
)
from app.config import settings
from app.db import get_session
from app.domain import baseline as baseline_mod
from app.models import MyProduct

logger = logging.getLogger(__name__)

#: 锚点字段 ← 自家快照字段。顺序即「优先写谁」，不含 `current_price`（单独处理）。
SIMPLE_FIELD_MAP: tuple[tuple[str, str], ...] = (
    ("fba_available_units", "available_units"),
    ("daily_sales_avg", "daily_units_30d"),
    ("title", "title"),
    ("image_url", "image_url"),
    ("category_path", "category_path"),
)

#: 这些字段 SP-API 结构上给不了，同步时保持原值并说明原因。
KEPT_MANUAL: dict[str, str] = {
    "rating": "SP-API 不提供星级，只能在商品页看（或手工填）",
    "review_count": "SP-API 不提供评价数，只能在商品页看（或手工填）",
    "break_even_price": "成本账，接口给不出",
    "target_margin_rate": "经营目标，接口给不出",
    "lead_time_days": "采购与头程时效，接口给不出",
    "core_keywords": "要广告搜索词报告，SP-API 这一组接口里没有",
    "sub_category_rank": "SP-API 的排名组通常是大类，写进子类排名会混淆口径，故不自动写",
}


def status() -> dict:
    """SP-API 配置状态。面板上「能不能同步」全看这个，不猜。"""
    adapter = SpApiAdapter()
    missing = adapter.missing_credentials()
    out: dict = {
        "configured": not missing,
        "missing": missing,
        # 缺的是哪几个**环境变量**。只说 "refresh_token" 没人知道去哪填。
        "missing_env": [env_name(m) for m in missing],
        "region": adapter.region,
        "hint": "",
    }
    if missing:
        # 补法那段文案由异常自己生成（含每条凭据去哪拿），不在这里重写一遍：
        # 两处各写一次，改了一处另一处就开始骗人。
        out["hint"] = str(SpApiNotConfigured(missing))
    return out


def sync(
    tenant_id: Optional[str] = None,
    *,
    asin: Optional[str] = None,
    marketplace: str = "US",
    dry_run: bool = False,
) -> dict:
    """同步一次。同步函数外壳，内部 `asyncio.run`（与 `pool.add_competitor` 一致）。

    `dry_run=True` 只取数、不落库，用来在面板上「先看看能拿到什么」。
    """
    try:
        return asyncio.run(
            _sync(
                tenant_id=tenant_id,
                asin=asin,
                marketplace=marketplace,
                dry_run=dry_run,
            )
        )
    except SpApiNotConfigured as exc:
        return _not_configured(exc)
    except SpApiAuthError as exc:
        # 凭据在，但换不出 token。这不是"暂时没数据"，配置问题要吵。
        return {
            "ok": False,
            "status": "auth_failed",
            "error": str(exc),
            "hint": "三项凭据填了但换不到 token：检查 refresh_token 是否已被重新授权覆盖、"
                    "应用是否还处于「草稿」状态、以及自营店铺是否点过授权。",
        }


def _not_configured(exc: Exception) -> dict:
    st = status()
    return {
        "ok": False,
        "status": "not_configured",
        "error": "SP-API 还没配置，同步不了。",
        "missing": st["missing"],
        "missing_env": st["missing_env"],
        "hint": st["hint"],
    }


async def _sync(
    *,
    tenant_id: Optional[str],
    asin: Optional[str],
    marketplace: str,
    dry_run: bool,
) -> dict:
    tenant_id = tenant_id or settings.tenant_id

    # 凭据先查：没配置的时候连库都不用碰，报错也更早更准。
    adapter = SpApiAdapter()
    if not adapter.configured:
        return _not_configured(SpApiNotConfigured(adapter.missing_credentials()))

    with get_session() as s:
        my = s.execute(
            select(MyProduct).where(MyProduct.tenant_id == tenant_id).limit(1)
        ).scalar_one_or_none()
        if my is None:
            return {
                "ok": False,
                "status": "no_anchor",
                "error": "还没建自家锚点：先在面板上填 ASIN 与当前售价，再同步。",
                "hint": "同步是「往已有锚点上补数」，不是「凭空造一个锚点」。",
            }
        target_asin = (asin or my.asin or "").strip().upper()
        my_id = int(my.id)
        before = {
            "current_price": my.current_price,
            "fba_available_units": my.fba_available_units,
            "daily_sales_avg": my.daily_sales_avg,
        }

    if not target_asin:
        return {
            "ok": False,
            "status": "no_asin",
            "error": "锚点没有 ASIN，SP-API 不知道同步哪个商品。",
        }

    snap = await adapter.fetch_self_snapshot(target_asin, marketplace)

    written: dict[str, Any] = {}
    skipped: list[dict] = []
    notes: list[str] = []

    # ---- 简单映射 ----
    for target, src in SIMPLE_FIELD_MAP:
        value = getattr(snap, src, None)
        if value in (None, "", [], {}):
            skipped.append(
                {
                    "field": target,
                    "reason": _skip_reason(snap, src, target),
                }
            )
            continue
        written[target] = value

    # ---- 价格：只认购物车价 ----
    if snap.buybox_price is not None:
        written["current_price"] = snap.buybox_price
        if snap.listing_price is not None and abs(snap.buybox_price - snap.listing_price) > 0.01:
            notes.append(
                f"标价 ${snap.listing_price:.2f}，购物车价 ${snap.buybox_price:.2f}："
                "锚点写的是购物车价（到手口径），券后价差已体现。"
            )
    elif snap.listing_price is not None:
        skipped.append(
            {
                "field": "current_price",
                "reason": (
                    f"只拿到标价 ${snap.listing_price:.2f}，没有购物车价。"
                    "标价和到手价不是一回事（挂券时差别很大），所以没有写入——"
                    "请确认后手工填。"
                ),
            }
        )
    else:
        skipped.append({"field": "current_price", "reason": "Pricing 段没返回本店报价"})

    # ---- 排名：拿到了也不写，只说一声 ----
    if snap.bsr_rank is not None:
        notes.append(
            f"SP-API 返回的大类排名是 {snap.bsr_rank}，**没有**写进子类 BSR："
            "两者口径不同，混在一列里会和竞品的子类排名直接比较。要的话请手工核对后填。"
        )
    for field, reason in KEPT_MANUAL.items():
        if field == "sub_category_rank":
            continue
        skipped.append({"field": field, "reason": reason})

    # ---- 分段失败 ----
    errors = list(snap.errors)

    if dry_run:
        return {
            "ok": True,
            "status": "dry_run",
            "asin": target_asin,
            "written": written,
            "skipped": skipped,
            "notes": notes,
            "errors": errors,
            "data_quality": snap.data_quality,
            "captured_at": snap.captured_at.isoformat(timespec="seconds"),
            "hint": "这是预览：什么都没写库。确认无误后再点一次「同步」。",
        }

    if not written:
        return {
            "ok": False,
            "status": "nothing_written",
            "asin": target_asin,
            "skipped": skipped,
            "notes": notes,
            "errors": errors or ["SP-API 这次一个字段都没返回"],
            "hint": "一个字段都没拿到时不要覆盖库里已有的值——"
                    "把「接口挂了」写成「锚点为空」是最难查的一类事故。",
        }

    # ---- 落库 ----
    with get_session() as s:
        my = s.get(MyProduct, my_id)
        changed: list[dict] = []
        for field, value in written.items():
            old = getattr(my, field, None)
            if old != value:
                changed.append({"field": field, "before": old, "after": value})
            setattr(my, field, value)

        # 逐格标出身。同步覆盖掉的字段一律改成 sp_api——
        # 手填的旧值已经被真值替掉，来源要是还留着 `manual`，
        # 下次没人知道这个数其实已经能和后台对上了。
        sources = dict(my.field_sources or {})
        for field in written:
            if field in baseline_mod.FIELD_ORIGINS:
                sources[field] = baseline_mod.ORIGIN_SP_API
        my.field_sources = sources

        # SP-API 写进去的字段不再是假设值
        assumed = baseline_mod.read_assumed_fields(my.assumed_fields)
        for field in written:
            if field in assumed:
                assumed.remove(field)
        my.assumed_fields = assumed

        # 整行来源：只在**真实模式**下改成 sp_api。
        # Mock 库里的那套演示锚点必须保持 sellersprite_proxy，
        # 否则它会摇身一变成为"SP-API 同步来的真实数据"。
        from app.db import DATA_MODE_LIVE, get_data_mode

        if get_data_mode() == DATA_MODE_LIVE:
            my.source = baseline_mod.SOURCE_SP_API
        my.updated_at = datetime.now()

    return {
        "ok": True,
        "status": "synced",
        "asin": target_asin,
        "written": written,
        "changed": changed,
        "skipped": skipped,
        "notes": notes,
        "errors": errors,
        "data_quality": snap.data_quality,
        "captured_at": snap.captured_at.isoformat(timespec="seconds"),
        "before": before,
    }


def _skip_reason(snap, src: str, target: str) -> str:
    """字段没拿到时，说清是「接口给不了」「这轮失败」还是「本来就没有」。"""
    prov = snap.provenance_of(src)
    if prov == "unavailable":
        reason = snap.unavailable_reason(src)
        return reason or "SP-API 结构上不提供这个字段"
    if prov == "error":
        return "这一段接口这轮失败了（不是没有数据，见 errors）"
    return "接口这次没返回值"
