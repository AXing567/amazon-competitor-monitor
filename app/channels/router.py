"""渠道路由。

上层只说「推一条预警」，这里决定发给谁、用哪个渠道、卡片还是文本。

目标解析顺序：
1. 数据库里该租户已绑定的渠道与目标 ID
2. .env 里的默认 chat_id
3. 都没有 → NullChannel（写日志，不报错）

第 3 条很重要：没配渠道时整个系统仍然能跑，方便先验证业务逻辑。
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select

from app.channels.base import Channel, NullChannel
from app.channels.feishu import get_feishu
from app.channels.render import render_alert_card, render_daily_card
from app.config import settings
from app.db import get_session
from app.models import ChannelBinding
from app.services.briefer import BriefItem, DailyBrief

logger = logging.getLogger(__name__)


class ChannelRouter:
    def __init__(self) -> None:
        self._null = NullChannel()

    # --------------------------------------------------------
    # 渠道与目标解析
    # --------------------------------------------------------

    def _channel(self, name: str) -> Channel:
        if name == "feishu":
            ch = get_feishu()
            if not ch.configured:
                logger.debug("飞书未配置，降级为日志输出")
                return self._null
            return ch
        return self._null

    def _targets(self, tenant_id: str) -> list[tuple[str, str]]:
        """返回 [(渠道名, 目标ID), ...]。"""
        out: list[tuple[str, str]] = []
        try:
            with get_session() as s:
                rows = list(
                    s.execute(
                        select(ChannelBinding).where(ChannelBinding.tenant_id == tenant_id)
                    ).scalars()
                )
            for r in rows:
                if r.channel == "feishu":
                    out.append(("feishu", r.platform_user_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取渠道绑定失败: %s", exc)

        if not out and settings.feishu_default_chat_id:
            out.append(("feishu", settings.feishu_default_chat_id))

        if not out:
            out.append(("feishu", ""))
        return out

    # --------------------------------------------------------
    # 推送
    # --------------------------------------------------------

    async def push_alert(self, tenant_id: str, text: str, item: Optional[BriefItem] = None) -> bool:
        card = render_alert_card(item) if item else None
        sent_any = False

        for channel_name, target in self._targets(tenant_id):
            ch = self._channel(channel_name)
            try:
                ok = await ch.send_card(target, card) if card else await ch.send_text(target, text)
            except Exception as exc:  # noqa: BLE001
                logger.error("渠道 %s 推送异常: %s", channel_name, exc)
                ok = False

            if not ok and card:
                # 卡片发不出去时退回纯文本，保证信息不丢
                logger.info("卡片推送失败，降级为文本")
                ok = await ch.send_text(target, text)

            sent_any = sent_any or ok

        return sent_any

    async def push_daily(self, tenant_id: str, brief: DailyBrief, text: str) -> bool:
        card = render_daily_card(brief)
        sent_any = False

        for channel_name, target in self._targets(tenant_id):
            ch = self._channel(channel_name)
            try:
                ok = await ch.send_card(target, card)
            except Exception as exc:  # noqa: BLE001
                logger.error("日报推送异常(%s): %s", channel_name, exc)
                ok = False

            if not ok:
                ok = await ch.send_text(target, text)

            sent_any = sent_any or ok

        return sent_any

    async def reply(self, message_id: str, text: str, card: Optional[dict] = None) -> bool:
        """回复用户消息（Agent 对话用）。"""
        ch = get_feishu()
        if not ch.configured:
            logger.info("[未配置飞书] 回复内容：\n%s", text)
            return True
        if card:
            ok = await ch.reply_card(message_id, card)
            if ok:
                return True
        return await ch.reply_text(message_id, text)

    async def health(self) -> dict:
        feishu = await get_feishu().health()
        return {"feishu": feishu}


# ============================================================
# 单例
# ============================================================

_router: Optional[ChannelRouter] = None


def get_router() -> ChannelRouter:
    global _router
    if _router is None:
        _router = ChannelRouter()
    return _router
