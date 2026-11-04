"""REST · 面板数据端点（`/api/panel/*`）。

五个视图各有自己的聚合端点。它们与 `/api/cap/*` 的分工是：

| | `/api/cap/*` | `/api/panel/*` |
|---|---|---|
| 面向 | Agent 与任意调用方 | **只给这一个面板页面** |
| 形态 | 一问一答，一个能力一个语义 | 一屏一次请求，可以合并多个能力的输出 |
| 稳定性 | 契约（改了要通知所有人） | 跟着视图走（改了只影响这一屏） |

**面板端点不写业务判断**：它只做三件事——挑能力、按展示需要合并结果、算"这一屏
要摆哪几个数字"。任何"这个算不算异常""哪个更严重"仍然在 L2/L3。
所以这里没有一处阈值比较，全是 `safe_call` 之后再排序/计数。

## 为什么 GET 全 200 + `ok:false`

与 `routes/config.py` 同一条规矩：前端 `api()` 只认 `ok === true`，
业务失败（比如 asin 不在池中）不是 HTTP 错误。只有"能力不存在/无权限"
（`CapabilityError`）才由 `capabilities.py` 那三个通用端点映射成 404/403，
面板端点的路径是写死的、不会出现"能力不存在"。

## 为什么 `board` 的"近 N 天变化"要去重

它复用 `get_daily_brief`（与飞书日报同一份计算），那一份会**按
（竞品, 信号类型）去重**，只保留最新一条。这正是面板想要的口径：
同一个竞品连着三天降价算"一件事"，不是三件。但也因为如此，
这个数字不能和 `signals` 表行数直接对账——**页面上不写"共 N 次对比"
这类会引发这种对账的文案**（同 `config` 视图的「样本数」）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Header, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.api.deps import resolve_ctx
from app.capabilities import safe_call
from app.capabilities.queries import STATUS_VALUES
from app.capabilities.types import SOURCE_PANEL
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["panel"])


def _json(payload: Any) -> JSONResponse:
    """统一出口。**永远 200**（见模块文档）。"""
    return JSONResponse(content=jsonable_encoder(payload))


async def _body(request: Request) -> tuple[Optional[dict], Optional[JSONResponse]]:
    """读 body。失败时返回 `(None, 响应)`。

    不用 pydantic：面板的请求体就那么几个字段，而 pydantic 校验失败会给 422，
    前端就多了一种要处理的结果（同 `routes/config.py` 的理由）。
    """
    try:
        raw = await request.json()
    except Exception:  # noqa: BLE001
        return None, _json({"ok": False, "error": "请求体不是合法 JSON",
                            "hint": "检查请求头 Content-Type 是否为 application/json"})
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return None, _json({"ok": False, "error": "请求体必须是 JSON 对象",
                            "error_kind": "param",
                            "received": type(raw).__name__,
                            "hint": f"收到的是 {type(raw).__name__}，需要一个 JSON 对象，"
                                    "例如 {\"asin\":\"B0...\",\"status\":\"paused\"}"})
    return raw, None


def _ctx(x_user_id: Optional[str]):
    return resolve_ctx(x_user_id, source=SOURCE_PANEL)


#: 能力层把异常压成 `{ok:false, error:"OperationalError: ... [SQL: SELECT ...]"}`。
#: 那段原文对排查有用、对用户没用（面板上出现一整段 SQL，唯一能做的就是截图发给人）。
#: 所以：**能认出来的错误换成人话**，原文放进 `detail` 由前端折叠显示。
_HUMAN_ERRORS: tuple[tuple[str, str], ...] = (
    ("OperationalError", "数据库访问失败：表结构或数据库文件有问题"),
    ("IntegrityError", "写入被数据库拒绝：违反了一条数据约束"),
    ("ProgrammingError", "SQL 与当前表结构不匹配"),
    ("TimeoutError", "这次查询超时了"),
    ("DetachedInstanceError", "读数据时对象已被回收（服务端缺陷，请报一下）"),
)

_BASE_HINT = (
    "按顺序查：① 服务端日志里有完整堆栈 ② 数据库文件是否可读"
    " ③ 表结构是否与代码一致（`python scripts/init_db.py` 会补表补列，不会删数据）"
)

#: 参数类错误的建议。给错了的"建议"比不给更糟：让用户为一句拼错的 status
#: 去翻数据库文件权限，他会先怀疑整套系统，然后放弃这一页。
_PARAM_HINT = (
    "检查这次请求的参数取值和类型；合法值可以查 "
    "`GET /api/config/thresholds/schema` 或 `GET /api/cap`（能力自描述）。"
)


def _match_error_type(raw: str) -> Optional[str]:
    """错误首行里能认出已知异常类型就返回人话，认不出来返回 `None`。"""
    head = (raw or "").strip().split("\n", 1)[0]
    for key, text in _HUMAN_ERRORS:
        if key in head:
            return text
    return None


def _humanize(raw: str) -> str:
    """把错误翻成人话。认不出来就截断原文——不假装懂。"""
    return _match_error_type(raw) or (
        (raw or "").strip().split("\n", 1)[0][:180] or "未知错误"
    )


def _call(name: str, ctx, **kwargs) -> dict:
    """`safe_call` + 说人话。

    为什么补在这一层而不是能力层：`error` 的原文与 `hint` 的建议都是**面向这一屏**的，
    同一个错误在 Agent 对话里的建议和在面板上的建议不一样。
    """
    res = safe_call(name, ctx=ctx, **kwargs)
    if res.get("ok") is True:
        return res

    raw = res.get("error") or ""
    out = dict(res)
    human = _match_error_type(raw)
    if human:
        # 认得出异常类型：主文案换人话，原文**一律**留在 detail。
        # 不能因为"这条只有五十个字"就放过它——面板上出现 `[SQL: SELECT ...]`
        # 这件事本身，就说明它不是一句给人看的话，长度与可读性无关。
        out["error"] = human
        out["detail"] = raw
    elif len(raw) > 160 or "\n" in raw:
        # 认不出来但明显是堆栈/长错误：截断当主文案，原文折叠
        out["error"] = _humanize(raw)
        out["detail"] = raw
    if not out.get("hint"):
        # 按错误类别分流：能力层抛的 ParamError 已经在 `error_kind` 里标了自己是
        # 参数问题（`app/capabilities/__init__.py`），别再把系统排查指引盖上去。
        out["hint"] = _PARAM_HINT if out.get("error_kind") == "param" else _BASE_HINT
    return out


# ============================================================
# ① 上下文：这一页要按谁的身份、对哪个自家产品
# ============================================================


@router.get("/api/panel/context")
async def api_panel_context(
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> Any:
    """面板启动时先问一次：当前租户、操作人、自家产品。

    `my_product_id` 是产品层阈值的 `scope_id`。没有它，"产品覆盖"那一层打不开，
    所以这里必须给——而不是让前端去猜/去翻竞品列表。
    """
    ctx = _ctx(x_user_id)
    status = await run_in_threadpool(_call, "get_my_status", ctx)
    return _json(
        {
            "ok": True,
            "tenant_id": ctx.tenant_id,
            "user_id": ctx.user_id,
            "source": ctx.source,
            "my_product_id": status.get("my_product_id"),
            "my_asin": status.get("my_asin"),
            "anchor_exists": bool(status.get("anchor_exists")),
            # 锚点体检结论也带上：面板的"能用性"取决于它，缺了哪个维度要能说清
            "anchor": status.get("anchor") or {},
            "portfolio": status.get("portfolio") or {},
            "undecided": status.get("undecided", 0),
            "last_24h": status.get("last_24h") or {},
            "panel_page_size": settings.panel_page_size,
        }
    )


# ============================================================
# ② 数据看板
# ============================================================


def _brief_items(brief: dict) -> list[dict]:
    """把日报的 P0 / P1 列表拼成一维，供"最近变化"列使用。

    注意键是小写的 `p0` / `p1`（`DailyBrief.to_dict()` 的写法），
    而条目里的 `level` 是大写 `P0`。这两个大小写不统一是历史遗留，
    照着自己以为的写法取值会**静默拿到空列表**——不是报错，只是这一列永远空白。
    """
    out: list[dict] = []
    for key, level in (("p0", "P0"), ("p1", "P1")):
        for it in brief.get(key) or []:
            item = dict(it)
            item.setdefault("level", level)
            out.append(item)
    return out


def _as_dt(value: Any) -> Optional[datetime]:
    """把能力层返回的时间（可能是 datetime，也可能是 isoformat 字符串）统一成 datetime。"""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace(" ", "T"))
        except ValueError:
            return None
    return None


def _build_board(items: list[dict], brief: dict, days: int) -> dict:
    """看板摘要 + 每个竞品一行。

    `items` 来自 `list_competitors`（已含 `battlefield` / `tier_missing`），
    `brief` 来自 `get_daily_brief`（已按竞品+类型去重）。

    这里**只是搬家**：把"哪个竞品最近变了"从信号维度搬到竞品维度，
    不做任何"这个严不严重"的判断（level 是 L2 给的）。
    """
    week = _brief_items(brief)
    by_level = {
        "P0": len(brief.get("p0") or []),
        "P1": len(brief.get("p1") or []),
        "P2": int(brief.get("p2_count") or 0),
    }

    now = datetime.now()
    day_ago = now - timedelta(hours=24)
    p0_24h = p1_24h = 0
    last_change: dict[str, dict] = {}
    for it in week:
        at = _as_dt(it.get("detected_at"))
        level = it.get("level")
        if at is not None and at >= day_ago:
            if level == "P0":
                p0_24h += 1
            elif level == "P1":
                p1_24h += 1
        asin = it.get("asin")
        if not asin:
            continue
        prev = last_change.get(asin)
        # 同一竞品的多条里取"最近的那条"（不是最严重的那条）：
        # 这一列的名字是「最近变化」，答案是时间，不是等级。
        if prev is None or (at is not None and _as_dt(prev.get("at")) is not None
                            and at > _as_dt(prev.get("at"))):
            last_change[asin] = {
                "at": at.isoformat() if at else None,
                "level": level,
                "signal_type": it.get("signal_type"),
                "description": it.get("description"),
            }

    by_tier: dict[str, int] = {}
    rows: list[dict] = []
    for it in items:
        tier = it.get("tier") or "unknown"
        by_tier[tier] = by_tier.get(tier, 0) + 1
        row = dict(it)
        row["last_change"] = last_change.get(it.get("asin"))
        # `title` 在 L3 里已截到 40 字（列表用），面板表格也只需要这么长
        rows.append(row)

    return {
        "summary": {
            "total": len(rows),
            "by_tier": by_tier,
            "undecided": by_tier.get("unknown", 0),
            "week_changes": by_level["P0"] + by_level["P1"] + by_level["P2"],
            "week_by_level": by_level,
            "p0_24h": p0_24h,
            "p1_24h": p1_24h,
            "days": days,
        },
        "items": rows,
    }


@router.get("/api/panel/board")
async def api_panel_board(
    days: int = Query(default=7, ge=1, le=90),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> Any:
    """看板：四个数字卡 + 竞品表（含每个竞品的最近一次变化）。"""
    ctx = _ctx(x_user_id)
    listed = await run_in_threadpool(_call, "list_competitors", ctx)
    if listed.get("ok") is not True:
        return _json(listed)

    brief = await run_in_threadpool(
        _call, "get_daily_brief", ctx, hours=int(days) * 24, include_text=False
    )
    if brief.get("ok") is not True:
        # 日报算不出来时，看板仍然要有表格——摘要降级为空，别让整屏消失
        logger.warning("panel board: 日报计算失败 %s", brief.get("error"))
        brief = {}

    board = _build_board(listed.get("items") or [], brief, int(days))
    return _json({"ok": True, **board})


# ============================================================
# ③ 单个竞品的变化历史（点一行展开）
# ============================================================


@router.get("/api/panel/competitors/{asin}/changes")
async def api_panel_changes(
    asin: str,
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=200, ge=1, le=1000),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> Any:
    """三条时间线：原始快照 / 信号 / 动作。

    快照必须给——信号是 L2 判出来的，判据错了只有原始快照能查。
    """
    ctx = _ctx(x_user_id)
    return _json(
        await run_in_threadpool(
            _call, "get_change_history", ctx, asin=asin, days=days, limit=limit
        )
    )


# ============================================================
# ④ 竞品池管理
# ============================================================


@router.get("/api/panel/pool")
async def api_panel_pool(
    status: Optional[str] = Query(default=None, description="逗号分隔，如 active,paused"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> Any:
    """池子清单，可按状态多选过滤（面板上是几个可点的芯片）。

    能力层的 `status` 只支持单值（Agent 那边够用），面板要"同时看暂停的和在盯的"，
    所以这里把逗号列表拆开、逐个取自然后合并——**这是筛选，不是判断**，
    不需要（也不应该）在能力层再加一个多变体参数。
    """
    ctx = _ctx(x_user_id)
    res = await run_in_threadpool(
        _call, "list_competitors", ctx, include_archived=True
    )
    if res.get("ok") is not True:
        return _json(res)

    wanted = [s.strip().lower() for s in (status or "").split(",") if s.strip()]
    bad = [s for s in wanted if s not in STATUS_VALUES]
    if bad:
        return _json(
            {
                "ok": False,
                "error": f"未知状态 {bad[0]!r}",
                "hint": "可选：" + " / ".join(STATUS_VALUES),
            }
        )

    items = res.get("items") or []
    if wanted:
        items = [it for it in items if (it.get("status") or "") in wanted]

    counts: dict[str, int] = {}
    for it in res.get("items") or []:
        counts[it.get("status") or "unknown"] = counts.get(it.get("status") or "unknown", 0) + 1

    return _json(
        {
            "ok": True,
            "count": len(items),
            "status_filter": wanted,
            # 计数基于**未过滤**的全量，这样芯片上的数字不会因为取消勾选而变
            "counts_by_status": counts,
            "items": items,
        }
    )


@router.post("/api/panel/pool/{asin}/status")
async def api_panel_pool_status(
    asin: str,
    request: Request,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> Any:
    """改状态：active（恢复）/ paused（停采集，保留历史）/ archived（归档）。"""
    body, err = await _body(request)
    if err is not None:
        return err
    ctx = _ctx(x_user_id)
    return _json(
        await run_in_threadpool(
            _call,
            "set_competitor_status",
            ctx,
            asin=asin,
            status=body.get("status"),
        )
    )


# ============================================================
# ⑤ 动作记录
# ============================================================


@router.get("/api/panel/actions")
async def api_panel_actions(
    days: int = Query(default=30, ge=1, le=365),
    asin: Optional[str] = None,
    user_id: Optional[str] = None,
    limit: int = Query(default=200, ge=1, le=1000),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> Any:
    """动作时间线。

    `user_id` **不传 = 只看当前操作人**（`X-User-Id` 解析出来的人）；
    传 `all` = 全团队。

    为什么默认收窄，而不是默认全团队：`X-User-Id` 必须真的改变返回结果，
    否则它就是个装饰（验收项要的正是这条）。而"看不见别人跟过价"这个真实需求
    用**显式的** `user_id=all` 表达——默认全团队时，人会误以为列表里都是自己的记录，
    于是重复跟一次价，而那恰恰是这一屏要防止的事。
    """
    ctx = _ctx(x_user_id)
    raw = (user_id or "").strip()
    if raw.lower() in ("all", "*", "team", "全团队"):
        target: Optional[int] = None
    elif raw == "":
        target = ctx.user_id
    else:
        try:
            target = int(raw)
        except (TypeError, ValueError):
            return _json(
                {
                    "ok": False,
                    "error": f"user_id 只能是数字或 all，收到 {user_id!r}",
                    "hint": "看自己的记录不传该参数；看全团队传 user_id=all",
                }
            )
    return _json(
        await run_in_threadpool(
            _call,
            "list_actions",
            ctx,
            days=days,
            asin=asin,
            user_id=target,
            limit=limit,
        )
    )


@router.post("/api/panel/actions")
async def api_panel_record_action(
    request: Request,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> Any:
    """手工补记一条动作（兜底入口，来源记成 `panel`）。

    `source` 由 `ctx` 决定，**不接受前端传**：能传就意味着有人能把
    "面板手补的"记成"飞书卡片点的"，复盘时就分不清哪些是当场决策。
    """
    body, err = await _body(request)
    if err is not None:
        return err
    ctx = _ctx(x_user_id)
    return _json(
        await run_in_threadpool(
            _call,
            "record_action",
            ctx,
            action_type=body.get("action_type"),
            asin=body.get("asin"),
            description=body.get("description") or "",
            reason=body.get("reason"),
            signal_id=body.get("signal_id"),
            verify_days=body.get("verify_days"),
        )
    )


# ============================================================
# ⑥ 数据健康
# ============================================================


@router.get("/api/panel/health")
async def api_panel_health(
    days: int = Query(default=7, ge=1, le=90),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> Any:
    """五块健康度一次给全：采集轮次 / 失败 / 价格可信度 / 新鲜度 / 未映射身份。

    一次性打包而不是五个端点：数据源挂掉时最需要这一页，
    五个请求意味着"五分之一的机会整页打不开"，而失败率本来就高的时候这很致命。
    """
    ctx = _ctx(x_user_id)
    return _json(await run_in_threadpool(_call, "get_data_health", ctx, days=days))
