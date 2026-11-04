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
from app.models import Action, Competitor, MyProduct, Signal, User
from app.services import binding, identity

logger = logging.getLogger(__name__)

#: 渠道取值，写进 actions.source。
SOURCES = ("panel", "feishu_card", "agent", "system")

#: 没填文字依据时的占位。宁可写一句「未填写」，也不要留 NULL——
#: 面板上「依据」这一列有一半是空的，会让整张表看起来像没设计过。
NO_REASON = "（未填写依据）"

TOKEN_EMPTY_SECRET = "未配置回调密钥"


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

    with get_session() as s:
        row = Action(
            tenant_id=tenant_id,
            my_product_id=target_my,
            decision_id=None,
            action_type=action_type,
            description=description or "",
            executed_at=now,
            verify_due_at=due,
            baseline_metrics=None,
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
            user = s.get(User, row.user_id) if row.user_id else None
            comp = s.get(Competitor, row.competitor_id) if row.competitor_id else None
            my = s.get(MyProduct, row.my_product_id) if row.my_product_id else None
            item_asin = comp.asin if comp else None
            if asin and item_asin != (asin or "").strip().upper():
                continue
            out.append(
                {
                    "id": row.id,
                    "at": row.executed_at.isoformat() if row.executed_at else None,
                    "user_id": row.user_id,
                    "user": user.name if user else None,
                    "asin": item_asin,
                    "my_asin": my.asin if my else None,
                    "action_type": row.action_type,
                    "description": row.description,
                    "reason": row.reason,
                    "source": row.source,
                    "verdict": row.verdict,
                    "verify_due_at": row.verify_due_at.isoformat() if row.verify_due_at else None,
                    "signal_id": row.signal_id,
                }
            )
        return out
