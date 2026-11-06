"""L3 · 决策落库：把「建议」变成可回查的审计记录。

## 在此之前发生过什么

`app/domain/decision.py` 里五条主线的判定逻辑是完整的，`briefer._attach_action`
也一直在调用它——但它算完就把结果放进内存里的 `BriefItem`，日报一渲染、消息一发，
**对象被回收，判断就消失了**。

后果不是「少了一张表的数据」，而是一句具体的话答不上来：
三天后老板问「你当时为什么建议我跟这个价」，系统能重新算一遍，
但算出来的是**现在的**建议——锚点补全了、阈值改过了，答案已经不一样了。
用现在的结论回答「当时为什么」，是一种看起来正确、实际会误导人的错误。

## 为什么 `persist()` 收 dict 而不是 `BriefItem`

`briefer` 要 import 本模块，本模块不能反过来 import `briefer`（循环）。
所以边界上只认普通字典，形状见 `_RECORD_KEYS`。

这不是放弃类型检查——恰恰相反：跨模块传 dict 时，字段名写错是**静默失败**
（`.get()` 返回 None），所以 `persist()` 在入口就把 `signal_id` / `competitor_id`
拣出来校验，缺了直接算跳过并计数，而不是写一行半空的记录进去。
半空的审计记录比没有记录更糟：它看起来有据可查，实际上查不出东西。

## 为什么写入是 best-effort

`persist()` 被两个地方调用：日报生成、实时预警。两处都在「马上要往外发消息」的
链路上。审计写失败（磁盘满、库被锁）时，**不能连累消息发不出去**——
老板可以不看审计记录，但不能不收到「对手断货了」。

所以失败只记日志、返回 `ok:false`，不抛。调用方也不需要处理返回值。
反过来说：如果哪天真需要「审计必须成功才能发」，那是在要求消息通道和数据库
要么同时成功要么同时失败，这件事在跨系统下做不到，别做。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.db import get_session
from app.models import Competitor, Decision, Signal

logger = logging.getLogger(__name__)

# ---- 判断结论的取值（与 decision.py 的 DecisionResult.decision 对齐）----
DECISION_ATTACK = "attack"
DECISION_DEFEND = "defend"
DECISION_LEARN = "learn"
DECISION_NO_ACTION = "no_action"

DECISION_LABELS: dict[str, str] = {
    DECISION_ATTACK: "进攻",
    DECISION_DEFEND: "防守",
    DECISION_LEARN: "学习",
    DECISION_NO_ACTION: "不动",
}

# ---- 出口 ----
ORIGIN_BRIEF = "brief"    # 日报
ORIGIN_ALERT = "alert"    # 实时预警
ORIGINS: tuple[str, ...] = (ORIGIN_BRIEF, ORIGIN_ALERT)

ORIGIN_LABELS = {ORIGIN_BRIEF: "日报", ORIGIN_ALERT: "实时预警"}

#: `persist()` 认的字段。列在这里是为了让「字典里能放什么」有唯一的答案，
#: 而不是散在三个调用点各自猜。
_RECORD_KEYS = (
    "signal_id",
    "competitor_id",
    "decision",
    "headline",
    "reasons",
    "actions",       # 注意：入参叫 actions，落库列叫 suggested_actions
    "metrics",
    "confidence",
    "verify_days",
    "signal_type",
)


# ============================================================
# 幂等键
# ============================================================


def dedupe_key(
    *,
    tenant_id: str,
    signal_id: Optional[int],
    decision: str,
    headline: str,
    actions: Optional[list],
    metrics: Optional[dict],
) -> str:
    """`sha256(租户|信号|结论|标题|动作|中间量)[:32]`。

    为什么把 `metrics` 也算进去：跟价判断的结论由算出来的盈亏平衡单量支撑。
    锚点里的日均销量从 85 改到 120 时，结论文案可能仍然是「可以跟进降价」，
    但支撑它的数变了——那是一次**不同的判断**，应该留两行。
    只按 (signal, decision) 去重会把这个差别抹平。

    为什么 `reasons` 不算进去：它是 `metrics` 的文字展开，
    两者同时变或同时不变，算一个就够；都算只会让键更脆
    （改一句文案就换钥匙，结果是一次无关紧要的措辞调整变成两条历史记录）。
    """
    payload = json.dumps(
        {
            "t": tenant_id,
            "s": signal_id,
            "d": decision or "",
            "h": headline or "",
            "a": actions or [],
            "m": metrics or {},
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# ============================================================
# 写入
# ============================================================


def persist(
    records: Iterable[dict],
    *,
    tenant_id: str,
    origin: str = ORIGIN_BRIEF,
) -> dict:
    """把一批判断写进 `decisions`。返回 `{"ok", "written", "duplicate", "skipped", "ids"}`。

    幂等由 `ux_decision_dedupe` 唯一索引兜底：并发下两个写入方都通过
    "先查后插"的前置检查时，第二个会被数据库挡住，这里捕获 `IntegrityError`
    并计入 `duplicate`。所以**不需要重试，也不需要串行化**。

    整个批次共用一个会话：一条判断写失败就整批回滚，避免「一半进了库」。
    审计记录半新半旧比全都没写更难排查——你会以为那天的判断只有三条。

    `ids` 是 `{signal_id: decision_id}`，供调用方回填到 `BriefItem` 上。
    重复记录也会给出已有行的 ID——「这条判断已经在库里了」和
    「这条判断对应哪一行」是两个问题，调用方两个都要。
    """
    rows_in = [r for r in records if isinstance(r, dict)]
    written = 0
    duplicate = 0
    skipped = 0
    ids: dict[int, int] = {}

    prepared: list[dict] = []
    for rec in rows_in:
        signal_id = rec.get("signal_id")
        competitor_id = rec.get("competitor_id")
        # 缺主键的判断不写：它没法被「按信号回查」，也就没有审计价值。
        # 刻意不猜（比如拿 asin 去反查 competitor_id）——猜错会写进另一个竞品的账上。
        if signal_id is None or competitor_id is None:
            skipped += 1
            continue
        prepared.append(rec)

    if not prepared:
        return {"ok": True, "written": 0, "duplicate": 0, "skipped": skipped, "ids": ids}

    created = datetime.now()
    try:
        with get_session() as s:
            for rec in prepared:
                verify_days = rec.get("verify_days")
                key = dedupe_key(
                    tenant_id=tenant_id,
                    signal_id=int(rec["signal_id"]),
                    decision=rec.get("decision") or "",
                    headline=rec.get("headline") or "",
                    actions=rec.get("actions"),
                    metrics=rec.get("metrics"),
                )
                existing = _existing_id(s, key)
                if existing is not None:
                    duplicate += 1
                    ids[int(rec["signal_id"])] = existing
                    continue

                due = (
                    created + timedelta(days=int(verify_days))
                    if verify_days
                    else None
                )
                row = Decision(
                    tenant_id=tenant_id,
                    signal_id=int(rec["signal_id"]),
                    competitor_id=int(rec["competitor_id"]),
                    decision=rec.get("decision") or DECISION_NO_ACTION,
                    headline=rec.get("headline") or "",
                    reasons=rec.get("reasons"),
                    suggested_actions=rec.get("actions"),
                    metrics=rec.get("metrics"),
                    confidence=rec.get("confidence"),
                    verify_days=int(verify_days) if verify_days else None,
                    verify_due_at=due,
                    signal_type=rec.get("signal_type"),
                    origin=origin,
                    dedupe_key=key,
                    created_at=created,
                )
                s.add(row)
                # 逐条 flush：让唯一索引在批次内就生效，同批次里的重复也能挡住。
                # 代价是每行一次 round-trip，但这个量级（一次日报几十行）不值得优化。
                try:
                    s.flush()
                except IntegrityError:
                    # 只可能来自并发（前置 `_existing_id` 已经挡住了常规重复）。
                    # 此时会话已脏，整批回滚是正确的：审计记录要么是完整的一次判断，
                    # 要么没有——半批会让人以为「那天只判了三条」。
                    logger.info("决策落库：撞唯一索引（并发去重），本批回滚")
                    s.rollback()
                    return {
                        "ok": True,
                        "written": 0,
                        "duplicate": duplicate + 1,
                        "skipped": skipped,
                        "ids": {},
                    }
                written += 1
                ids[int(rec["signal_id"])] = row.id

            return {
                "ok": True,
                "written": written,
                "duplicate": duplicate,
                "skipped": skipped,
                "ids": ids,
            }
    except SQLAlchemyError:
        # 审计写失败不能连累消息发送。见模块文档「为什么写入是 best-effort」。
        logger.warning("决策落库失败（不影响消息发送）", exc_info=True)
        return {
            "ok": False,
            "written": 0,
            "duplicate": duplicate,
            "skipped": skipped,
            "ids": {},
        }


def _existing_id(session, key: str) -> Optional[int]:
    """幂等键已存在则返回那一行的 ID。

    返回 ID 而不是 bool：调用方要把 `decision_id` 回填到 BriefItem 上，
    重复时也得给出「它对应哪一行」——否则第二次跑日报时，
    面板上那几条判断会突然从「有决策」变成「没决策」。
    """
    row = session.execute(select(Decision.id).where(Decision.dedupe_key == key)).first()
    return int(row[0]) if row else None


# ============================================================
# 读取
# ============================================================


def to_dict(row: Decision, *, competitor_asin: Optional[str] = None) -> dict:
    """行的对外形状。`decision` 同时给代码值和中文，前端不必自己维护映射表。"""
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "signal_id": row.signal_id,
        "competitor_id": row.competitor_id,
        "asin": competitor_asin,
        "decision": row.decision,
        "decision_label": DECISION_LABELS.get(row.decision, row.decision),
        "headline": row.headline,
        "reasons": row.reasons or [],
        "actions": row.suggested_actions or [],
        "metrics": row.metrics or {},
        "confidence": row.confidence,
        "verify_days": row.verify_days,
        "verify_due_at": (
            row.verify_due_at.isoformat(timespec="seconds") if row.verify_due_at else None
        ),
        "signal_type": row.signal_type,
        "origin": row.origin,
        "origin_label": ORIGIN_LABELS.get(row.origin or "", row.origin or ""),
        "at": row.created_at.isoformat(timespec="seconds") if row.created_at else None,
    }


def get(decision_id: int, tenant_id: Optional[str] = None) -> Optional[dict]:
    with get_session() as s:
        row = s.get(Decision, int(decision_id))
        if row is None:
            return None
        if tenant_id is not None and row.tenant_id not in (None, tenant_id):
            return None
        comp = s.get(Competitor, row.competitor_id) if row.competitor_id else None
        return to_dict(row, competitor_asin=comp.asin if comp else None)


def latest_for_signal(tenant_id: str, signal_id: int) -> Optional[dict]:
    """某条信号最近一次被判断成什么样。动作记录用它挂上 `decision_id`。

    取「最近一次」而不是「唯一一次」：一条信号可能因为锚点补全被重判，
    复盘时应该看到**人做动作那一刻**依据的那个判断，也就是最近的。
    """
    with get_session() as s:
        row = s.execute(
            select(Decision)
            .where(Decision.tenant_id == tenant_id, Decision.signal_id == int(signal_id))
            .order_by(desc(Decision.created_at), desc(Decision.id))
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        comp = s.get(Competitor, row.competitor_id) if row.competitor_id else None
        return to_dict(row, competitor_asin=comp.asin if comp else None)


# 这里原来还有 `list_for_competitor` 和 `stats` 两个函数，都没人调，已删。
# - 「按竞品看判断历史」交给 `get_change_history` 的第四条时间线：它在**同一个
#   session** 里和快照/信号/动作一起查。删掉重复实现，是为了不让两份查询逻辑
#   慢慢分叉——那正是「同一批数据、两条路给出不同结论」的老毛病。
# - `stats`（总条数、按结论分布）在等一个真实的调用方。现在没有页面要显示它，
#   写完挂着就是第二份「建了但没出口」。等做「复盘效果聚合」时和它的调用方一起加。
