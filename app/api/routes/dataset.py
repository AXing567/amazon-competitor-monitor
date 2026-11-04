"""REST · 数据集端点（`/api/dataset/*`）。

## 它解决的是「这套数据算不算数」

系统有两个物理隔离的库：真实库（`database_url`）与 Mock 库（`mock_database_url`）。
面板顶部要能回答三件事，否则用户不知道自己看到的数字该不该信：

1. 我现在在看哪个库？
2. 另一个库里有没有东西？
3. Mock 数据没了怎么重建？

这三个问题都是一个端点回答，而不是散在各视图里——「当前模式」是**全局状态**，
每个视图各自去猜就会出现"看板说 Mock、池子说真实"的分裂。

## 为什么重建要在这里同步做

造 9 个竞品 + 23 条快照 + 走一遍 DiffEngine 大约 2~4 秒。放到后台任务里
就要再做一个"进度查询"，而这是一个**人主动点的、会等结果的**操作
（点完就想看页面变化）。同步做，前端按钮转圈，回来就刷新——最少的活动部件。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.db import (
    DATA_MODES,
    MODE_LABELS,
    db_file_path,
    ensure_mode_ready,
    get_data_mode,
    get_session,
    url_for_mode,
    using_data_mode,
)
from app.models import Competitor, MyProduct, Snapshot
from app.services import mock_data

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dataset"])


def _json(payload: Any) -> JSONResponse:
    return JSONResponse(content=jsonable_encoder(payload))


def _mode_summary(mode: str) -> dict:
    """一个模式「有没有东西可看」的摘要。失败也返回 200（见模块文档）。"""
    path = db_file_path(mode=mode)
    info: dict[str, Any] = {
        "mode": mode,
        "label": MODE_LABELS[mode],
        "db_file": str(path) if path else url_for_mode(mode),
        "db_exists": bool(path and path.exists()),
        "ready": False,
        "my_asin": None,
        "competitors": 0,
        "snapshots": 0,
        "last_capture": None,
        "error": None,
    }
    try:
        ensure_mode_ready(mode)
        with using_data_mode(mode):
            with get_session() as s:
                my = s.execute(select(MyProduct).limit(1)).scalar_one_or_none()
                info["my_asin"] = my.asin if my else None
                info["ready"] = my is not None
                info["competitors"] = int(
                    s.execute(select(func.count()).select_from(Competitor)).scalar() or 0
                )
                info["snapshots"] = int(
                    s.execute(select(func.count()).select_from(Snapshot)).scalar() or 0
                )
                last = s.execute(select(func.max(Snapshot.captured_at))).scalar()
                info["last_capture"] = (
                    last.isoformat(sep=" ", timespec="seconds") if last else None
                )
    except Exception as exc:                     # noqa: BLE001
        # 一个库坏了不该让整个顶部条打不开：把错误收进它自己那一格。
        logger.warning("读取 %s 模式摘要失败：%s", mode, exc)
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


@router.get("/api/dataset")
async def api_dataset() -> Any:
    """当前模式 + 两个模式的概况。面板顶部条与「自家基线」页共用这一份。"""
    current = get_data_mode()
    return _json(
        {
            "ok": True,
            "current": current,
            "current_label": MODE_LABELS.get(current, current),
            "modes": [_mode_summary(m) for m in DATA_MODES],
        }
    )


@router.post("/api/dataset/mock/rebuild")
async def api_rebuild_mock() -> Any:
    """重建 Mock 数据集（先清空再重造）。

    故意**不需要**请求头带 `X-Data-Mode: mock`：这个动作的语义就是"造 Mock 库"，
    它自己在 mock 上下文里跑完再回来。要求调用方先切模式的话，
    在真实模式下点这个按钮会变成一个必须解释的 403。
    """
    try:
        result = await run_in_threadpool(mock_data.build, True)
    except mock_data.MockDataError as exc:
        return _json(
            {
                "ok": False,
                "error": str(exc),
                "hint": "Mock 数据自检没通过。看服务端日志里那条「场景名写的是 X，"
                        "引擎算出来是 Y」——那是数据没调好，不是引擎坏了。",
            }
        )
    except Exception as exc:                     # noqa: BLE001
        logger.exception("重建 Mock 数据失败")
        return _json(
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "hint": "看服务端日志。若提示两个库指向同一个文件，"
                        "检查 .env 的 DATABASE_URL 与 MOCK_DATABASE_URL。",
            }
        )
    return _json({"ok": True, **result})


@router.post("/api/dataset/mock/clear")
async def api_clear_mock(
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> Any:
    """清空 Mock 库（只删行，表结构保留）。"""
    try:
        result = await run_in_threadpool(mock_data.clear, True)
    except mock_data.MockDataError as exc:
        return _json({"ok": False, "error": str(exc)})
    return _json(result)
