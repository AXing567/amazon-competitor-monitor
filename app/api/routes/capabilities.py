"""REST · 通用能力分发（`/api/cap`）。

面板不需要为 33 个能力写 33 个端点：先 `GET /api/cap` 拿到自描述清单，
再 `POST /api/cap/{name}` 带上参数调用。加一个能力时面板不用改代码
（表单由 `ParamSpec` 驱动），这是"能力层"最实际的收益。

## 三个路由级约定

1. **`GET` 返回元数据，`POST` 执行**。`GET /api/cap/list_competitors` 拿到的是
   「有哪些参数」，不是竞品列表——查询也要用 `POST`。
   原因：查询参数里有 JSON 对象（`simulate_thresholds.values`），
   放进 query string 既难看又有长度上限，而"GET 用 query、POST 用 body"
   这一条规则会被前端记成两套代码。统一 POST，前端只有一条路。
2. **handler 一律丢进线程池跑**。能力层的 handler 是同步的，其中 `collect_now`
   内部还会 `asyncio.run`（同步调用异步采集）。直接在事件循环里执行会
   撞上 "asyncio.run() cannot be called from a running event loop"，
   而这类错只在点了"立刻采集"时才出现——单测里点不到的坑，结构上就该避开。
3. **`agent_chat` / `reset_context` 这类 `agent_only` 能力在这里固定 403**。
   模型调用"和模型对话"= 递归调自己，一次预算能烧出无限循环，所以这道门必须有。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.api.deps import resolve_ctx
from app.capabilities import (
    CODE_FORBIDDEN,
    CODE_NOT_FOUND,
    CapabilityError,
    call,
    list_capabilities,
    to_schema,
)
from app.capabilities.types import SCOPES, SOURCE_PANEL

logger = logging.getLogger(__name__)

router = APIRouter(tags=["capabilities"])

_STATUS_BY_CODE = {CODE_NOT_FOUND: 404, CODE_FORBIDDEN: 403}


def _fail(message: str, *, status: int = 200, **extra: Any) -> JSONResponse:
    body: dict = {"ok": False, "error": message}
    body.update(extra)
    return JSONResponse(content=body, status_code=status)


@router.get("/api/cap")
def api_cap_list(scope: Optional[str] = None) -> Any:
    """能力清单（自描述）。面板拿它渲染整张能力表。"""
    if scope is not None and scope not in SCOPES:
        return _fail(f"scope 只能是 {'/'.join(SCOPES)}，收到 {scope!r}")
    items = [cap.to_schema() for cap in list_capabilities(scope=scope)]
    return {"ok": True, "count": len(items), "items": items}


@router.get("/api/cap/{name}")
def api_cap_describe(name: str) -> Any:
    """单个能力的元数据（不含 handler）。"""
    try:
        return {"ok": True, **to_schema(name)}
    except CapabilityError as exc:
        return _fail(str(exc), status=_STATUS_BY_CODE.get(exc.code, 400), **(exc.payload or {}))


@router.post("/api/cap/{name}")
async def api_cap_invoke(
    name: str,
    request: Request,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> Any:
    """执行一个能力。body 就是该能力的参数对象。

    非法 JSON / body 不是对象 / 参数缺失，一律 **200 + `ok:false`**：
    对前端来说"点了一下没成功"是一个结果，不是一次网络异常。
    """
    try:
        raw = await request.json()
    except Exception:  # noqa: BLE001 —— 空 body / 坏 JSON 都走这里
        return _fail("请求体不是合法 JSON", hint="POST 的 body 应该是该能力的参数对象，可为空对象 {}")

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return _fail("请求体必须是 JSON 对象（能力参数表）", received=type(raw).__name__)

    # `ctx` / `name` 是保留字：能力参数里不可能有同名项，
    # 但用户在 body 里多写一个会把 call() 的签名撞掉，所以直接剔除。
    payload = {k: v for k, v in raw.items() if k not in ("ctx", "name")}

    ctx = resolve_ctx(x_user_id, source=SOURCE_PANEL)
    try:
        result = await run_in_threadpool(call, name, ctx=ctx, **payload)
    except CapabilityError as exc:
        return _fail(str(exc), status=_STATUS_BY_CODE.get(exc.code, 400), **(exc.payload or {}))
    except Exception as exc:  # noqa: BLE001 —— 兜底：这个端点永远不许 500
        logger.exception("能力 %s 调用异常", name)
        return _fail(f"{type(exc).__name__}: {exc}", capability=name)

    return JSONResponse(content=jsonable_encoder(result))
