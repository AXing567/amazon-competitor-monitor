"""FastAPI 主入口。

一个进程同时干三件事：
1. 跑定时任务（APScheduler）——采集、日报、P0 预警
2. 提供 HTTP API——给网页端和外部调用
3. 接收飞书事件回调——Agent 对话入口

为什么合成一个进程：单租户自用，运维成本越低越好。
真到了要多实例的规模，再把调度拆出去（`SCHEDULER_ENABLED=false` 即可）。

## 路由分工（T04 之后）

| 前缀 | 归属 | 说明 |
|---|---|---|
| `/api/panel/*` | T05 面板 | 五个视图的聚合端点 |
| `/api/config/*` | `app/api/routes/config.py` | 阈值配置 7 个端点 |
| `/api/cap/*` | `app/api/routes/capabilities.py` | 通用能力分发（33 个能力共用 3 个端点） |
| `/api/users` `/api/bindings` | `app/api/routes/identity.py` | 用户 / 身份 / 绑定 |
| `/api/brief` `/api/competitors` ... | 本文件 | **老端点，保留不动**：外部脚本还在用 |

老端点为什么留着：它们是"单点直调"的旧形态，功能与能力层重复但不冲突。
删掉会让已经在跑的 curl 脚本断掉，而收益只是少 40 行代码——不值。
新代码一律走能力层，老端点标在这里当作技术债清单。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.agent import llm
from app.api.deps import resolve_ctx
from app.api.middleware import DataModeMiddleware
from app.api.routes import api_router
from app.capabilities.types import SOURCE_SYSTEM
from app.channels.feishu import (
    decrypt_event,
    get_feishu,
    parse_message_event,
    verify_token,
)
from app.channels.router import get_router
from app.config import settings
from app.db import init_db
from app.services import briefer, collector, pool
from app.services.scheduler import (
    scheduler_status,
    start_scheduler,
    stop_scheduler,
)
from app.web import render_panel

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


# ============================================================
# 生命周期
# ============================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("数据库就绪")

    if settings.scheduler_enabled:
        start_scheduler()
    else:
        logger.info("调度已禁用（SCHEDULER_ENABLED=false）")

    logger.info("服务已启动 http://%s:%d", settings.app_host, settings.app_port)
    yield

    stop_scheduler()
    logger.info("服务已停止")


app = FastAPI(
    title="竞品监控服务",
    version="0.1.0",
    description="亚马逊竞品监控 · 采集/分层/预警/Agent 对话",
    lifespan=lifespan,
)

# 数据模式：从 `X-Data-Mode` 读 live / mock，钉进本次请求的上下文。
# 用纯 ASGI 中间件而不是 `@app.middleware("http")`，理由见 `app/api/middleware.py`
# ——那条链路上 ContextVar 的传播不能有"通常没问题"。
app.add_middleware(DataModeMiddleware)

# T04：L4 能力层的 REST 出口。挂在这里而不是散落在本文件，
# 是为了让「有哪些端点」这件事只有一个地方能回答。
app.include_router(api_router)


# ============================================================
# 健康检查
# ============================================================


@app.get("/health")
async def health() -> dict:
    feishu = await get_feishu().health()
    adapter = await _adapter_health()
    return {
        "service": "ok",
        "tenant": settings.tenant_id,
        "scheduler": scheduler_status(),
        "channels": {"feishu": feishu},
        "data_source": adapter,
        "llm_configured": bool(settings.deepseek_api_key),
    }


async def _adapter_health() -> dict:
    from app.adapters.sellersprite import SellerSpriteAdapter

    try:
        return await SellerSpriteAdapter().health()
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc)}


# ============================================================
# 业务 API
# ============================================================


@app.get("/api/brief")
def api_brief(hours: int = 24) -> dict:
    brief = briefer.build_daily_brief(settings.tenant_id, hours=hours)
    data = brief.to_dict()
    data["text"] = brief.to_text()
    return data


@app.get("/api/competitors")
def api_competitors(tier: Optional[str] = None) -> dict:
    items = pool.list_competitors(settings.tenant_id, tier=tier)
    return {"count": len(items), "items": items}


class AddCompetitorBody(BaseModel):
    asin: str
    force: bool = False


@app.post("/api/competitors")
def api_add_competitor(
    body: AddCompetitorBody,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> dict:
    """加入竞品池。

    这是**写路径**，所以必须记下「谁加的」：`competitor_bindings.user_id` 是
    预警按人分发的唯一依据，记错了（落到默认用户）等于给这条竞品配了一个假负责人。
    以前这里直调 `pool.add_competitor(settings.tenant_id, ...)`，身份在这一跳就丢了。

    `source` 用 `SOURCE_SYSTEM` 而不是 `SOURCE_PANEL`：本端点是**老端点，外部脚本还在用**
    （见模块文档），触发方式是脚本而不是人点面板——记成 panel 会让「谁在什么时候
    加了竞品」这条线索指向一个根本没点过的人。来源只影响 `collect_runs.trigger`
    与动作留痕，不影响任何业务判断。
    """
    ctx = resolve_ctx(x_user_id, source=SOURCE_SYSTEM)
    return pool.add_competitor(
        ctx.tenant_id, body.asin, force=body.force, user_id=ctx.user_id
    )


class BaselineBody(BaseModel):
    asin: Optional[str] = None
    current_price: Optional[float] = None
    break_even_price: Optional[float] = None
    target_margin_rate: Optional[float] = None
    daily_sales_avg: Optional[float] = None
    fba_available_units: Optional[int] = None
    lead_time_days: Optional[int] = None


@app.post("/api/baseline")
def api_set_baseline(body: BaselineBody) -> dict:
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    return pool.update_baseline(settings.tenant_id, **fields)


class CollectBody(BaseModel):
    asin: Optional[str] = None


@app.post("/api/collect")
async def api_collect(body: CollectBody) -> dict:
    if body.asin:
        with_res = await collector.acollect_competitor_by_asin(settings.tenant_id, body.asin)
        return with_res
    results = await collector.collect_all(settings.tenant_id)
    return {
        "total": len(results),
        "success": sum(1 for r in results if r.ok),
        "p0": sum(r.p0_count for r in results),
        "items": [
            {
                "asin": r.asin,
                "ok": r.ok,
                "quality": r.data_quality,
                "signals": len(r.signals),
                "errors": r.errors[:3],
            }
            for r in results
        ],
    }


class ChatBody(BaseModel):
    message: str
    session_id: str = "web"


@app.post("/api/chat")
async def api_chat(body: ChatBody) -> dict:
    return await llm.achat(body.message, session_id=body.session_id)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """面板入口（五个视图，单文件 HTML，零外链）。

    刻意**不跳转**到 `/panel`：请求 `/` 拿到 200 的一个页面，是运维最想看到的信号。
    数据源全挂的时候这一页也能打开——它只依赖 `/api/panel/*`，而那些端点
    在数据源熔断时返回 200 + `ok:false`，于是页面显示的是错误卡片而不是白屏。
    """
    return render_panel()


@app.get("/panel", response_class=HTMLResponse)
def panel() -> str:
    """与 `/` 同一个页面，只是给书签一个语义明确的地址。"""
    return render_panel()


# `/legacy` 与 `_panel_placeholder()` 已删除（2026-11-04）。
# 旧看板当初的定位是「保留到新面板全部验收通过为止」——六视图现已全部接通
# （含自家基线），那个条件已经满足。留着它的代价不是 219 行代码，
# 而是每个新来的人都要先判断一次「这两个页面哪个是真的」。
# 删掉的东西在 git 历史里都还在：`git log -- app/api/dashboard.py`。


# placeholder 已删除，见上文说明。



# ============================================================
# 飞书事件回调
# ============================================================

_seen_events: set[str] = set()


@app.post("/feishu/webhook")
async def feishu_webhook(request: Request, background: BackgroundTasks):
    """飞书事件订阅入口。

    飞书要求 3 秒内响应，所以这里只做「验证 + 塞后台」，绝不在请求里调模型。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"code": -1, "msg": "invalid json"}, status_code=400)

    # ---- 加密模式 ----
    if "encrypt" in body:
        if not settings.feishu_encrypt_key:
            logger.error("收到加密事件但未配置 FEISHU_ENCRYPT_KEY")
            return JSONResponse({"code": -1, "msg": "encrypt key not configured"}, status_code=400)
        decrypted = decrypt_event(settings.feishu_encrypt_key, body["encrypt"])
        if decrypted is None:
            return JSONResponse({"code": -1, "msg": "decrypt failed"}, status_code=400)
        body = decrypted

    # ---- URL 校验 ----
    if body.get("type") == "url_verification":
        if not verify_token(body):
            return JSONResponse({"code": -1, "msg": "token mismatch"}, status_code=403)
        return {"challenge": body.get("challenge")}

    # ---- token 校验 ----
    if not verify_token(body):
        logger.warning("飞书回调 token 校验失败")
        return JSONResponse({"code": -1, "msg": "token mismatch"}, status_code=403)

    # ---- 事件去重（飞书会重试） ----
    header = body.get("header") or {}
    event_id = header.get("event_id") or body.get("uuid")
    if event_id:
        if event_id in _seen_events:
            return {"code": 0}
        _seen_events.add(event_id)
        if len(_seen_events) > 2000:
            _seen_events.clear()

    event_type = header.get("event_type") or body.get("event", {}).get("type")
    if event_type != "im.message.receive_v1":
        return {"code": 0}

    event = body.get("event") or {}
    parsed = parse_message_event(event)
    if parsed is None:
        return {"code": 0}

    if parsed.get("unsupported"):
        background.add_task(
            _reply_async, parsed["message_id"], "目前只认文字消息，发文字给我就行。"
        )
        return {"code": 0}

    text = parsed.get("text") or ""
    if not text:
        return {"code": 0}

    background.add_task(
        _handle_agent_message,
        session_id=parsed.get("open_id") or parsed.get("chat_id") or "feishu",
        message_id=parsed["message_id"],
        text=text,
    )
    return {"code": 0}


async def _handle_agent_message(session_id: str, message_id: str, text: str) -> None:
    """后台跑 Agent，跑完回消息。"""
    router = get_router()
    try:
        result = await llm.achat(text, session_id=session_id)
        reply = result.get("reply") or "（无回复）"
        if len(reply) > 3500:
            reply = reply[:3500] + "\n…（内容过长已截断）"
        await router.reply(message_id, reply)
        logger.info("已回复飞书消息 tool_calls=%s", [t["tool"] for t in result.get("tool_calls", [])])
    except Exception as exc:  # noqa: BLE001
        logger.exception("处理飞书消息失败")
        await router.reply(message_id, f"处理失败：{exc}")


async def _reply_async(message_id: str, text: str) -> None:
    await get_router().reply(message_id, text)


# ============================================================
# 手动触发（调试用）
# ============================================================


@app.post("/api/run/daily-brief")
async def api_run_daily_brief() -> dict:
    """立刻生成并推送一次日报，用来测试渠道是否通。"""
    brief = briefer.build_daily_brief(settings.tenant_id, hours=24)
    ok = await get_router().push_daily(settings.tenant_id, brief, brief.to_text())
    return {"pushed": ok, "text": brief.to_text()}


@app.post("/api/run/p0-check")
async def api_run_p0_check() -> dict:
    from app.services.scheduler import push_p0_alerts

    sent = await push_p0_alerts(minutes=600)
    return {"sent": sent}
