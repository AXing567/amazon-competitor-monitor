"""FastAPI 主入口。

一个进程同时干三件事：
1. 跑定时任务（APScheduler）——采集、日报、P0 预警
2. 提供 HTTP API——给网页端和外部调用
3. 接收飞书事件回调——Agent 对话入口

为什么合成一个进程：单租户自用，运维成本越低越好。
真到了要多实例的规模，再把调度拆出去（`SCHEDULER_ENABLED=false` 即可）。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.agent import llm
from app.api.dashboard import DASHBOARD_HTML
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
def api_add_competitor(body: AddCompetitorBody) -> dict:
    return pool.add_competitor(settings.tenant_id, body.asin, force=body.force)


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
    return DASHBOARD_HTML


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
