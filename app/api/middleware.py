"""数据模式中间件：把请求头 `X-Data-Mode` 钉到本次请求的上下文里。

## 为什么是纯 ASGI 中间件，不是 `@app.middleware("http")`

`ContextVar` 只在**同一个协程上下文**里可靠传播。Starlette 的 `BaseHTTPMiddleware`
（即 `@app.middleware("http")` 的装饰器形式）会在下游外面包一层 task，
某些路径上 ContextVar 的改动传不进去——表现是"请求头明明带了 mock，
端点里读到的却是 live"，而且时有时无。

纯 ASGI 中间件和下游应用在同一个 task 里，`await self.app(...)` 前后就是
同一个上下文，set / reset 必然成对。这条链路上不能有"通常没问题"。

## 非法值为什么是 400 而不是"退回真实模式"

两种退回方向都有害：
- 退回 live：用户以为在看 Mock，实际看的是真实库 → 拿真数据当演示
- 退回 mock：用户以为在看真实数据，实际看的是假数据 → 拿假数据做决定

拼错的头只可能来自手写请求或脚本（面板只送合法值），所以直接拒绝最清楚。
"""
from __future__ import annotations

import logging

from fastapi.responses import JSONResponse

from app.db import (
    DATA_MODES,
    bind_data_mode,
    coerce_data_mode,
    ensure_mode_ready,
    unbind_data_mode,
)

logger = logging.getLogger(__name__)

HEADER = b"x-data-mode"


class DataModeMiddleware:
    """读 `X-Data-Mode` → 钉上下文 → 保证该模式的库已建表 → 处理 → 还原。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        raw = ""
        for key, value in scope.get("headers") or []:
            if key == HEADER:
                raw = value.decode("latin-1")
                break

        try:
            mode = coerce_data_mode(raw)
        except ValueError as exc:
            res = JSONResponse(
                {
                    "ok": False,
                    "error": str(exc),
                    "hint": f"X-Data-Mode 只能是 {' / '.join(DATA_MODES)}；不传等于 live。",
                },
                status_code=400,
            )
            await res(scope, receive, send)
            return

        token = bind_data_mode(mode)
        try:
            # Mock 库很可能是第一次被访问（文件都还没有）。先建表再交给端点，
            # 否则用户看到的是「no such table: my_products」——
            # 一句和「你还没生成 Mock 数据」完全无关的错误。
            # 已经建过的模式会被 `_initialized` 挡住，不重复 create_all。
            ensure_mode_ready(mode)
            await self.app(scope, receive, send)
        finally:
            unbind_data_mode(token)
