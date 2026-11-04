"""L3 · 数据健康：采集失败轮次、价格可信度、快照新鲜度、未映射身份。

## 这个模块在回答一个问题

「我看到的结论有多可信？」——数据源挂掉的时候，用户最需要的恰恰是这一页，
所以它必须在**任何数据状态下**都能返回东西（空列表而不是报错）。

## 为什么「价格不可信」不需要新列

`Snapshot.net_price` 只可能由 promotions 接口写入（`collector.row_to_unified()`
正是靠「有 net_price ⇔ 当轮促销接口成功」这个等价关系反推 `price_source`）。
因此 **`net_price IS NULL` 等价于「当轮价格不可知」**，按天聚合「为空的比例」
就是价格可信度曲线。

**不要"补一个 0"**。补 0 会把「不知道」变成「价格是 0」，
下游的降价检测、均价统计会全部被污染，而且没有任何报错。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select

from app.config import settings
from app.db import get_session
from app.models import CollectRun, Competitor, Snapshot
from app.services import identity

logger = logging.getLogger(__name__)

#: 超过这么久没新快照，就算「数据过期」。与 briefer._data_health 保持同一口径。
STALE_HOURS = 36


def collect_health(tenant_id: Optional[str] = None, days: int = 7) -> dict:
    """五块健康度打包。一次性给全，避免面板为了拼一页打五个请求。"""
    tenant_id = tenant_id or settings.tenant_id
    window = max(1, int(days or 7))

    return {
        "ok": True,
        "window_days": window,
        "rounds": collect_rounds(tenant_id, days=window),
        "failures": failures(tenant_id, days=window),
        "price_trust": price_trust(tenant_id, days=window),
        "snapshot_freshness": snapshot_freshness(tenant_id),
        "unmapped_identities": identity.unmapped_identities(),
    }


def collect_rounds(tenant_id: Optional[str] = None, days: int = 7) -> dict:
    """采集轮次统计。`success_rate` 用 ok 数算，不用「没报错」算。"""
    tenant_id = tenant_id or settings.tenant_id
    since = datetime.now() - timedelta(days=max(1, int(days or 7)))

    with get_session() as s:
        rows = list(
            s.execute(
                select(CollectRun).where(
                    CollectRun.tenant_id == tenant_id, CollectRun.started_at >= since
                )
            ).scalars()
        )

    total = len(rows)
    ok = sum(1 for r in rows if r.ok)
    partial = sum(1 for r in rows if r.ok and r.data_quality == "partial")
    failed = total - ok
    return {
        "total": total,
        "ok": ok,
        "failed": failed,
        "partial": partial,
        "success_rate": round(ok / total, 3) if total else None,
    }


def failures(
    tenant_id: Optional[str] = None, days: int = 7, limit: int = 20
) -> list[dict]:
    """最近的失败轮次（新→旧）。没有失败就返回空列表——空列表本身是好消息。"""
    tenant_id = tenant_id or settings.tenant_id
    since = datetime.now() - timedelta(days=max(1, int(days or 7)))

    with get_session() as s:
        rows = list(
            s.execute(
                select(CollectRun)
                .where(
                    CollectRun.tenant_id == tenant_id,
                    CollectRun.started_at >= since,
                    CollectRun.ok.is_(False),
                )
                .order_by(CollectRun.started_at.desc())
                .limit(max(1, int(limit)))
            ).scalars()
        )

    out = []
    for r in rows:
        errors = list(r.errors or [])
        out.append(
            {
                "id": r.id,
                "at": r.started_at.isoformat() if r.started_at else None,
                "asin": r.asin,
                "trigger": r.trigger,
                "data_quality": r.data_quality,
                # 面板只显示一句人话；完整 errors 留在库里
                "error": errors[0] if errors else "未记录原因",
                "duration_ms": r.duration_ms,
            }
        )
    return out


def price_trust(tenant_id: Optional[str] = None, days: int = 7) -> list[dict]:
    """按天的价格可信度。

    `price_known` = 该天 `net_price` 非空的快照数；
    `price_unknown` = `net_price` 为空的快照数（当轮促销接口没成功）。
    判据见模块文档：**net_price IS NULL ⇔ 当轮价格不可知，不要补 0**。
    """
    tenant_id = tenant_id or settings.tenant_id
    since = datetime.now() - timedelta(days=max(1, int(days or 7)))

    with get_session() as s:
        rows = s.execute(
            select(Snapshot.captured_at, Snapshot.net_price)
            .join(Competitor, Snapshot.competitor_id == Competitor.id)
            .where(Competitor.tenant_id == tenant_id, Snapshot.captured_at >= since)
        ).all()

    buckets: dict[str, dict[str, int]] = defaultdict(lambda: {"rounds": 0, "price_known": 0})
    for captured_at, net_price in rows:
        if captured_at is None:
            continue
        day = captured_at.strftime("%Y-%m-%d")
        buckets[day]["rounds"] += 1
        if net_price is not None:
            buckets[day]["price_known"] += 1

    out = []
    for day in sorted(buckets):
        item = buckets[day]
        unknown = item["rounds"] - item["price_known"]
        out.append(
            {
                "date": day,
                "rounds": item["rounds"],
                "price_known": item["price_known"],
                "price_unknown": unknown,
                "trust_ratio": round(item["price_known"] / item["rounds"], 3)
                if item["rounds"]
                else None,
            }
        )
    return out


def snapshot_freshness(tenant_id: Optional[str] = None) -> list[dict]:
    """每个在盯的竞品「最后一次有数据是什么时候」。"""
    tenant_id = tenant_id or settings.tenant_id
    now = datetime.now()

    out: list[dict] = []
    with get_session() as s:
        comps = list(
            s.execute(
                select(Competitor).where(
                    Competitor.tenant_id == tenant_id, Competitor.status != "archived"
                )
            ).scalars()
        )
        for comp in comps:
            last = s.execute(
                select(Snapshot)
                .where(Snapshot.competitor_id == comp.id)
                .order_by(Snapshot.captured_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            hours_ago = None
            if last is not None and last.captured_at is not None:
                hours_ago = round((now - last.captured_at).total_seconds() / 3600, 1)
            out.append(
                {
                    "competitor_id": comp.id,
                    "asin": comp.asin,
                    "status": comp.status,
                    "last_at": last.captured_at.isoformat() if last and last.captured_at else None,
                    "hours_ago": hours_ago,
                    "quality": last.data_quality if last else None,
                    # 从没采到过数据也算 stale：那是「还没开始」而不是「一切正常」
                    "stale": hours_ago is None or hours_ago > STALE_HOURS,
                }
            )
    out.sort(key=lambda x: (x["hours_ago"] is None, -(x["hours_ago"] or 0)))
    return out
