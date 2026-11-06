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
from app.db import DATA_MODE_LIVE, get_data_mode, get_session
from app.domain import baseline as baseline_mod
from app.domain.tiering import judge_tier
from app.models import Competitor, MyProduct, Signal, Snapshot
from app.services import settings_store

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
    """拉自家 ASIN 的公开数据，补齐锚点。网络失败静默跳过。

    **真实模式下整段不跑。** 这一步填进去的是「别人家商品页上的公开数据」，
    自家真正的账（库存、销量、成本）它一个都给不了。以前它在真实模式下也跑，
    结果是一次"自动补全"就把自家锚点变得看起来齐全——而里面没有一个数来自后台。
    真实模式要的是 `self_sync`（SP-API），要不到就该空着，空着会如实说"判不了"。
    """
    if get_data_mode() == DATA_MODE_LIVE:
        return []

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
        # 这批数是反推来的，逐格标清。不标的话面板会把它显示成"已填"，
        # 而人看到"已填"就会以为它是自家的账。
        if filled:
            sources = dict(my.field_sources or {})
            for name in filled:
                key = name.split("(")[0]
                if key in baseline_mod.FIELD_ORIGINS:
                    sources[key] = baseline_mod.ORIGIN_SELLERSPRITE
            my.field_sources = sources
            my.source = baseline_mod.SOURCE_SELLERSPRITE
        return filled


def update_baseline(tenant_id: Optional[str], **fields) -> dict:
    """更新自家锚点。只接受白名单字段。

    `source` / `assumed_fields` 必须在这张白名单里：下面的循环对未声明的键
    **静默 `continue`**，所以漏掉任何一个，调用方传了也白传——
    而且不会有任何报错，只会在面板上看到"这个锚点看起来全是查来的"。
    """
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
        "bullet_points",
        "core_keywords",
        "category_path",
        # 溯源三列（见 models.MyProduct 第 6 节）
        "source",
        "assumed_fields",
        "field_sources",
    }

    # ---- 闸门：真实模式不接受卖家精灵反推 ----
    # 为什么拦在这里而不是只在页面上标红：写进来之后，下游每一个判断都会用它，
    # 而它们都不再知道自己用的是反推值。`_autofill()` 以前正是这么干的——
    # 一次"自动补全"就把自家锚点填满了别人家的公开数据。
    incoming_source = str(fields.get("source") or "").strip().lower()
    if get_data_mode() == DATA_MODE_LIVE:
        block = baseline_mod.live_block_reason(incoming_source)
        if block:
            return {
                "ok": False,
                "error": block,
                "error_kind": "source",
                "hint": "真实模式下自家数据只能来自 SP-API 同步或手工录入。"
                        "演示用的反推数据请切到 Mock 模式。",
            }

    # `assumed_fields` 的形状校验：这里再挡一道，是因为本函数还有能力层之外的
    # 两个调用方（`app/api/server.py`、`scripts/set_baseline.py`）。
    # 校验规则本身在 `domain.baseline`，与能力层共用同一份实现，消息不会分叉。
    normalized_assumed = None
    if fields.get("assumed_fields") is not None:
        try:
            normalized_assumed = baseline_mod.coerce_assumed_fields(
                fields["assumed_fields"]
            )
        except ValueError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "error_kind": "param",
                "hint": baseline_mod.ASSUMED_FIELDS_HINT,
            }

    updated: list[str] = []
    with get_session() as s:
        my = s.execute(
            select(MyProduct).where(MyProduct.tenant_id == tenant_id).limit(1)
        ).scalar_one_or_none()
        if my is None:
            # 必须带上本次传入的 asin：`my_products.asin` 是 NOT NULL，
            # 先建空行再 setattr 会在 flush 时撞 IntegrityError。
            # 这个 bug 只在"租户还没有自家产品"时出现（即在干净库上跑 demo/首次配置），
            # 有数据的库永远复现不出来——所以修在结构上，而不是加 try/except。
            my = MyProduct(
                tenant_id=tenant_id, asin=str(fields.get("asin") or ""), marketplace="US"
            )
            s.add(my)
            s.flush()

        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "assumed_fields":
                v = normalized_assumed if normalized_assumed is not None else []
            elif k == "source":
                v = str(v).strip().lower() or baseline_mod.SOURCE_MANUAL
            setattr(my, k, v)
            updated.append(k)

        # ---- 字段级出身：谁写的就标谁 ----
        # 走这条路的只有人（面板/API/脚本），所以本次写入的字段默认标 `manual`；
        # SP-API 同步走 `self_sync`，它自己会带 `field_sources`。
        # 为什么在这里自动标，而不是要求每个调用方自己拼：调用方有四个
        # （能力层、`api/server.py`、`scripts/set_baseline.py`、pilot 脚本），
        # 少写一个地方，那一批数就会显示成「来源未知」——比标错更糟。
        explicit = fields.get("field_sources")
        if isinstance(explicit, dict):
            sources = {
                str(k2): str(v2)
                for k2, v2 in explicit.items()
                if k2 in allowed or k2 in baseline_mod.FIELD_ORIGINS
            }
        else:
            sources = dict(my.field_sources or {})
            for k in updated:
                if k in baseline_mod.FIELD_ORIGINS:
                    sources[k] = baseline_mod.ORIGIN_MANUAL
        my.field_sources = sources

        # 填了售价与毛利率但没填保本价 → 自动推算一个，并明确标注是推算值
        inferred = None
        if "break_even_price" not in updated and my.current_price and my.target_margin_rate:
            inferred = baseline_mod.estimate_break_even_price(
                price=my.current_price, margin_rate=my.target_margin_rate
            )
            if inferred and not my.break_even_price:
                my.break_even_price = inferred
                updated.append("break_even_price(按毛利率推算)")
                # 推算出来的保本价**就是假设值**，必须自己承认。
                # `out["inferred"]` 只在这一刻存在，落库之后没人再记得它是算出来的；
                # 而保本价往下直接决定「该不该跟价」「要不要提价」这两个不能再错的动作。
                # 以前这里只是把结果塞进库里，锚点体检会把它当成实测值——正是本轮
                # 要消灭的「看着像证据的字段」。
                assumed = baseline_mod.read_assumed_fields(my.assumed_fields)
                if "break_even_price" not in assumed:
                    assumed.append("break_even_price")
                    my.assumed_fields = assumed
                    if "assumed_fields" not in updated:
                        updated.append("assumed_fields(自动标注：break_even_price)")

        out = _my_to_dict(my)
        # 成功也要带 `ok`：失败路径返回的是 `{"ok": False, "error": ...}`，
        # 若成功路径没有这个键，调用方写 `if not res.get("ok")` 会把**成功**当成失败，
        # 写 `if res.get("error")` 又得知道哪个键在哪种情况下存在。
        # 两条路径形状一致，判断才只有一种写法。
        out["ok"] = True
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
    user_id: Optional[int] = None,
) -> dict:
    """加一个竞品进池。会先拉数据跑四同法则，再决定进不进、进哪一级。

    `user_id` 是「谁加的 / 谁盯这个竞品」，只用于落 `competitor_bindings.user_id`，
    不参与任何判定（分层是产品属性，与个人无关）。允许为 `None`：那样下游
    `binding.upsert_from_judgment()` 会兜底到租户默认用户，与旧行为一致。
    """
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
        # 自家类目路径。以前这里是硬编码 None，导致 `node_match` 恒为 None、
        # `decidable_dims` 永远差一格，整池被标成「待判定」。
        # 现在从锚点上读；没配就还是 None（不猜），由 unknown 如实反映。
        my_path = list(my.category_path or []) or None

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
    # 阈值必须从配置读，否则「面板改了词重合门槛、实际入池还用老标准」——
    # 试算与生产不一致，正是最伤信任的那类 bug。
    # 只走 company + product 两层：分层是**产品属性**，不是个人偏好（与采集同理）。
    res = judge_tier(
        my_price=my_price,
        my_core_keywords=my_keywords,
        my_category_path=my_path,
        my_daily_units=my_daily,
        comp=snap,
        functional_match=functional_match,
        thresholds=settings_store.resolve_tiering_thresholds(tenant_id, my_id),
    )

    # 证据不足（能判的维度 < 2）时**不拒**：
    # 拿不到证据 ≠ 证据表明不是竞品。放进来标成 unknown（待判定），
    # 等数据补齐后由周维护正常改判；拒之门外则永远没有补数据的机会。
    if not res.passed and not force and not res.insufficient:
        return {
            "ok": False,
            "error": "未通过四同法则，不放进池子",
            "reject_reason": res.reject_reason,
            "price_ratio": res.price_ratio,
            "keyword_overlap": res.keyword_overlap,
            "node_match": res.node_match,
            "decidable_dims": res.decidable_dims,
            "hint": "如果你确信它是竞品，可以 force=true 强制加入，标记为观察级（C）",
            "snapshot": _brief_snapshot(snap),
        }

    # tier 取值 S/A/B/C/unknown。unknown = 待判定（缺数据），**不是不合格**。
    # 面板必须显示成「待判定」并说明缺什么，不许落到 C 那一档去显示。
    tier = res.effective_tier

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

    # 分层理由落库成绑定 basis（谁认为这个竞品和自家哪个产品同战场）。
    # 失败只记日志：绑定是增强，不该让「加竞品」这件事回滚。
    # `user_id` 只在这一处下传：绑定的归属人就是「谁加的」。
    # 传 None 时由 binding 层兜底成默认用户（旧行为），不会写出一条 user_id 为空的绑定。
    try:
        from app.services import binding as binding_mod
        binding_mod.upsert_from_judgment(tenant_id=tenant_id, competitor_id=comp_id,
                                         my_product_id=my_id, tier=tier,
                                         tier_reasons=res.tier_reasons,
                                         user_id=user_id)
    except Exception:  # noqa: BLE001
        logger.warning("写入对标绑定失败 competitor_id=%s", comp_id, exc_info=True)

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


def list_competitors(
    tenant_id: Optional[str],
    tier: Optional[str] = None,
    include_archived: bool = False,
) -> list[dict]:
    """池子清单。默认不含归档（归档 = 不再盯，日常看不见）。

    `include_archived=True` 是给「竞品池管理」那一屏用的：归档的人要能看见
    "它去哪了"并能恢复。默认关掉的原因同 `paused` 的注释——日常列表里堆着
    已经不盯的竞品，会让人误判"我还在盯这么多"。
    """
    tenant_id = tenant_id or settings.tenant_id
    with get_session() as s:
        stmt = select(Competitor).where(Competitor.tenant_id == tenant_id)
        if not include_archived:
            stmt = stmt.where(Competitor.status != "archived")
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
                    # 面板的「判定理由」要用：四同法则逐项结论 + 判定时间。
                    # 列表里带上，比让面板对 8 个竞品各调一次 get_competitor_detail（N+1）好。
                    "tier_reasons": c.tier_reasons,
                    "node_match": c.node_match,
                    "functional_match": c.functional_match,
                    "tier_updated_at": (
                        c.tier_updated_at.isoformat() if c.tier_updated_at else None
                    ),
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


# 「停」= paused：停止采集、保留历史、面板可见但灰显。
# 不用新代码去实现「不采集」——collect_all 只捞 status == "active"，
# 所以 paused 天然被跳过。这就是选 paused 而不是加一个 is_enabled 列的理由。
VALID_STATUSES = ("active", "paused", "archived")
_STATUS_EFFECT = {
    "active": "恢复采集", "paused": "已停止采集，历史保留", "archived": "已归档，不再出现在列表",
}


def set_competitor_status(tenant_id: Optional[str], asin: str, status: str) -> dict:
    """改竞品状态：active / paused / archived。"""
    tenant_id = tenant_id or settings.tenant_id
    status = (status or "").strip().lower()
    if status not in VALID_STATUSES:
        return {"ok": False, "error": f"status 只能是 {'/'.join(VALID_STATUSES)}，收到 {status!r}"}
    with get_session() as s:
        comp = s.execute(
            select(Competitor).where(
                Competitor.tenant_id == tenant_id, Competitor.asin == (asin or "").strip().upper()
            )
        ).scalar_one_or_none()
        if comp is None:
            return {"ok": False, "error": f"{asin} 不在池中"}
        comp.status = status
        return {"ok": True, "asin": comp.asin, "status": status, "effect": _STATUS_EFFECT[status]}


# ============================================================
# 工具
# ============================================================


def _my_to_dict(my: MyProduct) -> dict:
    a = baseline_mod.assess(my)
    return {
        "id": my.id,
        "asin": my.asin,
        # 溯源两列必须透出来：面板拿不到就没法区分「查来的」和「拍的」，
        # 这件事就退化成"只在库里存在"——下次有人只看面板，照样会把假设值当真值。
        # `source` 兜底成 manual：这一列上线前写的行是 NULL（迁移只 ADD COLUMN，
        # 不改写历史数据），而那些行确实既非反推也非 SP-API 同步。
        "source": my.source or baseline_mod.SOURCE_MANUAL,
        "assumed_fields": list(a.assumed_fields),
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
        # 路径本身也返回：只给个数的话，用户没法确认自己填对了没有。
        "category_path": list(my.category_path or []),
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
