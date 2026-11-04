"""L3 · 阈值试算（dry-run，绝不写库）。

## 这个模块存在的唯一理由

让老板在**改阈值之前**看到「改了会怎样」。所以它必须满足两条硬约束：

1. **复用生产的同一个 `DiffEngine`**。如果试算自己写一套「只比价格」的简化逻辑，
   用户会看到「试算说 3 次、实际报 12 次」，从此不信这个功能。
   本模块只做「喂数据 + 数结果」，判定一行都不自己写。
2. **零写库**。不写 signals、不写 snapshots、不写 collect_runs、不写阈值版本。
   验收方式是把 5 张表的 `count(*)` 在试算前后各拍一次快照，必须完全一致。

## 为什么给两个数字

- **原始命中数**（`current` / `candidate`）：回答「这个门槛有多敏感」；
- **冷却去重后的命中数**（`after_cooldown`）：回答「你实际会被打扰几次」。

只要一个数字都会误导：门槛放松时原始命中暴涨，但其中大半会被 4h/12h/48h 冷却吃掉。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.domain.diff import DiffEngine, SignalDraft
from app.domain.thresholds import (
    GROUP_DIFF,
    DiffThresholds,
    NotifyThresholds,
    defaults,
    validate,
)
from app.models import Competitor, MyProduct, Snapshot
from app.services import settings_store
from app.services.collector import is_oos_confirmed, resolve_season_phase, row_to_unified

logger = logging.getLogger(__name__)

#: 试算窗口上限。再长就只是在数历史，对「改阈值」这个决策没有增量信息。
MAX_DAYS = 90

_LEVELS = ("P0", "P1", "P2")


def _empty_counts() -> dict[str, int]:
    return {"p0": 0, "p1": 0, "p2": 0, "total": 0}


def _bump(counts: dict[str, int], level: str) -> None:
    key = (level or "").lower()
    if key in ("p0", "p1", "p2"):
        counts[key] += 1
        counts["total"] += 1


def _diff_overrides(values: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """从「可能是嵌套也可能是扁平」的入参里取出 diff 组的键。

    面板整页提交时会是 `{"diff": {...}, "tiering": {...}}`；飞书/Agent 调用时
    可能直接给 `{"price_p0_pct": 0.1}`。两种都得认，否则同一件事有两个入口两套行为。
    """
    values = dict(values or {})
    nested = values.get(GROUP_DIFF)
    if isinstance(nested, Mapping):
        raw = dict(nested)
    else:
        raw = values
    allowed = set(defaults(GROUP_DIFF))
    return {k: v for k, v in raw.items() if k in allowed}


def _personal_overrides(
    tenant_id: str,
    user_id: Optional[int],
    my_product_id: Optional[int],
) -> list[dict]:
    """当前操作人的**个人层**覆盖值，只用于在面板上提示，不参与任何计算。

    返回 `[{"group_name", "version", "keys", "values"}]`；没人或没覆盖就是空列表。
    为什么不复用 `resolve()`：那会把三层合并成一个数，看不出"这个值到底是谁设的"。
    这里要的恰恰是"个人层单独长什么样"。
    """
    if user_id is None:
        return []
    rows = settings_store.describe_layers(
        tenant_id, user_id=user_id, my_product_id=my_product_id
    )
    out: list[dict] = []
    for row in rows:
        if row.get("scope") != "user":
            continue
        values = dict(row.get("values") or {})
        out.append(
            {
                "group_name": row.get("group_name"),
                "version": row.get("version"),
                "keys": sorted(values),
                "values": values,
            }
        )
    return out


def simulate_diff_thresholds(
    tenant_id: Optional[str] = None,
    values: Optional[Mapping[str, Any]] = None,
    *,
    days: int = 7,
    asin: Optional[str] = None,
    user_id: Optional[int] = None,
    my_product_id: Optional[int] = None,
    personal_user_id: Optional[int] = None,
) -> dict:
    """把候选阈值和历史快照跑一遍，返回 `current / candidate / delta`。

    所有参数都可为空：没数据时返回 `ok: true` + 全 0，而不是报错——
    面板在任何时候都要能打开这一页（数据源全挂的时候尤其需要它）。

    ## `user_id` vs `personal_user_id`

    - `user_id`：参与**计算**的个人层。默认 None = 与定时采集同口径（company+product）。
      只有明确要做 per-user 模拟时才传。
    - `personal_user_id`：**只用于报告**。返回 `personal_overrides`，告诉调用方
      "这个人有 N 项个人覆盖，但试算与推送都不用它"。不传就什么都不报告。

    分开的理由：QA 的 D5 是"面板试算带 user 层、采集不带"，于是同一份数据两套阈值。
    修法不是"让采集也带 user 层"（个人覆盖本轮不参与推送，设计如此），
    而是把"参与计算"和"给人看"这两件事分开——前者只跟生产一致，后者只做提示。

    ## 为什么 diff 组不该接个人层

    「信号是否存在」是全局唯一事实：一轮采集 100 个竞品约 25 分钟、受数据源额度约束，
    不可能按人各跑一遍。所以决定信号产不产生的 `group_name="diff"` 生效层永远是
    company + product，与"谁在看"无关；个人差异的正确落点是 `notify`——它决定的是
    "这条信号推给谁"，不影响信号本身存不存在。返回值里的 `scope` 字段就是如实报告
    本次试算到底踩了哪几层（`scope.user_id is None` 表示没踩个人层）。
    """
    tenant_id = tenant_id or settings.tenant_id
    try:
        window_days = max(1, min(int(days or 7), MAX_DAYS))
    except (TypeError, ValueError):
        return {"ok": False, "error": f"days 必须是整数天数，收到 {days!r}"}

    overrides = _diff_overrides(values)
    cleaned, errors = validate(GROUP_DIFF, overrides)
    if errors:
        return {"ok": False, "error": errors[0], "errors": errors,
                "hint": "按 /api/config/thresholds/schema 给出的类型与范围修正"}

    # ---- 当前生效（company → product；user 只在显式传入时参与）----
    current_values, sources = settings_store.resolve(
        tenant_id, user_id=user_id, my_product_id=my_product_id
    )
    current_th = DiffThresholds.from_dict(current_values[GROUP_DIFF])
    candidate_th = current_th.merge(cleaned)
    notify_th = NotifyThresholds.from_dict(current_values["notify"])

    now = datetime.now()
    window_from = now - timedelta(days=window_days)

    with get_session() as s:
        series = _load_series(s, tenant_id, asin=asin, window_from=window_from, window_to=now)

    current = _evaluate(series, current_th)
    candidate = _evaluate(series, candidate_th)
    # 冷却模拟必须用同一份 notify 阈值跑两次，否则两个数字不可比
    cooldown_current = _count_after_cooldown(current["timeline"], notify_th)
    cooldown_candidate = _count_after_cooldown(candidate["timeline"], notify_th)

    delta = {
        key: candidate["totals"][key] - current["totals"][key] for key in _empty_counts()
    }
    changes = _diff_records(current["records"], candidate["records"])

    snapshot_rows = sum(len(item["rows"]) for item in series)
    pairs = sum(max(0, len(item["rows"]) - 1) for item in series)

    # 「参与计算的两层」与「这个人的个人覆盖（只提示，不参与）」分开返回。
    # 面板把后者显示成一句提醒，避免用户以为"我改了个人层，试算应该跟着变"。
    layers_used = settings_store.describe_layers(tenant_id, my_product_id=my_product_id)
    personal_overrides = _personal_overrides(tenant_id, personal_user_id, my_product_id)

    # 本次试算**实际**踩了哪几层——如实报告，不是"应该踩哪几层"。
    # 为什么值得单独返一个字段：试算的数字会被拿去做"要不要改阈值"的决策，
    # 而它只有和生产采集同口径时才有意义。生产走
    # `collector.py: resolve_diff_thresholds(tenant, my_product_id)`（不含个人层），
    # 所以 `user_id is None` + 不含 "user" 才是正常态；一旦含 "user"，
    # 面板必须当场告诉用户"这次结果和线上不一样"，而不是让人自己比对两个页面。
    diff_sources = (sources or {}).get(GROUP_DIFF) or {}
    scope = {
        "layers": sorted({str(v) for v in diff_sources.values()}),
        "user_id": user_id,
        "note": (
            "试算与生产采集同口径（公司层 + 产品层）。"
            "个人层只影响推送给谁，不改变信号是否产生。"
        ),
    }

    return {
        "ok": True,
        "window": {
            "from": window_from.isoformat(timespec="seconds"),
            "to": now.isoformat(timespec="seconds"),
            "days": window_days,
            "competitors": len(series),
            "snapshots": snapshot_rows,
            "pairs": pairs,
            "phase": series[0]["phase"] if series else "warmup",
        },
        "thresholds": {
            "current": current_th.to_dict(),
            "candidate": candidate_th.to_dict(),
            "changed_keys": sorted(cleaned),
        },
        "layers": layers_used,
        "personal_overrides": personal_overrides,
        # 试算口径自证：layers 是**本次真正生效**的层，user_id 为 None 即"没踩个人层"
        "scope": scope,
        "parity": {
            "user_layer_in_simulation": user_id is not None,
            "note": (
                "试算默认只算「公司默认 + 产品覆盖」两层，与定时采集/飞书推送同口径；"
                "个人覆盖不参与计算。"
            ),
        },
        "current": current["totals"],
        "candidate": candidate["totals"],
        "delta": delta,
        # 两个数字摆在一起，避免只看到一个就下结论（见模块文档「为什么给两个数字」）
        "hit_counts": {
            "current_raw": current["totals"]["total"],
            "current_after_cooldown": cooldown_current["total"],
            "candidate_raw": candidate["totals"]["total"],
            "candidate_after_cooldown": cooldown_candidate["total"],
        },
        "after_cooldown": {"current": cooldown_current, "candidate": cooldown_candidate},
        "by_competitor": _group_by_competitor(changes, current["by_asin"], candidate["by_asin"]),
        "samples": changes[:10],
    }


# ============================================================
# 取窗口数据
# ============================================================


def _load_series(
    s: Session,
    tenant_id: str,
    *,
    asin: Optional[str],
    window_from: datetime,
    window_to: datetime,
) -> list[dict]:
    """取每个竞品在窗口内（含窗口前最后一条基线）的快照序列。

    为什么要把「窗口前最后一条」也带上：窗口内第一条快照没有前序可比，
    不带基线的话每个竞品都会白白丢掉窗口起点的那一次对比，
    而那次对比往往正是「上周改了阈值，这一周表现如何」的关键一条。
    """
    stmt = select(Competitor).where(
        Competitor.tenant_id == tenant_id,
        Competitor.status != "archived",     # paused 仍参与试算：老板要看「重新打开会怎样」
    )
    if asin:
        stmt = stmt.where(Competitor.asin == (asin or "").strip().upper())

    out: list[dict] = []
    for comp in s.execute(stmt).scalars():
        rows = list(
            s.execute(
                select(Snapshot)
                .where(
                    Snapshot.competitor_id == comp.id,
                    Snapshot.captured_at >= window_from,
                    Snapshot.captured_at <= window_to,
                )
                .order_by(Snapshot.captured_at)
            ).scalars()
        )
        baseline = s.execute(
            select(Snapshot)
            .where(
                Snapshot.competitor_id == comp.id,
                Snapshot.captured_at < window_from,
            )
            .order_by(desc(Snapshot.captured_at))
            .limit(1)
        ).scalar_one_or_none()
        if baseline is not None:
            rows.insert(0, baseline)

        if len(rows) < 2:
            continue

        my = s.get(MyProduct, comp.my_product_id)
        out.append(
            {
                "competitor_id": comp.id,
                "asin": comp.asin,
                "tier": comp.tier or "C",
                "my_price": my.current_price if my else None,
                "phase": resolve_season_phase(s, my),
                "rows": [row_to_unified(r, comp.marketplace or "US") for r in rows],
            }
        )
    return out


# ============================================================
# 跑判定（与生产完全同一段代码）
# ============================================================


def _evaluate(series: list[dict], thresholds: DiffThresholds) -> dict:
    """用给定阈值把整个窗口跑一遍。

    返回：
      totals    各级别命中数
      by_asin   每个竞品的命中数
      records   配对用的索引 {(asin, 序号, 信号类型, after_value): draft}
      timeline  [(asin, 时刻, draft)]，时间升序，供冷却模拟用
    """
    totals = _empty_counts()
    by_asin: dict[str, dict[str, int]] = defaultdict(_empty_counts)
    records: dict[tuple, SignalDraft] = {}
    timeline: list[tuple[str, datetime, SignalDraft]] = []

    for item in series:
        engine = DiffEngine(phase=item["phase"], thresholds=thresholds)
        rows = item["rows"]
        for pair_idx, (prev, curr) in enumerate(zip(rows, rows[1:])):
            drafts = engine.compare(
                prev,
                curr,
                tier=item["tier"],
                my_price=item["my_price"],
                # 与生产同一条规则：断货是否已被上一轮确认
                oos_confirmed=is_oos_confirmed(prev),
            )
            for d in drafts:
                _bump(totals, d.level)
                _bump(by_asin[item["asin"]], d.level)
                records[(item["asin"], pair_idx, d.signal_type, d.after_value)] = d
                timeline.append((item["asin"], curr.captured_at or datetime.now(), d))

    timeline.sort(key=lambda x: x[1])
    return {
        "totals": totals,
        "by_asin": dict(by_asin),
        "records": records,
        "timeline": timeline,
    }


def _count_after_cooldown(
    timeline: list[tuple[str, datetime, SignalDraft]], notify_th: NotifyThresholds
) -> dict[str, int]:
    """在内存里模拟生产的冷却去重，数「实际会打扰你几次」。

    规则与 `collector._is_cooling_down` 一致：同一竞品同一信号类型，
    在「该信号等级的冷却窗口」内只算一次。窗口长度取自 notify 组配置。

    **边界口径必须与生产一致**：恰好等于窗口长度时算「还在冷却中」（抑制）。
    生产那边是 `detected_at >= now - window`，刚好卡在窗口起点的那条会被查到、
    于是被抑制；这里如果写成 `<`，同一条就会**被放行**，试算与线上差一次打扰。
    微秒级时间戳下几乎撞不上，但「几乎」不是「不会」，而且两边一旦不一致，
    以后再查「为什么试算说 5 次、线上推了 6 次」就要重新推一遍这里。
    """
    totals = _empty_counts()
    accepted: dict[tuple[str, str], datetime] = {}

    for asin, at, draft in timeline:
        key = (asin, draft.signal_type)
        window = timedelta(hours=notify_th.cooldown_hours(draft.level))
        last = accepted.get(key)
        if last is not None and (at - last) <= window:
            continue
        accepted[key] = at
        _bump(totals, draft.level)

    return totals


def _diff_records(
    current: dict[tuple, SignalDraft], candidate: dict[tuple, SignalDraft]
) -> list[dict]:
    """比对两轮结果，产出「等级变了 / 新出现 / 消失了」的明细。"""
    out: list[dict] = []
    all_keys = set(current) | set(candidate)

    for key in all_keys:
        before = current.get(key)
        after = candidate.get(key)
        before_level = before.level if before else None
        after_level = after.level if after else None
        if before_level == after_level:
            continue
        source = after or before
        out.append(
            {
                "asin": key[0],
                "signal_type": key[2],
                "before_level": before_level,
                "after_level": after_level,
                "description": source.description if source else "",
            }
        )
    return out


def _group_by_competitor(
    changes: list[dict],
    current_by_asin: dict[str, dict[str, int]],
    candidate_by_asin: dict[str, dict[str, int]],
) -> list[dict]:
    """只列「有变化」的竞品，最多 20 个：试算页要的是差异，不是全量清单。"""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for change in changes:
        buckets[change["asin"]].append(change)

    out = []
    for asin, items in buckets.items():
        out.append(
            {
                "asin": asin,
                "current": current_by_asin.get(asin, _empty_counts()),
                "candidate": candidate_by_asin.get(asin, _empty_counts()),
                "changed": items[:20],
            }
        )
    out.sort(key=lambda x: (-len(x["changed"]), x["asin"]))
    return out[:20]
