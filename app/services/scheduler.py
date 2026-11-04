"""调度中心。

三件事，节奏完全不同：

1. **采集**（高频、静默）——默认 1 小时一轮；旺季切到 15 分钟。
   采集只写库，不发消息。跑多密都不会打扰老板。
2. **P0 预警**（低频、必须打扰）——采集结束后立刻检查有没有 P0，
   有就单独推一条。这是唯一允许随时打断老板的通道。
3. **日报**（每天一次）——固定早上推送，把过去 24 小时的变化收成一份。

季节日历会动态调整采集间隔：旺季错过的代价 >> 误报的代价，
所以旺季不只是"提高频率"，同时 DiffEngine 的阈值也会收紧（见 diff.RULES）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.config import settings
from app.db import get_session
from app.models import Competitor, MyProduct, Signal
from app.services import briefer, collector

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


# ============================================================
# 任务
# ============================================================


async def job_collect() -> None:
    """采集 + 判定 + 落库，然后触发 P0 推送。"""
    tenant_id = settings.tenant_id
    started = datetime.now()
    results = await collector.collect_all(tenant_id)

    ok = sum(1 for r in results if r.ok)
    failed = len(results) - ok
    p0 = sum(r.p0_count for r in results)
    logger.info(
        "采集完成 %d/%d 成功，失败 %d，P0 信号 %d，耗时 %.1fs",
        ok,
        len(results),
        failed,
        p0,
        (datetime.now() - started).total_seconds(),
    )

    if p0 and settings.p0_push_enabled:
        await push_p0_alerts()


async def push_p0_alerts(minutes: int = 90) -> int:
    """取出未推送的 P0 信号，逐条发出去。"""
    from app.channels.router import get_router  # 延迟导入，避免循环依赖

    items = briefer.build_alerts_since(settings.tenant_id, minutes=minutes)
    if not items:
        return 0

    router = get_router()
    sent = 0
    for item in items:
        text = briefer.build_alert_text(item)
        # 带上 `competitor_id`：路由层据此把这条预警发给「盯这个竞品的人」。
        # `item.competitor_id` 可能是 None（例如历史数据），这里不做判断——
        # 为 None 时路由层自然退回租户默认渠道，判断只留在一处。
        ok = await router.push_alert(
            settings.tenant_id, text, item=item, competitor_id=item.competitor_id
        )
        if ok:
            sent += 1
    logger.info("P0 预警已推送 %d/%d 条", sent, len(items))
    return sent


async def job_daily_brief() -> None:
    """生成并推送日报。"""
    from app.channels.router import get_router

    brief = briefer.build_daily_brief(settings.tenant_id, hours=24)
    text = brief.to_text()
    router = get_router()
    ok = await router.push_daily(settings.tenant_id, brief, text)
    logger.info("日报推送%s（P0=%d, P1=%d）", "成功" if ok else "失败", len(brief.p0), len(brief.p1))


async def job_pool_maintenance() -> None:
    """每周重算一次分层，把掉队的降级、把黑马升级。

    分层不是一次性动作。对手会换策略，你的价格也会动，
    所以四同法则必须定期重跑，否则池子会慢慢失真。
    """
    from app.domain.tiering import judge_tier
    from app.models import Snapshot
    from app.services import settings_store
    from app.services.collector import row_to_unified

    # 数据现状（不是待办）：`snapshots.keywords` 与 `MyProduct.category_path` 都已落库。
    #   采集侧 `collector._save_snapshot` 会把流量词与类目路径写进快照，
    #   老库由 `app/migrations.py` 幂等补列，还原侧 `row_to_unified` 二者都会带回。
    #   因此正常情况下 `decidable_dims` 为 3，下面那个 insufficient 分支不会命中。
    #   保留它的理由：那是「将来再加维度、而新维度数据尚未采集」时的护栏，
    #   不是这一轮的临时补丁——**不要**因为现在不命中就删掉它。
    changed = 0
    skipped: list[str] = []
    with get_session() as s:
        comps = list(
            s.execute(
                select(Competitor).where(
                    Competitor.tenant_id == settings.tenant_id,
                    Competitor.status == "active",
                )
            ).scalars()
        )
        for comp in comps:
            my = s.get(MyProduct, comp.my_product_id)
            if my is None:
                continue
            # 显式取「最新一条」，不用 `comp.snapshots[-1]`：
            #   1. 关系列表会把该竞品最多 180 条快照全部加载进内存，只为取最后一条；
            #   2. 更要紧的是 `[-1]` 的语义依赖关系的排序。排序一漏（关系少写 order_by），
            #      `[-1]` 就退化成「数据库返回顺序的最后一条」——补采历史或 CSV 回填后
            #      那是**过期快照**，拿它重算会把整个池子的结论覆盖成旧值，且完全静默。
            # 这里把「最新」写进 SQL 的 ORDER BY，语义不依赖任何调用方约定。
            last = s.execute(
                select(Snapshot)
                .where(Snapshot.competitor_id == comp.id)
                .order_by(Snapshot.captured_at.desc(), Snapshot.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if last is None:
                continue
            snap = row_to_unified(last, comp.marketplace)

            # 阈值必须从配置读，否则「面板改了分层门槛、周维护还按老标准重算」，
            # 池子会跟配置慢慢脱节。只走 company + product 两层：分层是产品属性。
            # 注意：每轮都重新 resolve，不做进程内缓存——配置是随时会被改的。
            res = judge_tier(
                my_price=my.current_price,
                my_core_keywords=my.core_keywords or {},
                my_category_path=list(my.category_path or []) or None,
                my_daily_units=my.daily_sales_avg,
                comp=snap,
                functional_match=comp.functional_match,
                thresholds=settings_store.resolve_tiering_thresholds(
                    settings.tenant_id, my.id
                ),
            )
            # 证据不足 → 跳过，一个字段都不写回。
            # 为什么不写回 res.tier：那等于把「判不了」记成「判成 C」，而 C 是有含义的结论。
            # 之前这个任务就是这么把整个池子每周扫平一次的，而且完全静默。
            # 也不覆盖 tier_reasons：半份理由会在半年后被当成「当初就是这么判的」。
            if res.insufficient:
                skipped.append(comp.asin)
                continue

            if res.tier != comp.tier:
                logger.info("竞品 %s 分层调整 %s → %s", comp.asin, comp.tier, res.tier)
                comp.tier = res.tier
                comp.tier_reasons = res.tier_reasons
                comp.tier_updated_at = datetime.now()
                changed += 1
            comp.price_ratio = res.price_ratio
            comp.keyword_overlap = res.keyword_overlap
            comp.node_match = res.node_match

    if skipped:
        logger.info(
            "本轮 %d 个竞品因可判维度不足（能判的维度 < 2）跳过重分层：%s",
            len(skipped),
            "、".join(skipped[:10]) + ("…" if len(skipped) > 10 else ""),
        )
    logger.info("池子维护完成，%d 个竞品分层发生变化", changed)


async def job_purge_old_snapshots(days: int = 180) -> None:
    """清理过期快照，保留信号与决策记录。

    快照是最占空间的数据，但超过半年的日粒度快照对决策没有增量价值。
    """
    from sqlalchemy import delete

    from app.models import Snapshot

    cutoff = datetime.now() - timedelta(days=days)
    with get_session() as s:
        res = s.execute(delete(Snapshot).where(Snapshot.captured_at < cutoff))
        logger.info("清理过期快照 %s 条", res.rowcount)


# ============================================================
# 启动 / 停止
# ============================================================


def _current_interval_minutes() -> int:
    """按季节阶段决定采集间隔。"""
    from app.services.collector import resolve_season_phase

    try:
        with get_session() as s:
            my = s.execute(
                select(MyProduct).where(MyProduct.tenant_id == settings.tenant_id).limit(1)
            ).scalar_one_or_none()
            phase = resolve_season_phase(s, my)
    except Exception:  # noqa: BLE001
        phase = "warmup"

    if phase == "blast":
        return settings.blast_interval_minutes
    return settings.collect_interval_minutes


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    sch = AsyncIOScheduler(timezone="Asia/Shanghai")

    # ---- 采集 ----
    sch.add_job(
        job_collect,
        trigger=IntervalTrigger(minutes=_current_interval_minutes()),
        id="collect",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
        replace_existing=True,
    )

    # ---- 日报 ----
    sch.add_job(
        job_daily_brief,
        trigger=CronTrigger(hour=settings.daily_brief_hour, minute=settings.daily_brief_minute),
        id="daily_brief",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # ---- 池子维护（每周） ----
    sch.add_job(
        job_pool_maintenance,
        trigger=CronTrigger(
            day_of_week=settings.pool_maintain_weekday, hour=settings.pool_maintain_hour
        ),
        id="pool_maintenance",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # ---- 快照清理（每天深夜） ----
    sch.add_job(
        job_purge_old_snapshots,
        trigger=CronTrigger(hour=3, minute=30),
        id="purge",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    sch.start()
    _scheduler = sch
    logger.info(
        "调度已启动：采集每 %d 分钟，日报 %02d:%02d",
        _current_interval_minutes(),
        settings.daily_brief_hour,
        settings.daily_brief_minute,
    )
    return sch


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("调度已停止")
    _scheduler = None


def reschedule_collect(minutes: int) -> None:
    """手动改采集间隔（Agent 在旺季可以调这个）。"""
    if _scheduler is None:
        return
    _scheduler.reschedule_job("collect", trigger=IntervalTrigger(minutes=minutes))
    logger.info("采集间隔已改为 %d 分钟", minutes)


def scheduler_status() -> dict:
    if _scheduler is None or not _scheduler.running:
        return {"running": False, "jobs": []}
    return {
        "running": True,
        "jobs": [
            {
                "id": j.id,
                "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
                "trigger": str(j.trigger),
            }
            for j in _scheduler.get_jobs()
        ],
    }
