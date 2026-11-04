"""L3 · 对标绑定：谁 + 哪个竞品 + 哪个自家产品。

## 核心洞察

四同法则算出的 `tier_reasons` 本来就是绑定的依据，以前只用于筛选、没落库。
所以这张表最关键的列不是三个外键，而是 `basis`：它存的是
**「当时为什么认为这两个是同战场」的判定快照**，而不是一个布尔结论。

## 为什么 basis 里必须存阈值版本号

半年后回头质疑「为什么当初把这个 ASIN 判成 S」，而阈值已经改过三版了。
没有当时的阈值，这个判断就无法复现——无法复现的结论等于没有结论。

## 为什么是三元组

同一个竞品，A 拿自家 A1 对标、B 拿自家 A2 对标（比如同时做两个价位段）。
二元组会把两人强行挤到一行，后写覆盖先写。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select

from app.config import settings
from app.db import get_session
from app.domain import tiering as tiering_mod
from app.domain.thresholds import GROUP_TIERING, TieringThresholds
from app.domain.tiering import DIM_ORDER
from app.models import Competitor, CompetitorBinding, MyProduct, User
from app.services import identity, settings_store

logger = logging.getLogger(__name__)

SOURCE_AUTO = "auto_tiering"
SOURCE_MANUAL = "manual"


# ============================================================
# 判定快照
# ============================================================


def build_basis(
    *,
    tier_reasons: Optional[dict],
    tier: str,
    thresholds_version: int = 0,
    threshold_layers: Optional[dict] = None,
    judged_at: Optional[datetime] = None,
) -> dict:
    """把分层理由 + 阈值版本 + 原始值打包成可复现的快照。"""
    reasons = dict(tier_reasons or {})
    return {
        "tier": tier,
        # 这五个键是「能否复现当初判断」的最小集合，删任何一个都会让半年后的追问无法回答
        "price_ratio": reasons.get("price_ratio"),
        "keyword_overlap": reasons.get("keyword_overlap"),
        "node_match": reasons.get("node_match"),
        "thresholds_version": int(thresholds_version or 0),
        "judged_at": (judged_at or datetime.now()).isoformat(timespec="seconds"),
        # 以下为附加信息：tier_reasons 原文 + 各层版本，便于一眼看出是哪一层动过
        "functional_match": reasons.get("functional_match"),
        "tier_reasons": reasons,
        "threshold_layers": dict(threshold_layers or {}),
    }


def build_basis_summary(
    tier_reasons: Optional[dict],
    tier: str = "C",
    thresholds: Optional[TieringThresholds] = None,
) -> str:
    """一句话人话理由。面板列表直接显示，不必每次解析 JSON。

    不允许返回空串或「无」：一个没有理由的结论等于没有结论，
    而这句话是老板在池子里扫一眼时唯一的决策依据。

    **暂定必须说出口**：`tier_reasons["provisional"]` 为真时，头部写成
    「暂定 B 级：」，且缺的那一维写成「词重合未测」——不许写成
    「加权词重合 0%」或「重合较低」，那两句话都会让人以为我们评估过。
    """
    th = thresholds or TieringThresholds()
    reasons = dict(tier_reasons or {})
    provisional = bool(reasons.get("provisional"))
    black_horse = bool(reasons.get("black_horse"))

    parts: list[str] = []

    ratio = reasons.get("price_ratio")
    if ratio is None:
        parts.append("价格数据缺失，价格带无法核对")
    else:
        # 带内用两位小数就够（方向本来就一致）；带外必须让方向看得出来，
        # 否则 $15.99 那种会写成「价格比 0.70 超出 0.7~1.4 价格带」。见 `fmt_ratio_to_show_side`。
        lo, hi = th.price_ratio_min, th.price_ratio_max
        if reasons.get("price_band_ok"):
            parts.append(f"价格比 {ratio:.2f} 在 {lo}~{hi} 带内")
        else:
            side = "低于下限" if ratio < lo else "高于上限"
            parts.append(
                f"价格比 {tiering_mod.fmt_ratio_to_show_side(ratio, lo, hi)} {side}"
                f"（{lo}~{hi} 价格带）"
            )

    overlap = reasons.get("keyword_overlap")
    if overlap is None:
        thin = bool(reasons.get("keyword_sample_thin"))
        if provisional:
            # 「没测」不是「测了偏低」。缺哪几维、这一档是凭什么暂定的，
            # 都由判定当时记录的 `missing_dims` 决定，不在这里另猜一份。
            missing = [
                dim
                for dim in DIM_ORDER
                if dim in (reasons.get("missing_dims") or ["keyword_overlap"])
            ]
            kept = [dim for dim in DIM_ORDER if dim not in missing]
            lack = "、".join(tiering_mod.missing_dim_labels(missing)) or "关键维度"
            if thin:
                # 有词，只是样本薄。写成「缺流量词」会把人送去查采集（那里没问题）。
                wc = reasons.get("keyword_word_count")
                head = (
                    f"竞品词库只抽到 {wc} 个词（上游每个 ASIN 只回一页），词重合算不出结论"
                    if wc
                    else "竞品词库抽样过薄，词重合算不出结论"
                )
            else:
                head = f"缺{lack}，词重合未测"
            parts.append(
                f"{head}"
                f"（这一档是按{'、'.join(tiering_mod.missing_dim_labels(kept)) or '其余维度'}暂定的）"
            )
        else:
            parts.append(
                "未测流量词（黑马判定不看这一维）"
                if black_horse
                else "缺少流量词数据，重合度未计算"
            )
    else:
        parts.append(
            f"加权词重合 {overlap:.0%}"
            f"（≥{th.keyword_overlap_gate:.0%} 入池，≥{th.overlap_s:.0%} 判 S）"
        )

    node = reasons.get("node_match")
    if node is True:
        parts.append("末级节点一致")
    elif node is False:
        parts.append("末级节点不同，靠词重合跨界截流")
    else:
        parts.append("节点数据缺失，未做节点核对")

    if reasons.get("functional_match") is True:
        parts.append("功能形态已确认同品")
    elif reasons.get("functional_match") is False:
        parts.append("功能形态判定为非同品")
    else:
        parts.append("功能形态待人工确认")

    head = f"判为 {tier} 级：" if tier else ""
    if tier == "unknown":
        # 不许写成「判为 C 级」或「判为 unknown 级」：C 是结论，unknown 是还没结论。
        # 也不许写成「暂定」：暂定是「有字母级但依据不全」，unknown 是「连字母级都没有」。
        head = "待判定（证据不足）："
    elif black_horse:
        # 黑马的依据是上架天数 + 月销，不是相关度。头部就把来源说出来，
        # 否则读者会把它当成「词重合很高」那一类 A。
        head = f"新品黑马 {tier} 级（上架 90 天内 + 月销 ≥1000）："
    elif provisional:
        head = f"暂定 {tier} 级："
    return head + "；".join(parts)


# ============================================================
# 写入
# ============================================================


def upsert(
    *,
    competitor_id: int,
    my_product_id: int,
    user_id: Optional[int] = None,
    tenant_id: Optional[str] = None,
    basis: Optional[dict] = None,
    basis_summary: Optional[str] = None,
    source: str = SOURCE_MANUAL,
    confidence: Optional[float] = None,
    status: str = "active",
) -> dict:
    """插入或更新一条绑定（按 user + competitor + product 三元组）。"""
    tenant_id = tenant_id or settings.tenant_id
    uid = int(user_id) if user_id is not None else identity.default_user_id(tenant_id)
    now = datetime.now()

    with get_session() as s:
        if s.get(Competitor, int(competitor_id)) is None:
            return {"ok": False, "error": f"竞品 #{competitor_id} 不存在"}
        if s.get(MyProduct, int(my_product_id)) is None:
            return {"ok": False, "error": f"自家产品 #{my_product_id} 不存在"}

        row = s.execute(
            select(CompetitorBinding).where(
                CompetitorBinding.user_id == uid,
                CompetitorBinding.competitor_id == int(competitor_id),
                CompetitorBinding.my_product_id == int(my_product_id),
            )
        ).scalar_one_or_none()

        if row is None:
            row = CompetitorBinding(
                tenant_id=tenant_id,
                user_id=uid,
                competitor_id=int(competitor_id),
                my_product_id=int(my_product_id),
                basis=basis,
                basis_summary=basis_summary,
                source=source,
                confidence=confidence,
                status=status,
                created_at=now,
                updated_at=now,
            )
            s.add(row)
        else:
            # 已有绑定：只更新「会变的东西」。created_at 不动，它记录的是「什么时候绑的」
            if basis is not None:
                row.basis = basis
            if basis_summary is not None:
                row.basis_summary = basis_summary
            row.source = source
            if confidence is not None:
                row.confidence = confidence
            row.status = status
            row.updated_at = now
        s.flush()

        return {
            "ok": True,
            "binding_id": row.id,
            "user_id": uid,
            "basis_summary": row.basis_summary,
            "status": row.status,
        }


def upsert_from_judgment(
    *,
    tenant_id: str,
    competitor_id: int,
    my_product_id: int,
    tier: str,
    tier_reasons: Optional[dict],
    user_id: Optional[int] = None,
    confidence: Optional[float] = None,
) -> dict:
    """把一次分层判定的结果落成绑定（`add_competitor` / `rebuild_basis` 都走它）。

    为什么集中在这里而不是让 pool 自己拼 basis：
    pool 已经混了两个业务域（已知问题，下一轮处理），再加一段 basis 拼装只会更糟。
    pool 只负责「调我一下」，快照长什么样由本模块决定。
    """
    layers = settings_store.effective_version(
        tenant_id, group_name=GROUP_TIERING, my_product_id=my_product_id, user_id=user_id
    )
    thresholds = settings_store.resolve_tiering_thresholds(
        tenant_id, my_product_id, user_id=user_id
    )
    basis = build_basis(
        tier_reasons=tier_reasons,
        tier=tier,
        thresholds_version=layers["version"],
        threshold_layers=layers["layers"],
    )
    summary = build_basis_summary(tier_reasons, tier=tier, thresholds=thresholds)
    return upsert(
        competitor_id=competitor_id,
        my_product_id=my_product_id,
        user_id=user_id,
        tenant_id=tenant_id,
        basis=basis,
        basis_summary=summary,
        source=SOURCE_AUTO,
        confidence=confidence,
    )


def rebuild_basis(
    tenant_id: Optional[str] = None,
    *,
    competitor_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> dict:
    """用**最近一次分层结果**重算 basis（不重新跑四同法则）。

    为什么不去重跑判定：重跑要用当天的价格与词，等于把「当初为什么这么判」
    换成「今天会怎么判」，那就失去了快照的意义。要重判请走池子维护任务。
    """
    tenant_id = tenant_id or settings.tenant_id

    with get_session() as s:
        stmt = select(Competitor).where(
            Competitor.tenant_id == tenant_id, Competitor.status != "archived"
        )
        if competitor_id is not None:
            stmt = stmt.where(Competitor.id == int(competitor_id))
        comps = [
            {
                "id": c.id,
                "asin": c.asin,
                "tier": c.tier or "C",
                "tier_reasons": c.tier_reasons,
                "my_product_id": c.my_product_id,
            }
            for c in s.execute(stmt).scalars()
        ]

    rebuilt: list[dict] = []
    skipped = 0
    for comp in comps:
        if not comp["tier_reasons"]:
            # 没有判定依据就没法生成 basis。宁可跳过，也不要写一个空的快照充数。
            skipped += 1
            continue
        res = upsert_from_judgment(
            tenant_id=tenant_id,
            competitor_id=comp["id"],
            my_product_id=comp["my_product_id"],
            tier=comp["tier"],
            tier_reasons=comp["tier_reasons"],
            user_id=user_id,
        )
        if res.get("ok"):
            rebuilt.append(
                {"asin": comp["asin"], "binding_id": res["binding_id"],
                 "basis_summary": res["basis_summary"]}
            )
        else:
            skipped += 1
            logger.warning("重建绑定失败 %s: %s", comp["asin"], res.get("error"))

    return {"ok": True, "rebuilt": len(rebuilt), "skipped": skipped, "items": rebuilt}


def disable(tenant_id: Optional[str], binding_id: int) -> dict:
    """停用绑定（软删）。历史动作仍引用它，所以不能真删。"""
    tenant_id = tenant_id or settings.tenant_id
    with get_session() as s:
        row = s.get(CompetitorBinding, int(binding_id))
        if row is None or row.tenant_id != tenant_id:
            return {"ok": False, "error": f"绑定 #{binding_id} 不存在"}
        row.status = "disabled"
        row.updated_at = datetime.now()
        return {"ok": True, "binding_id": row.id, "status": row.status}


# ============================================================
# 查询 / 解析
# ============================================================


def list_for(
    tenant_id: Optional[str] = None,
    *,
    competitor_id: Optional[int] = None,
    user_id: Optional[int] = None,
    include_disabled: bool = False,
) -> list[dict]:
    tenant_id = tenant_id or settings.tenant_id
    with get_session() as s:
        stmt = select(CompetitorBinding).where(CompetitorBinding.tenant_id == tenant_id)
        if competitor_id is not None:
            stmt = stmt.where(CompetitorBinding.competitor_id == int(competitor_id))
        if user_id is not None:
            stmt = stmt.where(CompetitorBinding.user_id == int(user_id))
        if not include_disabled:
            stmt = stmt.where(CompetitorBinding.status == "active")

        out = []
        for row in s.execute(stmt.order_by(CompetitorBinding.id)).scalars():
            user = s.get(User, row.user_id)
            comp = s.get(Competitor, row.competitor_id)
            my = s.get(MyProduct, row.my_product_id)
            out.append(
                {
                    "id": row.id,
                    "user_id": row.user_id,
                    "user": user.name if user else None,
                    "competitor_id": row.competitor_id,
                    "asin": comp.asin if comp else None,
                    "my_product_id": row.my_product_id,
                    "self_asin": my.asin if my else None,
                    "source": row.source,
                    "confidence": row.confidence,
                    "status": row.status,
                    "basis_summary": row.basis_summary,
                    "basis": dict(row.basis or {}),
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                }
            )
        return out


def resolve_binding(
    tenant_id: Optional[str], user_id: Optional[int], competitor_id: Optional[int]
) -> Optional[dict]:
    """找到某人对某竞品的 active 绑定；找不到返回 None。"""
    if competitor_id is None:
        return None
    tenant_id = tenant_id or settings.tenant_id
    uid = int(user_id) if user_id is not None else identity.default_user_id(tenant_id)
    rows = list_for(tenant_id, competitor_id=competitor_id, user_id=uid)
    return rows[0] if rows else None


def resolve_my_product(
    tenant_id: Optional[str], user_id: Optional[int], competitor_id: Optional[int]
) -> Optional[int]:
    """解析「这个竞品对应我哪个自家产品」。

    优先级：本人绑定 > 竞品自带的外键 > 租户第一个自家产品。
    为什么允许退化到第三档：`Competitor.my_product_id` 本来就非空，
    所以「拿不到自家产品」在实际数据里几乎不会发生；但真发生了要能兜住，
    而不是让一条动作记录写不进去。
    """
    tenant_id = tenant_id or settings.tenant_id

    binding = resolve_binding(tenant_id, user_id, competitor_id)
    if binding is not None:
        return int(binding["my_product_id"])

    with get_session() as s:
        if competitor_id is not None:
            comp = s.get(Competitor, int(competitor_id))
            if comp is not None and comp.my_product_id:
                return int(comp.my_product_id)
        my = s.execute(
            select(MyProduct).where(MyProduct.tenant_id == tenant_id).limit(1)
        ).scalar_one_or_none()
        return int(my.id) if my is not None else None
