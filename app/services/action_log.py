"""L3 · 动作记录 + 飞书按钮回调 token。

## 为什么 `actions` 表要加那 5 列

它建了但一直零使用。真要用起来，缺的不是「动作是什么」（`action_type` 有），
而是**「谁、针对谁、依据什么、从哪来」**——10 个人共用一套系统时，
没有 `user_id` 就是一笔糊涂账；没有 `signal_id` 就说不清「你当时为什么这么做」。

## 回调为什么 fail-closed

`POST /api/actions/callback` 能被外部（飞书卡片按钮）触发。如果 `ACTION_TOKEN_SECRET`
没配就跳过校验，这个端点等于「任何人都能伪造一条『老板已决定跟价』」。
一个能被随意伪造的写端点，比没有这个端点危险得多。所以：**密钥为空 → 直接拒绝**。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import desc, select

from app.config import settings
from app.db import get_session
from app.models import Action, Competitor, MyProduct, Signal, Snapshot, User
from app.services import binding, decision_log, identity

logger = logging.getLogger(__name__)

#: 渠道取值，写进 actions.source。
SOURCES = ("panel", "feishu_card", "agent", "system")

#: 没填文字依据时的占位。宁可写一句「未填写」，也不要留 NULL——
#: 面板上「依据」这一列有一半是空的，会让整张表看起来像没设计过。
NO_REASON = "（未填写依据）"

TOKEN_EMPTY_SECRET = "未配置回调密钥"

#: 复盘结论。三个值刻意**不含**「部分有效」：
#: 三态已经够用，加中间态之后人会一律选中间，复盘就失去信息量。
VERDICT_EFFECTIVE = "effective"
VERDICT_INEFFECTIVE = "ineffective"
VERDICT_INCONCLUSIVE = "inconclusive"
VERDICTS: tuple[str, ...] = (
    VERDICT_EFFECTIVE,
    VERDICT_INEFFECTIVE,
    VERDICT_INCONCLUSIVE,
)
VERDICT_LABELS: dict[str, str] = {
    VERDICT_EFFECTIVE: "有效",
    VERDICT_INEFFECTIVE: "无效",
    VERDICT_INCONCLUSIVE: "判不了",
}

#: 动作类型的中文名。与面板下拉的候选保持一致；面板可以自由填新类型，
#: 未收录的照原样显示（见 views/actions.py 的说明）。
ACTION_LABELS: dict[str, str] = {
    "price_follow": "跟价",
    "price_hold": "不跟价（守住）",
    "price_raise": "提价",
    "price_cut": "降价",
    "stock_alert": "备货 / 补货",
    "listing_optimize": "改 listing",
    "coupon_test": "加券 / 试券",
    "ad_shift": "调广告",
    "no_action": "观察不动",
}

SOURCE_LABELS: dict[str, str] = {
    "panel": "面板手工",
    "feishu_card": "飞书卡片",
    "agent": "Agent 代录",
    "system": "系统",
}

# ============================================================
# 指标快照：复盘的前后对比靠这两份
# ============================================================
#
# ## 为什么只挑这几个指标
#
# 挑的标准是**「动作会改变它，而且它能被独立观测」**：
#
# - 对手价 / 有货：跟价、抢流量的直接对象；
# - 对手 BSR / 星级 / 评价数：对手有没有被你的动作影响；
# - 我的售价 / 日均销量 / FBA 库存 / 类目排名：你自己的结果。
#
# 刻意**不**把「我的转化率」「广告 ACOS」放进来：它们不在这套系统里，
# 放进来只会得到一串 None，而 None 在复盘表上会被读成「没有变化」。
#
# ## 为什么只给 kind 不给格式化后的字符串
#
# 后端拼 `$19.99`、前端再 money() 一次 → `$$19.99`。这个错只有截图能发现。
# 所以这里给的是**值 + 类型**，谁来展示谁格式化（面板用 JS、日报用 Python）。
# 见 ARCHITECTURE「值类型由 L2 说、格式由 L5 定」。

COMPETITOR_METRICS: dict[str, tuple[str, str]] = {
    "price": ("对手价", "money"),
    "rank_main": ("对手 BSR", "rank"),
    "rating": ("对手星级", "rating"),
    "review_count": ("对手评价数", "int"),
    "in_stock": ("对手有货", "bool"),
}

MY_METRICS: dict[str, tuple[str, str]] = {
    "current_price": ("我的售价", "money"),
    "daily_sales_avg": ("我的日均销量", "float1"),
    "fba_available_units": ("我的 FBA 库存", "int"),
    "sub_category_rank": ("我的类目排名", "rank"),
}


def _capture_metrics(
    s, *, competitor_id: Optional[int], my_product_id: Optional[int]
) -> dict:
    """抓一份「此刻」的指标。返回 `{"at", "competitor", "mine", "mine_updated_at"}`。

    自家那半边的数**只在人手动更新锚点时才会变**，所以顺手把 `updated_at`
    一起记下来：复盘时如果发现 `mine` 前后一模一样，得能分辨
    「确实没变」和「这半个月根本没人更新过锚点」——
    后者意味着这份复盘没有依据，而不是「动作无效」。
    """
    competitor: dict = {}
    mine: dict = {}
    mine_updated: Optional[str] = None

    if competitor_id is not None:
        last = s.execute(
            select(Snapshot)
            .where(Snapshot.competitor_id == int(competitor_id))
            .order_by(desc(Snapshot.captured_at))
            .limit(1)
        ).scalar_one_or_none()
        if last is not None:
            for key in COMPETITOR_METRICS:
                competitor[key] = getattr(last, key, None)
            competitor["captured_at"] = (
                last.captured_at.isoformat(timespec="seconds")
                if last.captured_at
                else None
            )

    if my_product_id is not None:
        my = s.get(MyProduct, int(my_product_id))
        if my is not None:
            for key in MY_METRICS:
                mine[key] = getattr(my, key, None)
            if my.updated_at:
                mine_updated = my.updated_at.isoformat(timespec="seconds")

    return {
        "at": datetime.now().isoformat(timespec="seconds"),
        "competitor": competitor,
        "mine": mine,
        "mine_updated_at": mine_updated,
    }


def diff_metrics(before: Optional[dict], after: Optional[dict]) -> list[dict]:
    """两份指标差成人话可读的结构化变化。**只算差值，不判好坏。**

    为什么不判：同一个「对手价降了 10%」，在你要跟价时是威胁、
    在他断货时是机会。把「好/坏」写进这里，等于把业务判断塞进数据层，
    然后面板上那句「对手降价了（不利）」会在断货场景下说反话。

    无用项直接不出现在结果里（两边都没有、或压根没变），
    而不是给一条「无变化」——复盘表上一堆「无变化」会淹没真正动了的那两行。
    """
    changes: list[dict] = []
    before = before or {}
    after = after or {}

    for group, labels in (("competitor", COMPETITOR_METRICS), ("mine", MY_METRICS)):
        b_grp = before.get(group) or {}
        a_grp = after.get(group) or {}
        for key, (label, kind) in labels.items():
            if key not in b_grp and key not in a_grp:
                continue
            b = b_grp.get(key)
            a = a_grp.get(key)
            if b is None and a is None:
                continue
            delta = None
            delta_pct = None
            if isinstance(b, (int, float)) and isinstance(a, (int, float)):
                if not isinstance(b, bool) and not isinstance(a, bool):
                    delta = round(a - b, 4)
                    if b:
                        delta_pct = round((a - b) / abs(b) * 100, 1)
            if b == a:
                continue
            changes.append(
                {
                    "group": group,
                    "key": key,
                    "label": label,
                    "kind": kind,
                    "before": b,
                    "after": a,
                    "delta": delta,
                    "delta_pct": delta_pct,
                }
            )
    return changes


#: 前后对比的三种状态。**不能合并成「有变化 / 没变化」两态**，见 `compare_metrics`。
STATE_CHANGED = "changed"
STATE_UNCHANGED = "unchanged"
STATE_NO_DATA = "no_data"

COMPARISON_LABELS = {
    STATE_CHANGED: "指标有变化",
    STATE_UNCHANGED: "指标没有任何变化",
    STATE_NO_DATA: "没有可比数据",
}


def compare_metrics(before: Optional[dict], now: Optional[dict]) -> dict:
    """把「差了多少」和「有没有的比」分成两件事说清楚。

    ## 为什么这一层必须存在

    `diff_metrics()` 返回空列表有两种完全不同的原因：

    1. **两边都有数，只是没变** → 这是个结论：动作之后指标一点没动，
       通常说明动作没起作用，或者变化还没传导到这些指标上；
    2. **有一边没数** → 这不是结论，是缺数据。该去补采集/补锚点。

    把两者都渲染成「无变化」，等于让第 2 种情况**冒充**第 1 种。
    而复盘时的默认读法就是「没变化 = 白做了」，于是一次漏采会被记成一次失败的动作，
    然后那条经验被写进下次的策略里。这类错误不报错，只让判断慢慢变差。

    所以用三态：`changed` / `unchanged` / `no_data`。

    ## `mine_stale` 为什么单独标出来

    自家那半边（售价、日均销量、库存）只在人手动更新锚点时才会变。
    如果锚点自动作以来**一次都没更新过**，那「我的日均销量没变」这句话
    不说明任何事——它是没更新，不是没变化。不标出来的话，
    复盘表会给人一种「系统在跟踪我的销量」的错觉。
    """
    before = before or {}
    now = now or {}
    changes = diff_metrics(before, now)

    before_has = bool(before.get("competitor") or before.get("mine"))
    now_has = bool(now.get("competitor") or now.get("mine"))

    if not before_has or not now_has:
        state = STATE_NO_DATA
    elif changes:
        state = STATE_CHANGED
    else:
        state = STATE_UNCHANGED

    b_updated = before.get("mine_updated_at")
    n_updated = now.get("mine_updated_at")
    mine_stale = bool(b_updated and n_updated and b_updated == n_updated)

    return {
        "state": state,
        "state_label": COMPARISON_LABELS[state],
        "changes": changes,
        # 锚点自那次动作以来没动过 → 自家那半边的数不能当证据
        "mine_stale": mine_stale,
        "baseline_at": before.get("at"),
    }


# ============================================================
# token 签名 / 校验
# ============================================================


def sign_action_token(
    signal_id: int, action_type: str, secret: Optional[str] = None
) -> str:
    """`{signal_id}.{action_type}.{hmac16}`。

    把 signal_id 与 action_type 一起签进去：光签 signal_id 的话，
    同一张卡片上的「同意跟价」和「不同意」会拿到同一个 token，
    按钮就互相能顶替了。
    """
    key = _secret(secret)
    if not key:
        raise ValueError(TOKEN_EMPTY_SECRET)
    payload = f"{int(signal_id)}.{action_type}"
    digest = hmac.new(key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{digest[:16]}"


def verify_action_token(token: str, secret: Optional[str] = None) -> tuple[bool, str]:
    """返回 `(是否通过, 失败原因)`。**永不抛异常**（端点层不该因为脏 token 500）。"""
    key = _secret(secret)
    if not key:
        # fail-closed：不是「跳过校验」，是「拒绝服务」
        return False, TOKEN_EMPTY_SECRET

    parts = (token or "").strip().split(".")
    if len(parts) != 3:
        return False, "token 格式不正确"
    signal_id, action_type, digest = parts
    if not signal_id.isdigit() or not action_type or not digest:
        return False, "token 格式不正确"

    payload = f"{signal_id}.{action_type}"
    expected = hmac.new(key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(expected, digest):
        return False, "签名不匹配"
    return True, ""


def _secret(secret: Optional[str]) -> str:
    return (secret if secret is not None else settings.action_token_secret) or ""


# ============================================================
# 记录动作
# ============================================================


def record(
    tenant_id: Optional[str] = None,
    *,
    action_type: str,
    asin: Optional[str] = None,
    competitor_id: Optional[int] = None,
    description: str = "",
    reason: Optional[str] = None,
    signal_id: Optional[int] = None,
    verify_days: Optional[int] = None,
    user_id: Optional[int] = None,
    my_product_id: Optional[int] = None,
    source: str = "panel",
) -> dict:
    """记一条动作。返回 `{"ok", "action_id", "verify_due_at", "unbound_warning"}`。

    「无法确定自家产品」时**返回 ok:false 而不是写 NULL**：
    `actions.my_product_id` 是验证环节的主键（7 天后要拿它比指标），
    写 NULL 等于埋一颗「验证时才炸」的雷。
    """
    tenant_id = tenant_id or settings.tenant_id
    action_type = (action_type or "").strip()
    if not action_type:
        return {"ok": False, "error": "action_type 不能为空"}
    if source not in SOURCES:
        return {"ok": False, "error": f"未知来源 {source!r}（可选：{'/'.join(SOURCES)}）"}

    asin = (asin or "").strip().upper() or None

    # ---- 第一步：只读，把所有需要的东西解析出来 ----
    # 为什么不在一个会话里做完全部事：`identity.resolve_current_user` 与
    # `binding.resolve_my_product` 内部各自开会话。在「已经读过数据、还没提交」的
    # 会话里嵌套开第二个写会话，SQLite(WAL) 会出现读事务升级写事务时
    # 撞上别人刚提交的快照，报 database is locked。这类错只在空库首启时出现，
    # 上线后极难复现——所以结构上就避开，而不是靠重试。
    with get_session() as s:
        comp = None
        if competitor_id is not None:
            comp = s.get(Competitor, int(competitor_id))
            if comp is None:
                return {"ok": False, "error": f"竞品 #{competitor_id} 不存在"}
        elif asin:
            comp = s.execute(
                select(Competitor).where(
                    Competitor.tenant_id == tenant_id, Competitor.asin == asin
                )
            ).scalar_one_or_none()
            if comp is None:
                return {
                    "ok": False,
                    "error": f"{asin} 不在池中",
                    "hint": "先把竞品加进池子，再记录针对它的动作",
                }

        cid = comp.id if comp is not None else None
        resolved_asin = comp.asin if comp is not None else asin

        # 依据：优先用信号原文（机器写的），其次人工填的，最后占位
        sig = s.get(Signal, int(signal_id)) if signal_id is not None else None
        if signal_id is not None and sig is None:
            return {"ok": False, "error": f"信号 #{signal_id} 不存在"}
        signal_desc = sig.description if sig is not None else None

    user = identity.resolve_current_user(user_id, tenant_id)
    uid = int(user["id"])
    reason_text = (reason or "").strip() or signal_desc or NO_REASON

    target_my = (
        int(my_product_id)
        if my_product_id is not None
        else binding.resolve_my_product(tenant_id, uid, cid)
    )
    if target_my is None:
        return {
            "ok": False,
            "error": "无法确定自家产品，动作没有记录",
            "hint": "先设置自家产品锚点（scripts/set_baseline.py），或给竞品建一条绑定",
        }

    # 没绑定 == 这条动作用的是「租户默认品」，7 天后验证会拿错基准，必须提醒
    unbound_warning = None
    if cid is not None and binding.resolve_binding(tenant_id, uid, cid) is None:
        unbound_warning = "该竞品还没绑定自家产品，已按租户默认品记录"

    # ---- 第二步：纯写 ----
    now = datetime.now()
    due = now + timedelta(days=int(verify_days)) if verify_days else None

    # 依据的那次判断：动作记下来之后要能点开看「当时系统建议了什么、凭什么」。
    # 在这里查（而不是在写会话里查）是为了让写会话只做写——
    # 混着读会让「读到旧快照」和「写进新事务」出现在同一次往返里，排查时很难分清。
    linked_decision_id = None
    if signal_id is not None:
        found = decision_log.latest_for_signal(tenant_id, int(signal_id))
        linked_decision_id = found["id"] if found else None

    with get_session() as s:
        # 基线指标：**必须在这一刻抓**。晚一天抓，`baseline_metrics` 就已经
        # 包含了动作本身造成的变化，「前后对比」会变成「跟今天比」。
        baseline = _capture_metrics(
            s, competitor_id=cid, my_product_id=int(target_my)
        )
        row = Action(
            tenant_id=tenant_id,
            my_product_id=target_my,
            decision_id=linked_decision_id,
            action_type=action_type,
            description=description or "",
            executed_at=now,
            verify_due_at=due,
            baseline_metrics=baseline,
            user_id=uid,
            competitor_id=cid,
            signal_id=int(signal_id) if signal_id is not None else None,
            reason=reason_text,
            source=source,
        )
        s.add(row)
        s.flush()
        action_id = row.id

    return {
        "ok": True,
        "action_id": action_id,
        "asin": resolved_asin,
        "user": user["name"],
        "verify_due_at": due.isoformat(timespec="seconds") if due else None,
        "unbound_warning": unbound_warning,
        "decision_id": linked_decision_id,
        # 区分两件事，别合成一个布尔：
        #
        # `baseline_available` = 抓到东西了没（给「记下来」的即时反馈看）；
        # `comparable` = 到期时**能不能真的前后对比**。
        #
        # 判据不同的原因：只有竞品快照会随时间自己变。自家锚点是人手填的，
        # 它不构成时间序列——同一个人没改过锚点，那半边的「没变化」不说明任何事。
        # 所以「能不能对比」只看竞品那半边。
        #
        # `comparable` 为 None 表示**不适用**（这条动作压根没挂竞品，比如补货），
        # 而不是「不能对比」。两者混用会让面板对着一条补货动作弹
        # 「该竞品还没采过」，而那条动作根本没有竞品。
        "baseline_available": bool(
            baseline.get("competitor") or baseline.get("mine")
        ),
        "comparable": (bool(baseline.get("competitor")) if cid is not None else None),
    }


def handle_callback(
    token: str,
    choice: str,
    note: Optional[str] = None,
    operator_external_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    channel: str = "feishu",
) -> dict:
    """飞书卡片按钮回调：解析 token → 记一条动作。本轮只做「解析 + 写库」。

    不做卡片按钮渲染（那是下一轮联调的事），但 token 校验与写库路径现在就立起来，
    因为**校验逻辑一旦拖到联调时再写，就永远会有人想「先跳过校验跑通再说」**。
    """
    tenant_id = tenant_id or settings.tenant_id

    ok, msg = verify_action_token(token)
    if not ok:
        if msg == TOKEN_EMPTY_SECRET:
            return {
                "ok": False,
                "error": f"{msg}，回调已拒绝（fail-closed）",
                "hint": "在 .env 里配置 ACTION_TOKEN_SECRET 并重启服务",
            }
        return {"ok": False, "error": f"回调被拒绝：{msg}"}

    signal_id_str, action_type, _digest = token.strip().split(".")
    signal_id = int(signal_id_str)

    with get_session() as s:
        sig = s.get(Signal, signal_id)
        if sig is None:
            return {"ok": False, "error": f"信号 #{signal_id} 不存在，无法记录动作"}
        competitor_id = sig.competitor_id

    operator = None
    if operator_external_id:
        operator = identity.resolve_by_external(channel, operator_external_id, tenant_id)
    user_id = int(operator["id"]) if operator else None

    choice_text = (choice or "").strip() or "unknown"
    result = record(
        tenant_id,
        action_type=action_type,
        competitor_id=competitor_id,
        description=f"飞书卡片回复：{choice_text}",
        reason=(note or "").strip() or f"卡片按钮 {choice_text}",
        signal_id=signal_id,
        user_id=user_id,
        source="feishu_card",
    )
    if not result.get("ok"):
        return result

    resolved = operator["name"] if operator else (result.get("user") or "默认用户")
    result["resolved_user"] = resolved
    result["choice"] = choice_text
    return result


# ============================================================
# 查询
# ============================================================


def _action_row_to_dict(s, row: Action, *, with_metrics: bool = False) -> dict:
    """一行动作的对外形状。`list_actions` 与 `due_reviews` 共用，避免两处字段名分叉。"""
    user = s.get(User, row.user_id) if row.user_id else None
    comp = s.get(Competitor, row.competitor_id) if row.competitor_id else None
    my = s.get(MyProduct, row.my_product_id) if row.my_product_id else None

    due = row.verify_due_at
    overdue_days = None
    if due and not row.verdict:
        overdue_days = max(0, (datetime.now() - due).days)

    out = {
        "id": row.id,
        "at": row.executed_at.isoformat(timespec="seconds") if row.executed_at else None,
        "user_id": row.user_id,
        "user": user.name if user else None,
        "asin": comp.asin if comp else None,
        "my_asin": my.asin if my else None,
        "action_type": row.action_type,
        "action_label": ACTION_LABELS.get(row.action_type, row.action_type),
        "description": row.description,
        "reason": row.reason,
        "source": row.source,
        "source_label": SOURCE_LABELS.get(row.source or "", row.source or ""),
        "verdict": row.verdict,
        "verdict_label": VERDICT_LABELS.get(row.verdict or "", row.verdict or ""),
        "reviewed_at": (
            row.reviewed_at.isoformat(timespec="seconds") if row.reviewed_at else None
        ),
        "review_note": row.review_note,
        "verify_due_at": due.isoformat(timespec="seconds") if due else None,
        "overdue_days": overdue_days,
        "signal_id": row.signal_id,
        "decision_id": row.decision_id,
    }
    if with_metrics:
        out["baseline"] = row.baseline_metrics or {}
        out["result"] = row.result_metrics or {}
    return out


def list_actions(
    tenant_id: Optional[str] = None,
    *,
    days: int = 30,
    asin: Optional[str] = None,
    user_id: Optional[int] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """动作列表（新→旧）。面板视图④与 `get_change_history` 共用。"""
    tenant_id = tenant_id or settings.tenant_id
    since = datetime.now() - timedelta(days=max(1, int(days or 30)))
    page = int(limit or settings.panel_page_size)

    with get_session() as s:
        stmt = (
            select(Action)
            .where(Action.tenant_id == tenant_id, Action.executed_at >= since)
            .order_by(desc(Action.executed_at))
            .limit(max(1, page))
        )
        if user_id is not None:
            stmt = stmt.where(Action.user_id == int(user_id))

        rows = list(s.execute(stmt).scalars())
        out = []
        for row in rows:
            item = _action_row_to_dict(s, row)
            if asin and item["asin"] != (asin or "").strip().upper():
                continue
            out.append(item)
        return out


# ============================================================
# 复盘：到期队列 + 判定
# ============================================================


def due_reviews(
    tenant_id: Optional[str] = None,
    *,
    limit: int = 50,
    include_future_days: int = 0,
) -> list[dict]:
    """到期未复盘的动作（超期最久的排前面）。

    `include_future_days` 留出来是给面板「今天到期 + 提前 1 天预告」用的；
    日报只取超期/当日到期的，不预告——预告会让简报变长，而简报的价值密度
    恰恰来自「只说要动手的事」。

    ## 为什么返回里带 `changes`（前后对比）

    「7 天后回看」这件事，如果只给一行「该复盘 action #12 了」，人是不会动的：
    他还得自己去翻那天的价格、再翻今天的价格，然后才谈得上判断。
    把两张表并排放好，他只需要看一眼前后差了多少——这一步的成本决定复盘会不会发生。

    但**只摆事实、不给「有效/无效」**：那是业务判断，归人。
    系统能说的是「对手价从 19.99 降到 17.99，你的日均销量从 85 到 92」，
    至于这算不算「跟价有效」，取决于当时想解决的是份额还是利润。
    """
    tenant_id = tenant_id or settings.tenant_id
    horizon = datetime.now() + timedelta(days=max(0, int(include_future_days)))

    with get_session() as s:
        stmt = (
            select(Action)
            .where(
                Action.tenant_id == tenant_id,
                Action.verify_due_at.is_not(None),
                Action.verify_due_at <= horizon,
                Action.verdict.is_(None),
            )
            .order_by(Action.verify_due_at)
            .limit(max(1, int(limit)))
        )
        rows = list(s.execute(stmt).scalars())

        out: list[dict] = []
        for row in rows:
            item = _action_row_to_dict(s, row, with_metrics=True)
            # 「现在」这一侧的指标现取，不落库：它是可重算的派生量，
            # 落库会在每次打开面板时写一行，而它下一秒就会过期。
            now_metrics = _capture_metrics(
                s, competitor_id=row.competitor_id, my_product_id=row.my_product_id
            )
            item["now"] = now_metrics
            cmp = compare_metrics(row.baseline_metrics, now_metrics)
            item["comparison"] = cmp
            item["changes"] = cmp["changes"]
            out.append(item)
        return out


def review_action(
    tenant_id: Optional[str] = None,
    *,
    action_id: int,
    verdict: str,
    note: Optional[str] = None,
    user_id: Optional[int] = None,
) -> dict:
    """给一条到期动作填复盘结论。返回带前后对比的结果。

    ## 为什么允许改判（已判过的还能再判一次）

    复盘结论会变。当时看「无效」，一个月后回看发现对手那周在清仓、
    你的跟价其实守住了份额——这时候应该能改。改判留痕在 `reviewed_at`
    上（每次覆盖都会更新时间），而结论本身**不做版本历史**：
    复盘是给人看的结论，不是审计对象；审计对象是「当时的判断」，那在 `decisions` 里。
    """
    tenant_id = tenant_id or settings.tenant_id
    verdict = (verdict or "").strip()
    if verdict not in VERDICTS:
        return {
            "ok": False,
            "error": f"复盘结论只能是 {'/'.join(VERDICTS)}，收到 {verdict!r}",
            "hint": "有效=达到目的；无效=没达到；判不了=数据不足以判断（这也是个结论，别硬填）",
        }

    with get_session() as s:
        row = s.get(Action, int(action_id))
        if row is None:
            return {"ok": False, "error": f"动作 #{action_id} 不存在"}
        if row.tenant_id not in (None, tenant_id):
            return {"ok": False, "error": f"动作 #{action_id} 不属于当前租户"}

        now_metrics = _capture_metrics(
            s, competitor_id=row.competitor_id, my_product_id=row.my_product_id
        )
        row.result_metrics = now_metrics
        row.verdict = verdict
        row.reviewed_at = datetime.now()
        row.review_note = (note or "").strip() or None

        result = _action_row_to_dict(s, row, with_metrics=True)
        cmp = compare_metrics(row.baseline_metrics, now_metrics)
        result["comparison"] = cmp
        result["changes"] = cmp["changes"]
        # 提交前把要返回的东西全算完：`with` 一退出对象就 detached，
        # 之后访问属性会抛 DetachedInstanceError（只在有数据时复现）。
        return {"ok": True, **result}
