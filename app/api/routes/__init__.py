"""L4 · REST 路由汇总。

`server.py` 只 `app.include_router(api_router)` 一行，所有业务路由都挂在这里。

## 为什么按「视图」而不是按「资源」拆文件

`config.py` / `identity.py` / `capabilities.py` 对应的是**三个使用场景**
（阈值配置页、用户与绑定、通用能力分发），不是三张表的 CRUD。
按资源拆的结果是「保存阈值」和「试算阈值」分散在两个文件里，
而它们共享同一份生效值解析逻辑——分开了就一定会有一边忘了改。

## 路由顺序

`capabilities` 放最前：它的路径是 `/api/cap/{name}`，
如果把带通配段的模块放前面，将来加一个 `/api/cap/xxx` 的固定路径
就会被 `{name}` 抢先匹配（FastAPI 按注册顺序匹配）。
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.routes.capabilities import router as capabilities_router
from app.api.routes.config import router as config_router
from app.api.routes.identity import router as identity_router
from app.api.routes.panel import router as panel_router

api_router = APIRouter()
api_router.include_router(capabilities_router)
api_router.include_router(config_router)
api_router.include_router(identity_router)
api_router.include_router(panel_router)

# 这里是**唯一**的注册点：`server.py` 只 `include_router(api_router)` 一行。
# 别在 `server.py` 里再 include 一次，否则同一路由会注册两遍（OpenAPI 里会出现重复项）。

__all__ = ["api_router"]
