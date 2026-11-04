"""REST 层的上下文装配。

**这里是「当前用户」这个概念唯一被解析的地方**，而且解析结果只存在一次请求的局部变量里
（`CapContext`），不写进任何模块级状态。理由见 `app/capabilities/types.py` 的模块文档：
面板上切用户、飞书里谁在说话，如果靠进程内单例传递，多 worker 下必然串号，
而且出问题时没有任何痕迹可以还原「这个请求当时认为是哪个用户」。

`X-User-Id` **不做鉴权**（本轮裁定 #6）：单租户自用，没有登录体系，
这个头的语义是"我想以谁的身份操作"，不是"我是谁"的证明。
真要鉴权，入口应该是反代或登录态，而不是在这个头上加签名——那只会造出
一种看起来安全实际靠自觉的假安全。
"""
from __future__ import annotations

import logging
from typing import Optional

from app.capabilities import CapContext
from app.capabilities.types import SOURCE_PANEL
from app.config import settings
from app.services import identity

logger = logging.getLogger(__name__)


def resolve_user(x_user_id: Optional[str], tenant_id: Optional[str] = None) -> dict:
    """把 `X-User-Id` 解析成一个真实用户。解析不出来回落默认用户，**绝不抛异常**。"""
    return identity.resolve_current_user(x_user_id, tenant_id or settings.tenant_id)


def resolve_ctx(
    x_user_id: Optional[str],
    *,
    source: str = SOURCE_PANEL,
    tenant_id: Optional[str] = None,
) -> CapContext:
    """装配一次调用的上下文。

    `user_id` 一定是个真实存在的用户 id（解析不出来会回落默认用户），
    所以下游不需要再处理 `None`——「动作记录里 user_id 为空」这种账从一开始就不会出现。
    """
    tid = tenant_id or settings.tenant_id
    user = resolve_user(x_user_id, tid)
    return CapContext(tenant_id=tid, user_id=int(user["id"]), source=source)
