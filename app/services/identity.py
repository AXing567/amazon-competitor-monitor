"""L3 · 用户与平台身份映射（最小实现，不做登录系统）。

## 为什么没有登录

单机内网自用。一套登录（密码哈希、会话、过期、找回、改密）的维护成本
远超收益，而身份实际只在两处需要：**「这个动作是谁录的」** 和
**「飞书这条消息是谁发的」**。一个用户表 + 一个 open_id 映射表就够。

## 当前用户怎么确定（无状态方案，不用进程内单例）

1. 面板把选择存 localStorage，每个请求带 `X-User-Id` 头；
2. 服务端 `resolve_current_user(header)`：有头按 id 取，无头取第一个 active 的 owner，
   一个都没有就 `ensure_default_user()` 懒创建一个；
3. 飞书 webhook 拿 `open_id` 查 `user_identities(channel='feishu')`，
   查不到 → 回落默认用户，同时把未知身份记进**进程内**计数（面板数据健康页展示）。

### 为什么不用进程内「当前用户」单例

`uvicorn --workers > 1` 或热重载时进程内状态会分叉，出现「刚切完用户又变回去」
这种鬼故事。localStorage + header 是无状态的，怎么重启都一致。

### 为什么未映射身份只记在内存里

它只是一条排障线索（「有几个飞书号还没绑人」），不是业务数据。
为它建一张表意味着要处理清理、去重、多进程合并——收益不匹配。
代价是重启清零，可以接受；注释写在这里，免得后人以为漏了持久化。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select

from app.config import settings
from app.db import get_session
from app.models import User, UserIdentity

logger = logging.getLogger(__name__)

#: 默认用户名。不做登录，所以第一个用户就是「老板」——他自己。
DEFAULT_USER_NAME = "老板"

#: 支持的身份渠道。加渠道时只需加进这里：resolve_by_external 是通用的。
CHANNELS = ("feishu", "wecom", "dingtalk", "web")

# 未知身份计数：{(channel, external_id): {"channel":.., "external_id":.., "seen_count":.., "last_seen":..}}
_UNMAPPED_SEEN: dict[tuple[str, str], dict[str, Any]] = {}
_UNMAPPED_LOCK = threading.Lock()


# ============================================================
# 查询 / 兜底
# ============================================================


def ensure_default_user(tenant_id: Optional[str] = None) -> dict:
    """确保至少有一个可用的默认用户；已有则原样返回第一个 active 用户。

    幂等。`scripts/init_db.py` 会调它，面板首次打开也会（懒创建），
    所以「动作记录里 user_id 为空」这种账不该出现。
    """
    tenant_id = tenant_id or settings.tenant_id
    with get_session() as s:
        user = _first_active(s, tenant_id)
        if user is None:
            user = User(
                tenant_id=tenant_id,
                name=DEFAULT_USER_NAME,
                role="owner",
                active=True,
                created_at=datetime.now(),
            )
            s.add(user)
            s.flush()
            logger.info("已创建默认用户 #%s %s", user.id, user.name)
        return _user_to_dict(user)


def resolve_current_user(user_id: Any = None, tenant_id: Optional[str] = None) -> dict:
    """把 `X-User-Id`（可能是 str / int / None）解析成一个真实用户。

    解析不出来就回落默认用户，**绝不抛异常**：面板上多一个 500 换不来任何东西，
    而「取不到用户就按默认用户记一笔动作」至少留了痕迹。
    """
    tenant_id = tenant_id or settings.tenant_id
    uid = _to_int(user_id)
    if uid is not None:
        with get_session() as s:
            user = s.get(User, uid)
            if user is not None and user.active:
                return _user_to_dict(user)
        logger.warning("X-User-Id=%s 对应用户不存在或已停用，回落默认用户", user_id)
    return ensure_default_user(tenant_id)


def resolve_by_external(
    channel: str, external_id: str, tenant_id: Optional[str] = None
) -> Optional[dict]:
    """按平台身份反查用户。查不到返回 None（并记一次未映射计数）。"""
    channel = (channel or "").strip().lower()
    external_id = (external_id or "").strip()
    if not channel or not external_id:
        return None

    with get_session() as s:
        row = s.execute(
            select(UserIdentity).where(
                UserIdentity.channel == channel, UserIdentity.external_id == external_id
            )
        ).scalar_one_or_none()
        if row is None:
            _note_unmapped(channel, external_id)
            return None
        user = s.get(User, row.user_id)
        if user is None or not user.active:
            _note_unmapped(channel, external_id)
            return None
        out = _user_to_dict(user)
        out["external_id"] = external_id
        out["channel"] = channel
        return out


def list_users(tenant_id: Optional[str] = None, include_identities: bool = True) -> list[dict]:
    tenant_id = tenant_id or settings.tenant_id
    with get_session() as s:
        users = list(
            s.execute(
                select(User).where(User.tenant_id == tenant_id).order_by(User.id)
            ).scalars()
        )
        out = []
        for u in users:
            item = _user_to_dict(u)
            if include_identities:
                item["identities"] = [
                    {
                        "id": i.id,
                        "channel": i.channel,
                        "external_id": i.external_id,
                        "created_at": _iso(i.created_at),
                    }
                    for i in s.execute(
                        select(UserIdentity).where(UserIdentity.user_id == u.id)
                    ).scalars()
                ]
            out.append(item)
        return out


def default_user_id(tenant_id: Optional[str] = None) -> int:
    return int(ensure_default_user(tenant_id)["id"])


# ============================================================
# 写入
# ============================================================


def create_user(
    name: str, role: str = "member", tenant_id: Optional[str] = None
) -> dict:
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "用户名不能为空"}
    if role not in ("owner", "member"):
        return {"ok": False, "error": f"role 只能是 owner / member，收到 {role!r}"}
    tenant_id = tenant_id or settings.tenant_id

    with get_session() as s:
        user = User(
            tenant_id=tenant_id, name=name, role=role, active=True, created_at=datetime.now()
        )
        s.add(user)
        s.flush()
        return {"ok": True, **_user_to_dict(user)}


def upsert_identity(
    user_id: int, channel: str, external_id: str, tenant_id: Optional[str] = None
) -> dict:
    """绑定平台身份。同一身份已被别人占用时**拒绝**，不静默改绑。

    为什么拒绝而不是覆盖：静默改绑意味着「某天飞书里的老板突然变成小李」，
    而且没有任何痕迹。这种账比「绑不上」难查一百倍。
    """
    channel = (channel or "").strip().lower()
    external_id = (external_id or "").strip()
    if channel not in CHANNELS:
        return {"ok": False, "error": f"未知渠道 {channel!r}（可选：{'/'.join(CHANNELS)}）"}
    if not external_id:
        return {"ok": False, "error": "external_id 不能为空"}

    with get_session() as s:
        user = s.get(User, int(user_id))
        if user is None:
            return {"ok": False, "error": f"用户 #{user_id} 不存在"}

        exists = s.execute(
            select(UserIdentity).where(
                UserIdentity.channel == channel, UserIdentity.external_id == external_id
            )
        ).scalar_one_or_none()
        if exists is not None:
            if exists.user_id == user.id:
                return {"ok": True, "identity_id": exists.id, "unchanged": True}
            return {
                "ok": False,
                "error": f"{channel}:{external_id} 已绑定到用户 #{exists.user_id}，请先解绑",
            }

        row = UserIdentity(
            user_id=user.id,
            channel=channel,
            external_id=external_id,
            created_at=datetime.now(),
        )
        s.add(row)
        s.flush()
        return {"ok": True, "identity_id": row.id, "user_id": user.id}


# ============================================================
# 未映射身份（进程内计数，供数据健康页展示）
# ============================================================


def _note_unmapped(channel: str, external_id: str) -> None:
    key = (channel, external_id)
    with _UNMAPPED_LOCK:
        item = _UNMAPPED_SEEN.setdefault(
            key,
            {"channel": channel, "external_id": external_id, "seen_count": 0, "last_seen": None},
        )
        item["seen_count"] += 1
        item["last_seen"] = datetime.now().isoformat(timespec="seconds")
    logger.info("未映射的平台身份：%s:%s（第 %s 次）", channel, external_id, item["seen_count"])


def unmapped_identities() -> list[dict]:
    """按出现次数降序。只读快照，调用方可以安全修改返回值。"""
    with _UNMAPPED_LOCK:
        items = [dict(v) for v in _UNMAPPED_SEEN.values()]
    items.sort(key=lambda x: x["seen_count"], reverse=True)
    return items


def reset_unmapped() -> None:
    """仅供测试：清空进程内计数。"""
    with _UNMAPPED_LOCK:
        _UNMAPPED_SEEN.clear()


# ============================================================
# 工具
# ============================================================


def _first_active(s, tenant_id: str) -> Optional[User]:
    """优先 owner，其次任意 active 用户。"""
    users = list(
        s.execute(select(User).where(User.tenant_id == tenant_id, User.active.is_(True))).scalars()
    )
    if not users:
        return None
    owners = [u for u in users if u.role == "owner"]
    return (owners or users)[0]


def _user_to_dict(user: User) -> dict:
    return {
        "id": user.id,
        "tenant_id": user.tenant_id,
        "name": user.name,
        "role": user.role,
        "active": bool(user.active),
        "created_at": _iso(user.created_at),
    }


def _to_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None
