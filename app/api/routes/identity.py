"""REST · 用户 / 身份 / 绑定。

## 两个端点直连 L3（不经能力层），是个刻意的例外

| 端点 | 直连 | 为什么不做成能力 |
|---|---|---|
| `POST /api/users` | `identity.create_user` | 建用户是**运维动作**：它不该出现在模型能调的工具里（模型不该有"给我建个账号"的能力），也不在 33 个能力清单内。设计文档 4.3 有端点、4.2 没有对应能力，按"端点存在、能力不存在"处理。 |
| `DELETE /api/bindings/{id}` | `binding.disable` | 停用绑定是软删，属于同一类运维动作；能力层只提供 `upsert_binding`（正向建立），没有 `disable`。 |

其余端点一律走能力层。之所以敢开这两个例外：它们**不产生业务判断**，
只是把一次简单写入透传下去，不存在"面板和 Agent 各写一份、结论不一致"的风险。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.api.deps import resolve_ctx, resolve_user
from app.capabilities import safe_call
from app.capabilities.types import SOURCE_PANEL
from app.services import binding, identity

logger = logging.getLogger(__name__)

router = APIRouter(tags=["identity"])


def _json(payload: Any) -> JSONResponse:
    return JSONResponse(content=jsonable_encoder(payload))


async def _body(request: Request) -> tuple[Optional[dict], Optional[JSONResponse]]:
    try:
        raw = await request.json()
    except Exception:  # noqa: BLE001
        return None, _json({"ok": False, "error": "请求体不是合法 JSON"})
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return None, _json(
            {"ok": False, "error": "请求体必须是 JSON 对象", "received": type(raw).__name__}
        )
    return raw, None


# ============================================================
# 用户
# ============================================================


@router.get("/api/users")
async def api_users(
    include_identities: Optional[str] = None,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> Any:
    ctx = resolve_ctx(x_user_id, source=SOURCE_PANEL)
    return _json(
        await run_in_threadpool(
            safe_call, "list_users", ctx=ctx, include_identities=include_identities
        )
    )


@router.post("/api/users")
async def api_create_user(
    request: Request,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> Any:
    body, err = await _body(request)
    if err is not None:
        return err
    ctx = resolve_ctx(x_user_id, source=SOURCE_PANEL)
    result = await run_in_threadpool(
        identity.create_user,
        body.get("name") or "",
        body.get("role") or "member",
        ctx.tenant_id,
    )
    return _json(result)


@router.post("/api/users/{user_id}/identities")
async def api_bind_identity(
    user_id: int,
    request: Request,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> Any:
    """把平台身份绑到系统用户。已被别人占用时**拒绝**，不静默改绑。

    为什么拒绝：静默改绑意味着"某天飞书里的老板突然变成小李"，且没有痕迹。
    这种账比"绑不上"难查一百倍。
    """
    body, err = await _body(request)
    if err is not None:
        return err
    ctx = resolve_ctx(x_user_id, source=SOURCE_PANEL)
    return _json(
        await run_in_threadpool(
            safe_call,
            "upsert_identity",
            ctx=ctx,
            user_id=user_id,
            channel=body.get("channel"),
            external_id=body.get("external_id"),
        )
    )


@router.get("/api/session/me")
async def api_session_me(
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> Any:
    """我是谁。面板靠它决定顶部显示哪个名字、动作记录默认记到谁名下。"""
    user = await run_in_threadpool(resolve_user, x_user_id)
    return _json({"ok": True, "user": user, "x_user_id": x_user_id})


# ============================================================
# 绑定
# ============================================================


@router.get("/api/bindings")
async def api_bindings(
    competitor_id: Optional[str] = None,
    user_id: Optional[str] = None,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> Any:
    ctx = resolve_ctx(x_user_id, source=SOURCE_PANEL)
    return _json(
        await run_in_threadpool(
            safe_call,
            "list_bindings",
            ctx=ctx,
            competitor_id=competitor_id,
            user_id=user_id,
        )
    )


@router.post("/api/bindings")
async def api_upsert_binding(
    request: Request,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> Any:
    body, err = await _body(request)
    if err is not None:
        return err
    ctx = resolve_ctx(x_user_id, source=SOURCE_PANEL)
    return _json(
        await run_in_threadpool(
            safe_call,
            "upsert_binding",
            ctx=ctx,
            competitor_id=body.get("competitor_id"),
            my_product_id=body.get("my_product_id"),
            user_id=body.get("user_id"),
            confidence=body.get("confidence"),
        )
    )


@router.post("/api/bindings/rebuild")
async def api_rebuild_bindings(
    request: Request,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> Any:
    """用最近一次分层结果重算 basis。`competitor_id: null` = 全量重建。"""
    body, err = await _body(request)
    if err is not None:
        return err
    ctx = resolve_ctx(x_user_id, source=SOURCE_PANEL)
    return _json(
        await run_in_threadpool(
            safe_call,
            "rebuild_binding_basis",
            ctx=ctx,
            competitor_id=body.get("competitor_id"),
        )
    )


@router.delete("/api/bindings/{binding_id}")
async def api_disable_binding(
    binding_id: int,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> Any:
    """停用绑定（软删）。历史动作仍引用它，所以不能真删。"""
    ctx = resolve_ctx(x_user_id, source=SOURCE_PANEL)
    return _json(await run_in_threadpool(binding.disable, ctx.tenant_id, binding_id))
