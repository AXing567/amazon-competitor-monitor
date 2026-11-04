"""REST · 阈值配置（7 个端点）。

对应设计文档 4.3「阈值配置（视图②）」。全部走能力层，所以面板、Agent、脚本
看到的是同一份生效值、同一份版本历史。

## 为什么 POST 用原始 body 而不是 pydantic 模型

1. 契约要求"业务失败 = 200 + `ok:false`"，而 pydantic 校验失败会给 422。
   前端于是要处理三种结果（200-ok、200-fail、422），多出来的那种没人会写。
2. 参数是**动态的**：`values` 里有哪些键由 `THRESHOLD_SPEC` 决定，
   写死在模型里等于把 SPEC 抄了第二遍——下次加参数必忘。
3. 真正的校验在 `app/domain/thresholds.validate()`（范围 + 类型），
   错误信息是中文人话，比 pydantic 的 "value is not a valid float" 有用得多。

代价是 OpenAPI 里这几个 POST 没有 body schema。补偿方式是
`GET /api/config/thresholds/schema`——它本身就是给表单用的 schema。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.api.deps import resolve_ctx
from app.capabilities import safe_call
from app.capabilities.types import SOURCE_PANEL

logger = logging.getLogger(__name__)

router = APIRouter(tags=["config"])


def _json(payload: Any) -> JSONResponse:
    """统一出口。**永远 200**：业务失败只是 `ok:false`，不是 HTTP 错误。"""
    return JSONResponse(content=jsonable_encoder(payload))


async def _body(request: Request) -> tuple[Optional[dict], Optional[JSONResponse]]:
    """读 body。失败时返回 (None, 响应)，调用方直接 return 那个响应。

    为什么这两种失败都要带 hint：`/api/panel/*` 和 `/api/cap/*` 的错误都带建议，
    只有这一层不带时，面板上同一个错误卡就会有两种长相——一种有"建议"一段、
    一种只有一行字。用户看不出这是两种问题，只会觉得"有时候它给建议有时候不给"。
    """
    try:
        raw = await request.json()
    except Exception:  # noqa: BLE001
        return None, _json(
            {
                "ok": False,
                "error": "请求体不是合法 JSON",
                "error_kind": "param",
                "hint": "检查是否有尾逗号、单引号、未转义的引号或注释；"
                        "用 JSON.stringify 生成请求体，别手拼字符串。",
            }
        )
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return None, _json(
            {
                "ok": False,
                "error": "请求体必须是 JSON 对象",
                "received": type(raw).__name__,
                "error_kind": "param",
                "hint": f"收到的是 {type(raw).__name__}，需要一个 JSON 对象，例如 "
                        '{"scope":"company","values":{...}}',
            }
        )
    return raw, None


# ============================================================
# 1. schema
# ============================================================


@router.get("/api/config/thresholds/schema")
async def api_thresholds_schema(
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> Any:
    """全部可调参数的中文标签 / 类型 / 默认值 / 范围。面板表单由它驱动。"""
    ctx = resolve_ctx(x_user_id, source=SOURCE_PANEL)
    return _json(await run_in_threadpool(safe_call, "get_threshold_schema", ctx=ctx))


# ============================================================
# 2. 生效值
# ============================================================


def _empty_param(value: Any) -> bool:
    """这个参数值是不是「空」——前端把 null 拼进 URL 时会变成字符串 "null"。"""
    if value is None:
        return True
    return str(value).strip().lower() in ("", "null", "none", "undefined")


def _no_anchor() -> JSONResponse:
    """产品层缺锚点时的统一回复。

    为什么这不算参数错误：`my_product_id=null` 是**面板在空库上的正常行为**——
    它还没有自家产品，所以拿不到 id。把它当参数错误回，用户看到的是
    「my_product_id 需要整数，收到 'null'」——一句正确但没法行动的话。
    真正该说的是"还没有自家数据，去哪填"。
    """
    return _json(
        {
            "ok": False,
            "error": "还没有自家产品锚点，产品层无从覆盖",
            "error_kind": "param",
            "hint": "先在「自家数据」里填一次 ASIN / 售价 / 保本价（或调 POST /api/cap/set_baseline），"
                    "再回来改产品层。公司默认层现在就能改，不用等锚点。",
        }
    )


@router.get("/api/config/thresholds")
async def api_thresholds(
    user_id: Optional[str] = None,
    my_product_id: Optional[str] = None,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> Any:
    """三层合并后的生效值 + 每键来源 + 参与合并的层（含版本号）。

    `user_id` 不传就用本次请求携带的身份（`X-User-Id`）——所以换个人打开这一页，
    看到的阈值就不同，且不需要任何全局状态。
    """
    if my_product_id is not None and _empty_param(my_product_id):
        return _no_anchor()

    ctx = resolve_ctx(x_user_id, source=SOURCE_PANEL)
    return _json(
        await run_in_threadpool(
            safe_call,
            "get_effective_thresholds",
            ctx=ctx,
            user_id=user_id,
            my_product_id=my_product_id,
        )
    )


# ============================================================
# 3. 保存
# ============================================================


@router.post("/api/config/thresholds")
async def api_save_thresholds(
    request: Request,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> Any:
    """保存一组稀疏覆盖值。未提交的键保持原样，不会被冲掉。"""
    body, err = await _body(request)
    if err is not None:
        return err
    ctx = resolve_ctx(x_user_id, source=SOURCE_PANEL)
    result = await run_in_threadpool(
        safe_call,
        "save_thresholds",
        ctx=ctx,
        scope=body.get("scope"),
        scope_id=body.get("scope_id"),
        group_name=body.get("group_name"),
        values=body.get("values"),
        note=body.get("note") or "",
    )
    return _json(result)


# ============================================================
# 4. 版本历史
# ============================================================


@router.get("/api/config/thresholds/versions")
async def api_threshold_versions(
    scope: Optional[str] = None,
    scope_id: Optional[str] = None,
    group_name: Optional[str] = None,
    limit: Optional[str] = None,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> Any:
    """版本历史。`scope` 不传默认 `company`、`group_name` 不传默认 `diff`。

    为什么要给默认值：巡检脚本/curl 试端点时最容易漏参数，而漏了就得到
    `ok:false 缺少必填参数`——看起来像端点坏了。默认值取"最常看的那个作用域"，
    面板自己仍会显式传（它要在两层之间切换）。
    """
    ctx = resolve_ctx(x_user_id, source=SOURCE_PANEL)
    return _json(
        await run_in_threadpool(
            safe_call,
            "list_threshold_versions",
            ctx=ctx,
            scope=(scope or "company").strip().lower(),
            scope_id=scope_id,
            group_name=(group_name or "diff").strip().lower(),
            limit=limit or "20",
        )
    )


# ============================================================
# 5. 回滚
# ============================================================


@router.post("/api/config/thresholds/rollback")
async def api_rollback_thresholds(
    request: Request,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> Any:
    """回滚 = 追加一条新版本，历史不动。"""
    body, err = await _body(request)
    if err is not None:
        return err
    ctx = resolve_ctx(x_user_id, source=SOURCE_PANEL)
    result = await run_in_threadpool(
        safe_call,
        "rollback_thresholds",
        ctx=ctx,
        scope=body.get("scope"),
        scope_id=body.get("scope_id"),
        group_name=body.get("group_name"),
        version=body.get("version"),
        note=body.get("note") or "",
    )
    return _json(result)


# ============================================================
# 6. 试算
# ============================================================


@router.post("/api/config/thresholds/simulate")
async def api_simulate_thresholds(
    request: Request,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> Any:
    """拿历史快照试算候选阈值。**dry-run，零写入**。"""
    body, err = await _body(request)
    if err is not None:
        return err
    if body.get("my_product_id") is not None and _empty_param(body.get("my_product_id")):
        return _no_anchor()
    ctx = resolve_ctx(x_user_id, source=SOURCE_PANEL)
    result = await run_in_threadpool(
        safe_call,
        "simulate_thresholds",
        ctx=ctx,
        values=body.get("values"),
        days=body.get("days", 7),
        asin=body.get("asin"),
        user_id=body.get("user_id"),
        my_product_id=body.get("my_product_id"),
    )
    return _json(result)


# ============================================================
# 7. 人话说明
# ============================================================


@router.get("/api/config/thresholds/explain")
async def api_explain_thresholds(
    group_name: Optional[str] = None,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> Any:
    """把当前生效阈值翻译成一段人话（面板空态/帮助气泡用）。

    `group_name` 不传默认 `diff`——理由同 `/versions`：裸 curl 也应该 200。
    """
    ctx = resolve_ctx(x_user_id, source=SOURCE_PANEL)
    return _json(
        await run_in_threadpool(
            safe_call,
            "explain_thresholds",
            ctx=ctx,
            group_name=(group_name or "diff").strip().lower(),
        )
    )
