"""采集服务：把「拉数据 → 落快照 → 比差异 → 落信号」串成一条链。

一条铁律：采集失败绝不产生信号。数据源挂了就如实记一笔 errors，
宁可不报，也不能拿 None 去比出一个假信号——误报比漏报更能摧毁信任。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.adapters.sellersprite import SellerSpriteAdapter
from app.adapters.unified import UnifiedSnapshot
from app.db import get_session
from app.domain.diff import DiffEngine, SignalDraft
from app.models import (
    CollectRun,
    Competitor,
    Decision,
    MyProduct,
    SeasonCalendar,
    Signal,
    Snapshot,
)
from app.services import settings_store

logger = logging.getLogger(__name__)

#: 采集触发来源，写进 collect_runs.trigger。
#: 为什么要区分：定时采集失败是「数据源的问题」，面板手动采集失败是「刚才那个按钮的问题」，
#: 排查方向完全不同，事后只看一行日志必须能分清。
TRIGGER_SCHEDULE = "schedule"
TRIGGER_MANUAL = "manual"
TRIGGER_PANEL = "panel"
#: 模型在对话里主动要求采集。与 `manual` 分开是因为两者的失败模式不同：
#: 人点的采集失败通常是一次网络抖动，模型点的失败可能意味着它判断错了"该不该采"。
#: 合成一档，健康页就回答不了"是不是模型在乱调工具"。
TRIGGER_AGENT = "agent"


# ============================================================
# 返回结构
# ============================================================


@dataclass
class CollectResult:
    competitor_id: int
    asin: str
    ok: bool
    data_quality: str = "failed"
    signals: list[SignalDraft] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    is_first_snapshot: bool = False

    @property
    def p0_count(self) -> int:
        return sum(1 for s in self.signals if s.level == "P0")

    @property
    def p1_count(self) -> int:
        return sum(1 for s in self.signals if s.level == "P1")


# ============================================================
# 对外入口（同步 / 异步双版本）
# ============================================================


def collect_competitor(competitor_id: int, trigger: str = TRIGGER_SCHEDULE) -> CollectResult:
    """同步入口，给脚本和 Celery 风格调用用。"""
    return asyncio.run(acollect_competitor(competitor_id, trigger=trigger))


async def acollect_competitor(
    competitor_id: int, trigger: str = TRIGGER_SCHEDULE
) -> CollectResult:
    """采集单个竞品。内部自己开会话，不依赖外部事务。

    每一轮都会在 `collect_runs` 留一行痕迹（成功也留、失败更留）。
    没有这张表，「哪一轮采集失败了」只能靠猜，而数据健康页的全部价值就在这一问。
    """
    started_at = datetime.now()

    # ---- 读元信息 + 开一条采集日志 ----
    # 先把需要的元信息读出来，避免网络请求期间占着会话
    with get_session() as s:
        comp = s.get(Competitor, competitor_id)
        if comp is None:
            # 竞品本身不存在，日志写了也指不到任何对象，直接返回
            return CollectResult(competitor_id=competitor_id, asin="", ok=False, errors=["竞品不存在"])
        asin = comp.asin
        marketplace = comp.marketplace or "US"
        tier = comp.tier
        tenant_id = comp.tenant_id
        my_product_id = comp.my_product_id
        my = s.get(MyProduct, comp.my_product_id)
        my_price = my.current_price if my else None
        phase = resolve_season_phase(s, my)

        run = CollectRun(
            tenant_id=tenant_id,
            competitor_id=competitor_id,
            asin=asin,
            started_at=started_at,
            ok=False,          # 先记失败，跑成功再翻牌。这样「进程被 kill」也会留下失败痕迹。
            trigger=trigger,
        )
        s.add(run)
        s.flush()
        run_id = run.id

    # ---- 取阈值（系统行为没有自然人：user_id=None，只走 company → product）----
    # 取不到就落回默认值，绝不因为「配置读失败」而不采集。
    thresholds = settings_store.resolve_diff_thresholds(tenant_id, my_product_id)
    notify_th = settings_store.resolve_notify_thresholds(tenant_id, my_product_id)

    # ---- 拉数据（网络，不持锁） ----
    adapter = SellerSpriteAdapter()
    try:
        snap = await adapter.fetch_snapshot(asin, marketplace)
    except Exception as exc:  # noqa: BLE001
        # 抛出去之前先落账：否则「采集抛异常」这一轮在库里根本不存在，
        # 而它恰恰是最需要被看到的一类失败。
        _finish_run(
            run_id,
            started_at=started_at,
            ok=False,
            data_quality="failed",
            errors=[f"{type(exc).__name__}: {exc}"],
        )
        raise

    if snap.data_quality == "failed":
        logger.warning("采集失败 %s: %s", asin, snap.errors)
        _touch_checked(competitor_id)
        _finish_run(
            run_id,
            started_at=started_at,
            ok=False,
            data_quality="failed",
            errors=snap.errors or ["未取到任何有效字段"],
        )
        return CollectResult(
            competitor_id=competitor_id,
            asin=asin,
            ok=False,
            data_quality="failed",
            errors=snap.errors or ["未取到任何有效字段"],
        )

    # ---- 落库 + 比对 ----
    result: CollectResult
    with get_session() as s:
        comp = s.get(Competitor, competitor_id)
        if comp is None:
            # 竞品在这两个会话之间被删掉了（极小概率）。此时那条 collect_runs
            # 会保持 ok=0、finished_at=NULL —— 这正是「这一轮没跑完」的应有形态，
            # 数据健康页会把它算成一次失败。
            return CollectResult(competitor_id=competitor_id, asin=asin, ok=False, errors=["竞品已被删除"])

        prev_row = s.execute(
            select(Snapshot)
            .where(Snapshot.competitor_id == competitor_id)
            .order_by(desc(Snapshot.captured_at))
            .limit(1)
        ).scalar_one_or_none()

        new_row = _save_snapshot(s, competitor_id, snap)
        comp.last_checked_at = datetime.now()

        if prev_row is None:
            # 第一条快照没有可比对象，只建立基线，不报信号
            result = CollectResult(
                competitor_id=competitor_id,
                asin=asin,
                ok=True,
                data_quality=snap.data_quality,
                is_first_snapshot=True,
                errors=snap.errors,
            )
        else:
            prev_snap = row_to_unified(prev_row, marketplace)
            engine = DiffEngine(phase=phase, thresholds=thresholds)

            oos_confirmed = is_oos_confirmed(prev_snap)
            drafts = engine.compare(
                prev_snap,
                snap,
                tier=tier,
                my_price=my_price,
                oos_confirmed=oos_confirmed,
            )

            # 信号去重：同一竞品同一类型在冷却窗口内只落一条（窗口来自 notify 组配置）
            drafts = [
                d for d in drafts if not _is_cooling_down(s, competitor_id, d, cooldown=notify_th)
            ]

            _save_signals(s, comp.tenant_id, competitor_id, drafts)

            logger.info(
                "采集 %s | quality=%s | 信号 P0=%d P1=%d P2=%d",
                asin,
                snap.data_quality,
                sum(1 for d in drafts if d.level == "P0"),
                sum(1 for d in drafts if d.level == "P1"),
                sum(1 for d in drafts if d.level == "P2"),
            )

            result = CollectResult(
                competitor_id=competitor_id,
                asin=asin,
                ok=True,
                data_quality=snap.data_quality,
                signals=drafts,
                errors=snap.errors,
            )

    # 日志必须在 with 之外写：SQLite 同一时刻只允许一个写事务，
    # 在未提交的会话里嵌套开第二个 get_session() 会直接 "database is locked"。
    # signal_count 取「本次真正落库的信号数」（已过冷却去重），
    # 否则数据健康页会出现「日志说 5 条、库里只有 1 条」的对不上。
    _finish_run(
        run_id,
        started_at=started_at,
        ok=result.ok,
        data_quality=result.data_quality,
        signal_count=len(result.signals),
        errors=result.errors or None,
    )
    return result


async def acollect_competitor_by_asin(
    tenant_id: str, asin: str, trigger: str = TRIGGER_PANEL
) -> dict:
    """按 ASIN 采集单个竞品，返回 JSON 友好的结构（给 HTTP / 工具层用）。"""
    asin = (asin or "").strip().upper()
    with get_session() as s:
        comp = s.execute(
            select(Competitor).where(
                Competitor.tenant_id == tenant_id, Competitor.asin == asin
            )
        ).scalar_one_or_none()
        if comp is None:
            return {"ok": False, "error": f"{asin} 不在池中"}
        cid = comp.id

    res = await acollect_competitor(cid, trigger=trigger)
    return {
        "ok": res.ok,
        "asin": res.asin,
        "data_quality": res.data_quality,
        "is_first_snapshot": res.is_first_snapshot,
        "signal_count": len(res.signals),
        "signals": [d.to_row() for d in res.signals],
        "errors": res.errors,
    }


async def collect_all(tenant_id: str, trigger: str = TRIGGER_SCHEDULE) -> list[CollectResult]:
    """采集该租户下所有 active 竞品。并发 3 个，别把 bridge 打挂。

    只捞 `active`：所以 `paused`（老板点了「停」）与 `archived` 天然不会被采集，
    不需要在采集逻辑里写任何判断。
    """
    with get_session() as s:
        ids = [
            c.id
            for c in s.execute(
                select(Competitor).where(
                    Competitor.tenant_id == tenant_id,
                    Competitor.status == "active",
                )
            ).scalars()
        ]

    sem = asyncio.Semaphore(3)

    async def _one(cid: int) -> CollectResult:
        async with sem:
            try:
                return await acollect_competitor(cid, trigger=trigger)
            except Exception as exc:  # noqa: BLE001
                logger.exception("采集异常 competitor_id=%s", cid)
                return CollectResult(
                    competitor_id=cid, asin="", ok=False, errors=[f"{type(exc).__name__}: {exc}"]
                )

    return await asyncio.gather(*(_one(i) for i in ids))


# ============================================================
# 落库
# ============================================================


def _save_snapshot(s: Session, competitor_id: int, snap: UnifiedSnapshot) -> Snapshot:
    row = Snapshot(
        competitor_id=competitor_id,
        captured_at=snap.captured_at,
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
        content_hash=snap.title,          # 用标题做轻量指纹，够用
        # 上架日期：不写这一行，`A 级黑马`判据（上架 90 天内 + 月销过千）就只在
        # 「采完立刻判」那一次生效，**任何一次从库里重算都会变回 B**——
        # 周维护、面板改阈值后重算、demo 重算全走那条路，而且全程不报错。
        # 只截 `[:10]`：列是 VARCHAR(10)，SQLite 不强制长度，但一旦迁到 PG，
        # 带时分秒的字符串会被截断或报错；而所有消费方（tiering）本来就只取前 10 位。
        # None 就是 None，不补空串：空串会让「这行有没有值」永远回答"有"。
        release_date=(snap.release_date or "").strip()[:10] or None,
        # 分层判断要用的两个维度。空列表存 None 而不是 []：
        # 「没采到」（None）和「采到了但确实为空」在排查时是两件事，
        # 而 JSON 列的 `[]` 会被读成一个合法的空结果。这里两侧都当"无数据"处理，
        # 但库里保留区别，出问题时能看出是哪一种。
        category_path=snap.category_path or None,
        keywords=snap.keywords or None,
        # 必须落库，理由见 `models.Snapshot.traffic_keywords_total`：
        # 不存的话「采集时判」和「从库里重算」会用两套判据，结论可能不同且不报错。
        traffic_keywords_total=snap.traffic_keywords_total,
        data_quality=snap.data_quality,
        source=snap.source,
    )
    s.add(row)
    s.flush()
    return row


def _save_signals(s: Session, tenant_id: str, competitor_id: int, drafts: list[SignalDraft]) -> None:
    for d in drafts:
        s.add(
            Signal(
                tenant_id=tenant_id,
                competitor_id=competitor_id,
                category=d.category,
                signal_type=d.signal_type,
                level=d.level,
                before_value=d.before_value,
                after_value=d.after_value,
                change_pct=d.change_pct,
                description=d.description,
                detected_at=datetime.now(),
            )
        )


# ============================================================
# 冷却去重
# ============================================================

# 默认冷却窗口（降级为「默认值来源」：运行时优先用 notify 组配置，见 _is_cooling_down）。
# 保留常量是为了 NotifyThresholds 的默认值可以被测试逐键锁死。
COOLDOWN_HOURS = {"P0": 4, "P1": 12, "P2": 48}


def _cooldown_window(cooldown, level: str) -> int:
    """取某等级的冷却小时数。cooldown 可为 None / NotifyThresholds / {level: hours}。"""
    if cooldown is None:
        return COOLDOWN_HOURS.get(level, 24)
    if hasattr(cooldown, "cooldown_hours"):
        return int(cooldown.cooldown_hours(level))
    return int(dict(cooldown).get(level, COOLDOWN_HOURS.get(level, 24)))


def _is_cooling_down(
    s: Session, competitor_id: int, draft: SignalDraft, cooldown=None
) -> bool:
    """同竞品同信号类型在冷却窗口内是否已经报过。

    cooldown 不传就用默认窗口，所以老调用点行为一字不变；
    传了 `NotifyThresholds` 就按老板配的节奏去重。

    **边界语义：恰好等于窗口长度 → 仍算冷却中（抑制）**，即 `>=`。
    这是一个"宁可少报一次"的选择：多打扰一次会让人开始忽略推送，
    少报一次的代价只是这条变化进日报而不是进 P1 预警。
    试算侧 `simulator._count_after_cooldown` 必须用同一个口径（`<=`），
    否则试算给的次数会比线上少——而试算的全部意义就是"改之前先看会怎样"。
    """
    window = _cooldown_window(cooldown, draft.level)
    if window <= 0:
        return False
    since = datetime.now() - timedelta(hours=window)
    exists = s.execute(
        select(Signal.id)
        .where(
            Signal.competitor_id == competitor_id,
            Signal.signal_type == draft.signal_type,
            Signal.detected_at >= since,
        )
        .limit(1)
    ).scalar_one_or_none()
    return exists is not None


# ============================================================
# 采集日志（collect_runs）
# ============================================================


def _finish_run(
    run_id: int,
    *,
    started_at: datetime,
    ok: bool,
    data_quality: Optional[str],
    signal_count: int = 0,
    errors: Optional[list] = None,
) -> None:
    """收尾一条采集日志。**永不抛异常**。

    为什么吞掉这里的异常：它跑在采集的返回路径上，如果写日志失败反过来
    把一次成功的采集变成异常，就成了「观测手段打挂了业务」。所以只记 warning。
    """
    finished_at = datetime.now()
    duration_ms = int((finished_at - started_at).total_seconds() * 1000)
    try:
        with get_session() as s:
            run = s.get(CollectRun, run_id)
            if run is None:
                return
            run.finished_at = finished_at
            run.ok = ok
            run.data_quality = data_quality
            run.signal_count = signal_count
            run.errors = errors
            # 0 会让「这一轮到底跑没跑」变成歧义（0ms 与没跑分不清），下限取 1
            run.duration_ms = max(1, duration_ms)
    except Exception:  # noqa: BLE001
        logger.warning("写采集日志失败 run_id=%s", run_id, exc_info=True)


# ============================================================
# 辅助
# ============================================================


def _touch_checked(competitor_id: int) -> None:
    with get_session() as s:
        comp = s.get(Competitor, competitor_id)
        if comp:
            comp.last_checked_at = datetime.now()


def is_oos_confirmed(prev: UnifiedSnapshot) -> bool:
    """断货二次确认：**上一次**的采集结果是否也不在售。

    单次采集到 OOS 可能只是数据源抖动，或者 listing 短暂下架。
    只有在售状态连续两次为负，才按「真断货」处理（措辞上区分「已确认」/「首次发现」）。

    刻意做成同步纯函数：试算（simulator）要在内存里对历史快照跑同一段判定，
    如果它需要 await，试算就得自己开事件循环，那等于把同一段逻辑写两遍。
    """
    return prev.in_stock is False or (prev.stock_hint or "").upper() == "OOS"


def row_to_unified(row: Snapshot, marketplace: str = "US") -> UnifiedSnapshot:
    """把数据库快照还原成统一模型，用于和最新采集做对比。

    关于 price_source：net_price 只可能由 promotions 接口写入，
    所以「有 net_price」等价于「当轮促销接口成功」。用这个等价关系反推，
    不必给快照表加列。

    关于 keywords / category_path：这两列是后补的（老库里是 NULL），
    所以这里必须容忍 None。**不要**把 None 补成 `[]` 之后再存回去——
    那会把"从没采到过"写成"采到了但为空"，而这正好是本项目最忌讳的那种混淆。

    关于 release_date：同样是后补的列（老库里是 NULL），也必须容忍 None。
    它一还不上，`A 级黑马`判据（上架 90 天内 + 月销过千）就从「重算」这条路上
    静默消失——恢复时也一样，None 保持 None，不许补成空串。
    """
    return UnifiedSnapshot(
        asin="",
        marketplace=marketplace,
        price=row.price,
        coupon_value=row.coupon_value,
        net_price=row.net_price,
        price_source="promotion" if row.net_price is not None else None,
        deal_type=row.deal_type,
        rank_main=row.rank_main,
        rank_sub=row.rank_sub,
        rank_label=row.rank_label,
        monthly_units=row.monthly_units,
        monthly_revenue=row.monthly_revenue,
        rating=row.rating,
        review_count=row.review_count,
        in_stock=row.in_stock,
        stock_hint=row.stock_hint,
        buybox_owner=row.buybox_owner,
        title=row.title,
        image_url=row.image_url,
        release_date=row.release_date,
        category_path=list(row.category_path or []),
        keywords=list(row.keywords or []),
        # 抽样守卫的第二道判据（抽样不到总量 1/3）。不还原它，
        # 这条路径上的守卫就只剩「条数 < 50」，两条路的结论会分叉。
        traffic_keywords_total=row.traffic_keywords_total,
        data_quality=row.data_quality,
        source=row.source,
        captured_at=row.captured_at,
    )


def resolve_season_phase(s: Session, my_product: Optional[MyProduct]) -> str:
    """当前处于哪个季节阶段：warmup / blast / cooldown，默认 warmup。"""
    if my_product is None:
        return "warmup"
    today_md = datetime.now().strftime("%m-%d")
    rows = list(
        s.execute(
            select(SeasonCalendar).where(SeasonCalendar.tenant_id == my_product.tenant_id)
        ).scalars()
    )
    for r in rows:
        if r.start_md <= today_md <= r.end_md:
            return r.phase
    return "warmup"
