"""L4 · 写入类能力（13 个）。

写入类能力的唯一共同点是**会有副作用**，因此这里比查询类多守两条规矩：

1. **写操作必须能回答「谁做的」**：`ctx.user_id` / `ctx.source` 会被显式下传到
   `action_log` / `binding`，绝不依赖下游自己猜当前用户。
2. **失败要给人话 + 出路**：返回 `{"ok": false, "error": ..., "hint": ...}`，
   `hint` 写"怎么办"而不是重复一遍"哪里错了"。

业务判断仍然不在这里：比如 `add_competitor` 到底判 S 还是 A、`set_competitor_status`
改成 paused 之后哪些定时任务会跳过，都是 L3 的事。本层只负责把参数递下去。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from sqlalchemy import select

from app.capabilities.types import (
    CapContext,
    Capability,
    GROUP_WRITE,
    ParamSpec,
    SCOPE_SHARED,
    T_BOOL,
    T_ENUM,
    T_FLOAT,
    T_INT,
    T_JSON,
    T_STR,
)
from app.db import get_session
from app.domain import baseline as baseline_mod
from app.domain.thresholds import GROUPS
from app.models import Competitor, SeasonCalendar
from app.services import (
    action_log,
    binding,
    collector,
    identity,
    pool,
    settings_store,
)

logger = logging.getLogger(__name__)


def _p(*args, **kwargs) -> ParamSpec:
    return ParamSpec(*args, **kwargs)


def _competitor_id(tenant_id: str, asin: str) -> Optional[int]:
    asin = (asin or "").strip().upper()
    if not asin:
        return None
    with get_session() as s:
        row = s.execute(
            select(Competitor.id).where(
                Competitor.tenant_id == tenant_id, Competitor.asin == asin
            )
        ).scalar_one_or_none()
    return int(row) if row is not None else None


def _operator_name(ctx: CapContext) -> Optional[str]:
    """把 user_id 翻成姓名。

    为什么留痕存姓名而不是 id：半年后回看「这版阈值是谁改的」，
    没人记得 id=3 是谁。`identity.resolve_current_user` 解析不出就回落默认用户，
    所以这里不会因为身份缺失而让一次保存失败。
    """
    if ctx.user_id is None:
        return None
    return identity.resolve_current_user(ctx.user_id, ctx.tenant_id).get("name")


# ============================================================
# ① add_competitor
# ============================================================


def add_competitor(ctx: CapContext, asin: str, force: bool = False) -> dict:
    """入池。四同法则 + 分层 + 建基线 + 自动建绑定，全部在 L3 的 `pool` 里完成。

    `force=True` 时未通过四同法则也入池，但分层结果会是 `unknown`（证据不足）
    或最低观察级——不是硬塞成 C 级（见止血补丁：拿不到证据 ≠ 证据表明不成立）。

    `ctx.user_id` 必须原样递到 L3：绑定表要记的是「**谁**在盯这个竞品」，
    告警按人分发完全依赖这一列。以前这里没传，`binding.upsert()` 就用默认用户兜底，
    于是所有竞品都被记成默认负责人加的——那不是「没接线」，是记了一个假的人。
    `ctx.user_id` 为 `None`（不认识的平台身份）是允许的，由下游兜底，本层不报错。
    """
    return pool.add_competitor(ctx.tenant_id, asin, force=force, user_id=ctx.user_id)


# ============================================================
# ② archive_competitor
# ============================================================


def archive_competitor(ctx: CapContext, asin: str, reason: str = "") -> dict:
    return pool.archive_competitor(ctx.tenant_id, asin, reason)


# ============================================================
# ③ set_competitor_status
# ============================================================


def set_competitor_status(ctx: CapContext, asin: str, status: str) -> dict:
    """改状态。面板上的「停」= `paused`（停止采集但保留历史），不是删除。"""
    return pool.set_competitor_status(ctx.tenant_id, asin, status)


# ============================================================
# ④ set_baseline
# ============================================================


def set_baseline(ctx: CapContext, **kwargs) -> dict:
    """只改传了的字段，其他保持原样（稀疏更新，不做整行覆盖）。

    先挡一道"锚点还不存在且没给 asin"：`MyProduct.asin` 是 NOT NULL，
    直接走到 L3 会撞出一条 `IntegrityError: NOT NULL constraint failed`，
    而那句话对用户毫无意义。校验放在这里属于参数完整性检查，不是业务判断。

    `source` / `assumed_fields` 是本轮新增的两个参数，用来回答
    「这个锚点里的数哪些是查来的、哪些是拍的」。它们**必须同时出现在
    `CAPABILITIES` 的 params 里**：`_coerce_params` 只认声明过的键，
    未声明的会被静默丢掉——传了也白传，且没有任何报错。
    """
    fields = {k: v for k, v in (kwargs or {}).items() if v is not None}
    if not fields.get("asin") and pool.get_my_product(ctx.tenant_id) is None:
        return {
            "ok": False,
            "error": "还没有自家产品锚点，第一次设置必须给 asin",
            "hint": "例如 {\"asin\": \"B0XXXXXXXX\", \"current_price\": 29.99, "
                    "\"break_even_price\": 19.5, \"daily_sales_avg\": 12}",
        }
    # 形状校验放在这里而不是靠 T_JSON：T_JSON 只保证"是合法 JSON"，
    # 而这两种值一旦形状不对，坏的不是这次调用，是**以后每一次分层判断**
    # （比如 category_path 存成字符串，`node_match` 会静默变成 None，
    # 整池重新变成"待判定"，而没有任何一处会报错）。
    path = fields.get("category_path")
    if path is not None:
        if not isinstance(path, list) or not all(
            isinstance(x, str) and x.strip() for x in path
        ):
            return {
                "ok": False,
                "error": "category_path 必须是由非空字符串组成的列表",
                "error_kind": "param",
                "hint": '例如 ["Toys & Games", "Toy Vehicles", "Play Trucks"]，'
                        "从一级类目到最细节点依次写。",
            }
        fields["category_path"] = [x.strip() for x in path]

    kws = fields.get("core_keywords")
    if kws is not None:
        if not isinstance(kws, dict) or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in kws.values()
        ):
            return {
                "ok": False,
                "error": "core_keywords 必须是 {词: 流量占比} 的对象",
                "error_kind": "param",
                "hint": '例如 {"toy car": 0.31, "diecast car": 0.22}；'
                        "占比填 0~1 的小数，或 0~100 的百分数（会自动归一）。",
            }
        # 有人习惯填 31 表示 31%。两种情况都接受并归一成 0~1，
        # 否则权重会被放大 100 倍，词重合度直接爆掉。
        fields["core_keywords"] = {
            str(k): (float(v) / 100.0 if float(v) > 1 else float(v))
            for k, v in kws.items()
        }

    # 溯源两列。形状/取值不对当场拒，理由和 category_path 一样：
    # 坏值进了库不会报错，只会在**以后每一次体检**里安静地少说一句
    # 「这些字段是假设的」——而那句话正是这件事的全部目的。
    src = fields.get("source")
    if src is not None:
        src = str(src).strip().lower()
        if src not in baseline_mod.ANCHOR_SOURCES:
            return {
                "ok": False,
                "error": f"source 只能是 {'/'.join(baseline_mod.ANCHOR_SOURCES)}，"
                         f"收到 {src!r}",
                "error_kind": "param",
                "hint": "manual=手工填；sellersprite_proxy=用卖家精灵反推（pilot 期）；"
                        "sp_api=SP-API 同步。拿不准就填 manual。",
            }
        fields["source"] = src

    assumed = fields.get("assumed_fields")
    if assumed is not None:
        try:
            fields["assumed_fields"] = baseline_mod.coerce_assumed_fields(assumed)
        except ValueError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "error_kind": "param",
                "hint": baseline_mod.ASSUMED_FIELDS_HINT,
            }

    res = pool.update_baseline(ctx.tenant_id, **fields)
    if isinstance(res, dict):
        res.setdefault("ok", True)
        return res
    return {"ok": True, "result": res}


# ============================================================
# ⑤ set_season_phase
# ============================================================


def set_season_phase(
    ctx: CapContext,
    category: str,
    phase: str,
    start_md: str,
    end_md: str,
) -> dict:
    with get_session() as s:
        row = SeasonCalendar(
            tenant_id=ctx.tenant_id,
            category=category or "default",
            phase=phase or "blast",
            start_md=start_md or "11-01",
            end_md=end_md or "12-31",
            interval="15m" if phase == "blast" else "1h",
        )
        s.add(row)
        s.flush()
        return {
            "ok": True,
            "category": row.category,
            "phase": row.phase,
            "window": f"{row.start_md} ~ {row.end_md}",
            "effect": (
                "旺季期间采集间隔缩短到 15 分钟，信号阈值收紧"
                if row.phase == "blast"
                else "该时段按常规频率采集"
            ),
        }


# ============================================================
# ⑥ collect_now
# ============================================================


def collect_now(ctx: CapContext, asin: Optional[str] = None) -> dict:
    """立刻采集一次。`trigger` 由 `ctx.source` 决定（面板 / Agent / 定时各不相同）。

    为什么 trigger 一定要传下去：`collect_runs` 是排查"哪一轮为什么失败"的唯一线索，
    如果人工触发的采集也记成 `schedule`，健康页会把一次手动重试算成定时任务的失败。
    """
    trigger = ctx.trigger
    asin = (asin or "").strip().upper()

    if asin:
        cid = _competitor_id(ctx.tenant_id, asin)
        if cid is None:
            return {"ok": False, "error": f"{asin} 不在池中"}
        res = collector.collect_competitor(cid, trigger=trigger)
        return {
            "ok": res.ok,
            "asin": res.asin,
            "trigger": trigger,
            "data_quality": res.data_quality,
            "signal_count": len(res.signals),
            "signals": [s.to_row() for s in res.signals],
            "errors": res.errors,
        }

    results = asyncio.run(collector.collect_all(ctx.tenant_id, trigger=trigger))
    return {
        "ok": True,
        "trigger": trigger,
        "competitors": len(results),
        "success": sum(1 for r in results if r.ok),
        "signals": sum(len(r.signals) for r in results),
        "p0": sum(r.p0_count for r in results),
        "details": [
            {
                "asin": r.asin,
                "ok": r.ok,
                "quality": r.data_quality,
                "signals": len(r.signals),
                "errors": r.errors[:3],
            }
            for r in results
        ],
    }


# ============================================================
# ⑦ record_action
# ============================================================


def record_action(
    ctx: CapContext,
    action_type: str,
    asin: Optional[str] = None,
    description: str = "",
    reason: Optional[str] = None,
    signal_id: Optional[int] = None,
    verify_days: Optional[int] = None,
) -> dict:
    """记一条动作。身份与来源都从 `ctx` 取——这是「身份显式携带」的关键收益：
    同一个人在飞书里点按钮和在面板上点，会落成两条 `source` 不同但 `user_id` 相同的记录。
    """
    cid = None
    if asin:
        cid = _competitor_id(ctx.tenant_id, asin)
        if cid is None:
            return {
                "ok": False,
                "error": f"{asin} 不在池中",
                "hint": "先用 add_competitor 入池，或检查 ASIN 是否写错",
            }

    return action_log.record(
        ctx.tenant_id,
        action_type=action_type,
        asin=(asin or "").strip().upper() or None,
        competitor_id=cid,
        description=description,
        reason=reason,
        signal_id=signal_id,
        verify_days=verify_days,
        user_id=ctx.user_id,
        source=ctx.source,
    )


# ============================================================
# ⑦b review_action（复盘）
# ============================================================


def review_action(
    ctx: CapContext,
    action_id: int,
    verdict: str,
    note: Optional[str] = None,
) -> dict:
    """给一条到期动作填复盘结论。

    ## 为什么这个能力不叫 `judge_xxx`

    项目里的 `judge_*` 一律指「L2 纯函数的自动判断」。复盘**不是自动判断**：
    系统只负责把动作前后的指标并排放好，有效/无效由人填。
    名字混用会让后来的人以为系统能自己判有效性，然后去找那个「判据」——
    而它不存在，也不该存在（同一个降价，跟价时是威胁、断货时是机会）。

    ## 为什么 `verdict` 只有三个值

    有效 / 无效 / 判不了。刻意不加「部分有效」：加了之后人会一律选中间，
    复盘就退化成「每件事都有点效果」，等于没复盘。
    「判不了」是**必须存在**的第三个值——数据不够时逼人二选一，
    他会随手选一个，于是库里多了一条假结论。
    """
    return action_log.review_action(
        ctx.tenant_id,
        action_id=action_id,
        verdict=verdict,
        note=note,
        user_id=ctx.user_id,
    )


# ============================================================
# ⑧ handle_action_callback
# ============================================================


def handle_action_callback(
    ctx: CapContext,
    token: str,
    choice: str,
    note: Optional[str] = None,
    operator_external_id: Optional[str] = None,
) -> dict:
    """飞书卡片按钮回调。未配置 `ACTION_TOKEN_SECRET` 时 **fail-closed** 直接拒绝。

    本轮不联调卡片渲染，但校验与写库路径现在就立起来——
    校验逻辑一旦拖到联调时再写，就永远会有人想"先跳过校验跑通再说"。
    """
    return action_log.handle_callback(
        token,
        choice,
        note=note,
        operator_external_id=operator_external_id,
        tenant_id=ctx.tenant_id,
    )


# ============================================================
# ⑨ save_thresholds
# ============================================================


def save_thresholds(
    ctx: CapContext,
    scope: str,
    group_name: str,
    values: Any,
    scope_id: Optional[str] = None,
    note: str = "",
) -> dict:
    """保存一组稀疏覆盖值。未提交的键保持原样，不会被冲掉。

    返回值带 `warnings`（非阻断提示，例如"P1 门槛比 P0 还高"）：
    配置层只负责提醒后果，不替老板做决定。
    """
    if not isinstance(values, dict):
        return {
            "ok": False,
            "error": "values 必须是对象，例如 {\"price_p0_pct\": 0.05}",
            "hint": "面板整页提交时可以是 {\"diff\": {...}, \"tiering\": {...}}",
        }

    flat = values
    if any(k in GROUPS for k in values.keys()):
        nested = values.get(group_name)
        if isinstance(nested, dict):
            flat = nested
        else:
            flat = {k: v for k, v in values.items() if k not in GROUPS}

    return settings_store.save(
        scope=scope,
        group_name=group_name,
        values=flat,
        scope_id=str(scope_id) if scope_id not in (None, "") else None,
        tenant_id=ctx.tenant_id,
        operator=_operator_name(ctx),
        note=note,
    )


# ============================================================
# ⑩ rollback_thresholds
# ============================================================


def rollback_thresholds(
    ctx: CapContext,
    scope: str,
    group_name: str,
    version: int,
    scope_id: Optional[str] = None,
    note: str = "",
) -> dict:
    """回滚 = **追加一条新版本**，历史不动。这样"谁在什么时候把它改回去了"有痕迹。"""
    return settings_store.rollback(
        scope=scope,
        group_name=group_name,
        version=int(version),
        scope_id=str(scope_id) if scope_id not in (None, "") else None,
        tenant_id=ctx.tenant_id,
        operator=_operator_name(ctx),
        note=note or f"回滚到 v{version}",
    )


# ============================================================
# ⑪ upsert_binding
# ============================================================


def upsert_binding(
    ctx: CapContext,
    competitor_id: int,
    my_product_id: int,
    user_id: Optional[int] = None,
    confidence: Optional[float] = None,
) -> dict:
    """手动建立/更新绑定。`user_id` 不传则用本次调用携带的身份。"""
    return binding.upsert(
        competitor_id=int(competitor_id),
        my_product_id=int(my_product_id),
        user_id=int(user_id) if user_id is not None else ctx.user_id,
        tenant_id=ctx.tenant_id,
        source=binding.SOURCE_MANUAL,
        confidence=confidence,
    )


# ============================================================
# ⑫ rebuild_binding_basis
# ============================================================


def rebuild_binding_basis(ctx: CapContext, competitor_id: Optional[int] = None) -> dict:
    """用**最近一次分层结果**重算 basis（不重新跑四同法则）。

    为什么不重跑：重跑等于把"当初为什么这么判"换成"今天会怎么判"，
    那就失去了快照的意义。要重判请走池子维护任务。
    """
    return binding.rebuild_basis(
        ctx.tenant_id, competitor_id=competitor_id, user_id=ctx.user_id
    )


# ============================================================
# ⑬ upsert_identity
# ============================================================


def upsert_identity(
    ctx: CapContext,
    user_id: int,
    channel: str,
    external_id: str,
) -> dict:
    """绑定平台身份。同一身份已被别人占用时**拒绝**，不静默改绑。"""
    return identity.upsert_identity(
        int(user_id), channel, external_id, tenant_id=ctx.tenant_id
    )


# ============================================================
# 注册表
# ============================================================


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        name="add_competitor",
        scope=SCOPE_SHARED,
        group=GROUP_WRITE,
        title="加入竞品池",
        description="拉数据、跑四同法则、判分层、建基线快照，并自动建立对标绑定。",
        params=(
            _p("asin", "竞品 ASIN", T_STR, required=True),
            _p("force", "强行入池", T_BOOL, default=False,
               help="未通过四同法则也加，但分层会是 unknown（证据不足），不会硬判成 C"),
        ),
        handler=add_competitor,
    ),
    Capability(
        name="archive_competitor",
        scope=SCOPE_SHARED,
        group=GROUP_WRITE,
        title="移出竞品池",
        description="移出监控池但保留历史（不是物理删除）。",
        params=(
            _p("asin", "竞品 ASIN", T_STR, required=True),
            _p("reason", "归档原因", T_STR, help="便于日后回看"),
        ),
        handler=archive_competitor,
    ),
    Capability(
        name="set_competitor_status",
        scope=SCOPE_SHARED,
        group=GROUP_WRITE,
        title="改竞品状态",
        description="active=在盯 / paused=停止采集但保留历史 / archived=归档。",
        params=(
            _p("asin", "竞品 ASIN", T_STR, required=True),
            _p("status", "目标状态", T_ENUM, required=True,
               enum=list(pool.VALID_STATUSES)),
        ),
        handler=set_competitor_status,
    ),
    Capability(
        name="set_baseline",
        scope=SCOPE_SHARED,
        group=GROUP_WRITE,
        title="设置自家锚点",
        description="设置或更新自家产品的锚点数据；只传要改的字段，其他不变。",
        params=(
            _p("asin", "自家 ASIN", T_STR, help="首次设置时必填"),
            _p("current_price", "当前售价", T_FLOAT, minimum=0, unit="美元"),
            _p("break_even_price", "保本价", T_FLOAT, minimum=0, unit="美元",
               help="跌破就不该跟"),
            _p("target_margin_rate", "目标毛利率", T_FLOAT, minimum=0, maximum=1,
               unit="比例", help="填了它可自动推算保本价"),
            _p("daily_sales_avg", "日均销量", T_FLOAT, minimum=0, unit="单"),
            _p("fba_available_units", "FBA 可售库存", T_INT, minimum=0, unit="件"),
            _p("lead_time_days", "补货周期", T_INT, minimum=0, unit="天"),
            _p("sub_category_rank", "子类目 BSR", T_INT, minimum=1),
            _p("rating", "星级", T_FLOAT, minimum=0, maximum=5),
            _p("review_count", "评论数", T_INT, minimum=0),
            _p(
                "core_keywords", "核心出单词", T_JSON,
                help='{"词": 流量占比} 形式，例如 {"toy car": 0.31, "diecast car": 0.22}。'
                     "分层判断的「流量池」维度靠它算加权词重合度；不填这维度就判不了",
            ),
            _p(
                "category_path", "自家类目路径", T_JSON,
                help='从根到叶的列表，例如 ["Toys & Games", "Toy Vehicles", "Play Trucks"]。'
                     "分层判断的「节点」维度只认它",
            ),
            # Listing 三项：五大维度里的「Listing 基线」靠它们。
            # 之前只声明到 category_path 为止，于是这一整维在能力层是**只读**的
            # ——面板能看见标题、改不了标题，而 `assess()` 照样把这一维算进就绪度。
            # 「能看不能改」比不显示更糟：人会以为改不了是因为不值得改。
            _p("title", "标题", T_STR, help="对手抄你 listing 时，这是比对基准"),
            _p("image_url", "主图链接", T_STR),
            _p(
                "bullet_points", "五点描述", T_JSON,
                help="字符串列表，例如 [\"第一条\", \"第二条\"]",
            ),
            # 下面两个是「这个锚点能不能拿去决策」的开关。
            # 不声明它们的话，`_coerce_params` 会把传进来的值静默丢掉
            # （只认 params 里声明过的键），于是库里的 source 永远是默认值，
            # 而面板照旧把假设值当真值显示——事情看着"做完了"，实际一步都没通。
            _p(
                "source", "锚点来源", T_ENUM, enum=list(baseline_mod.ANCHOR_SOURCES),
                help="manual=手工填 / sellersprite_proxy=卖家精灵反推（pilot 期）/ "
                     "sp_api=SP-API 同步。决定面板上这个锚点该被信到什么程度",
            ),
            _p(
                "assumed_fields", "假设值字段清单", T_JSON,
                help='哪些字段是假设值而不是查到的，例如 '
                     '["break_even_price", "fba_available_units"]。'
                     "填了它，锚点体检会明确告警「哪些结论不可信」；不填等于声称全是实测",
            ),
        ),
        handler=set_baseline,
    ),
    Capability(
        name="set_season_phase",
        scope=SCOPE_SHARED,
        group=GROUP_WRITE,
        title="设置季节日历",
        description="旺季提高采集频率并收紧阈值，淡季相反。",
        params=(
            _p("category", "类目名", T_STR, required=True, help="如 toys"),
            _p("phase", "季节阶段", T_ENUM, required=True,
               enum=["warmup", "blast", "cooldown"],
               help="储备期 / 爆发期 / 退潮期"),
            _p("start_md", "开始日期", T_STR, required=True, help="MM-DD，如 11-20"),
            _p("end_md", "结束日期", T_STR, required=True, help="MM-DD，如 12-20"),
        ),
        handler=set_season_phase,
    ),
    Capability(
        name="collect_now",
        scope=SCOPE_SHARED,
        group=GROUP_WRITE,
        title="立刻采集",
        description="不等定时任务，马上采一次。不传 asin 则采全部（较慢）。",
        params=(
            _p("asin", "只采这个 ASIN", T_STR, help="不传则采全部"),
        ),
        handler=collect_now,
    ),
    Capability(
        name="record_action",
        scope=SCOPE_SHARED,
        group=GROUP_WRITE,
        title="记录动作",
        description="记下「我对这个竞品做了什么」，7 天后可回来验证效果。",
        params=(
            _p("asin", "针对哪个竞品", T_STR, help="不传则不挂竞品（如自家运营动作）"),
            _p("action_type", "动作类型", T_STR, required=True,
               help="如 price_follow / coupon / ads_shift"),
            _p("description", "具体做了什么", T_STR,
               help="写清楚到别人能复现的程度，如「用 Coupon 让到 26.99」"),
            _p("reason", "依据", T_STR, help="不写依据的动作，事后无法复盘"),
            _p("signal_id", "依据哪条信号", T_INT, minimum=1),
            _p("verify_days", "几天后验证", T_INT, minimum=1, maximum=90, default=7,
               unit="天"),
        ),
        handler=record_action,
    ),
    Capability(
        name="review_action",
        scope=SCOPE_SHARED,
        group=GROUP_WRITE,
        title="复盘一条动作",
        description="到期动作回填结论：有效 / 无效 / 判不了，并留存当时的对比指标与备注。",
        params=(
            _p("action_id", "动作 ID", T_INT, required=True, minimum=1,
               help="从 get_due_reviews 或 list_actions 里拿"),
            _p("verdict", "复盘结论", T_ENUM, required=True,
               enum=list(action_log.VERDICTS),
               help="有效=达到目的；无效=没达到；判不了=数据不足（这也是结论，别硬填）"),
            _p("note", "复盘备注", T_STR,
               help="比结论值钱：「有效，但对手那周断货了」能说明因果"),
        ),
        handler=review_action,
    ),
    Capability(
        name="handle_action_callback",
        scope=SCOPE_SHARED,
        group=GROUP_WRITE,
        title="动作回调",
        description="飞书卡片按钮回调：校验 token 后记一条动作。",
        params=(
            _p("token", "动作令牌", T_STR, required=True,
               help="格式 signal_id.action_type.签名"),
            _p("choice", "用户选择", T_ENUM, required=True,
               enum=["agree", "disagree", "ignore"]),
            _p("note", "补充说明", T_STR),
            _p("operator_external_id", "操作人平台 ID", T_STR,
               help="飞书 open_id，用来把动作记到正确的人名下"),
        ),
        handler=handle_action_callback,
    ),
    Capability(
        name="save_thresholds",
        scope=SCOPE_SHARED,
        group=GROUP_WRITE,
        title="保存阈值",
        description="写入一组稀疏覆盖值；只覆盖提交的键，未提交的保持原样。",
        params=(
            _p("scope", "作用域", T_ENUM, required=True,
               enum=list(settings_store.SCOPE_ORDER),
               help="company 公司默认 / user 个人覆盖 / product 产品覆盖"),
            _p("group_name", "阈值分组", T_ENUM, required=True, enum=list(GROUPS)),
            _p("values", "要改的键值", T_JSON, required=True,
               help="如 {\"price_p0_pct\": 0.05}"),
            _p("scope_id", "作用域 ID", T_STR,
               help="company 层留空自动取 tenant；user 填用户 id；product 填产品 id"),
            _p("note", "变更说明", T_STR, help="写在版本历史里，给人看"),
        ),
        handler=save_thresholds,
    ),
    Capability(
        name="rollback_thresholds",
        scope=SCOPE_SHARED,
        group=GROUP_WRITE,
        title="回滚阈值",
        description="回到历史某一版；结果是追加一条新版本，历史不动。",
        params=(
            _p("scope", "作用域", T_ENUM, required=True,
               enum=list(settings_store.SCOPE_ORDER)),
            _p("group_name", "阈值分组", T_ENUM, required=True, enum=list(GROUPS)),
            _p("version", "目标版本号", T_INT, required=True, minimum=1),
            _p("scope_id", "作用域 ID", T_STR),
            _p("note", "回滚说明", T_STR),
        ),
        handler=rollback_thresholds,
    ),
    Capability(
        name="upsert_binding",
        scope=SCOPE_SHARED,
        group=GROUP_WRITE,
        title="建立/更新绑定",
        description="手动指定「这个竞品对我哪个自家产品」，一般由分层自动完成。",
        params=(
            _p("competitor_id", "竞品 ID", T_INT, required=True, minimum=1),
            _p("my_product_id", "自家产品 ID", T_INT, required=True, minimum=1),
            _p("user_id", "归属哪个用户", T_INT, minimum=1,
               help="不传则用本次调用携带的身份"),
            _p("confidence", "置信度", T_FLOAT, minimum=0, maximum=1),
        ),
        handler=upsert_binding,
    ),
    Capability(
        name="rebuild_binding_basis",
        scope=SCOPE_SHARED,
        group=GROUP_WRITE,
        title="重算绑定依据",
        description="用最近一次分层结果重算 basis（不重新跑判定），用于补历史数据。",
        params=(
            _p("competitor_id", "只重算某个竞品", T_INT, minimum=1,
               help="不传则全量重算"),
        ),
        handler=rebuild_binding_basis,
    ),
    Capability(
        name="upsert_identity",
        scope=SCOPE_SHARED,
        group=GROUP_WRITE,
        title="绑定平台身份",
        description="把飞书 open_id / 企微 userid 映射到系统用户；已被占用则拒绝。",
        params=(
            _p("user_id", "系统用户 ID", T_INT, required=True, minimum=1),
            _p("channel", "渠道", T_ENUM, required=True,
               enum=list(identity.CHANNELS)),
            _p("external_id", "渠道内 ID", T_STR, required=True),
        ),
        handler=upsert_identity,
    ),
)
