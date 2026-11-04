"""渠道路由。

上层只说「推一条预警」，这里决定发给谁、用哪个渠道、卡片还是文本。

预警（`push_alert`）的目标解析顺序：
1. 传了 `competitor_id` → 「谁在盯这个竞品」：`competitor_bindings` 的 active 绑定人，
   再经 `user_identities` 翻成他们自己的平台 ID（人各收各的）
2. 解析不出人（没人绑这个竞品 / 绑的人都没配可用渠道）→ **退回**下面的租户级默认目标，
   并打一条 WARNING（监控系统静默丢弃 = 漏报，且没人会发现路由数据缺了）
3. 数据库里该租户已绑定的渠道与目标 ID（`channel_bindings`）
4. .env 里的默认 chat_id
5. 都没有 → NullChannel（写日志，不报错）

日报（`push_daily`）永远是团队级汇总，只走 3→5，不按人拆。

第 5 条很重要：没配渠道时整个系统仍然能跑，方便先验证业务逻辑。

## 为什么「只返回真有实现的渠道」

`NullChannel.send_text()` **返回 True**（见 `app/channels/base.py`）：渠道没配时它
「假装发送成功」。所以一旦把一个走未实现渠道（企微 / 钉钉 / web）的收件人交给
`_channel()`，会拿到 NullChannel → 返回 True → `push_alert` 认为送达，
而实际只打了一行日志。**告警静默消失，调用方看到的是成功。**

所以 `_targets_for_competitor()` 只输出 `_IMPLEMENTED_CHANNELS` 里的渠道，
其余渠道用 WARNING 点名列出。宁可「明说这条送不出去」，也不要「假装送出去了」。
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
from app.models import ChannelBinding, CompetitorBinding, User, UserIdentity
from app.services.briefer import BriefItem, DailyBrief

logger = logging.getLogger(__name__)

#: `_channel()` 里**真正有实现**的渠道。改这个常量前先确认 `_channel()` 里有对应分支，
#: 否则就是把收件人送进 NullChannel（返回 True 但什么都没发）。
_IMPLEMENTED_CHANNELS: tuple[str, ...] = ("feishu",)


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
        """租户级默认目标：[(渠道名, 目标ID), ...]。"""
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

    def _targets_for_competitor(
        self, tenant_id: str, competitor_id: int
    ) -> list[tuple[str, str]]:
        """谁在盯这个竞品 → [(渠道名, 平台用户ID), ...]。

        两次查询（绑定人一次、身份一次），与绑定人数量无关：
        不按人循环查库，所以这里**不是 N+1**。

        只返回 `_IMPLEMENTED_CHANNELS` 里的渠道——未实现的渠道会静默成功
        （见模块文档里 NullChannel 那个坑），一律不放进结果，改用 WARNING 点名。
        返回空列表表示「这个竞品没有可用收件人」，由调用方决定怎么退回。
        """
        cid = int(competitor_id)
        user_ids: list[int] = []
        try:
            with get_session() as s:
                user_ids = sorted(
                    {
                        int(uid)
                        for uid in s.execute(
                            select(CompetitorBinding.user_id).where(
                                CompetitorBinding.tenant_id == tenant_id,
                                CompetitorBinding.competitor_id == cid,
                                CompetitorBinding.status == "active",
                            )
                        ).scalars()
                        if uid is not None
                    }
                )
                if not user_ids:
                    # 没人绑这个竞品。这里不打日志：调用方会打一条含「退回」的 WARNING，
                    # 两处都打只会让日志里同一件事出现两次。
                    return []

                # 只取 active 用户的身份：停用的账号不该继续收预警。
                identities = list(
                    s.execute(
                        select(UserIdentity)
                        .join(User, User.id == UserIdentity.user_id)
                        .where(UserIdentity.user_id.in_(user_ids), User.active.is_(True))
                    ).scalars()
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("解析竞品 %s 的投递目标失败: %s", cid, exc)
            return []

        out: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        reachable_users: set[int] = set()
        unsupported: dict[str, list[str]] = {}

        for ident in identities:
            channel = (ident.channel or "").strip().lower()
            target = (ident.external_id or "").strip()
            if not channel or not target:
                continue
            if channel not in _IMPLEMENTED_CHANNELS:
                unsupported.setdefault(channel, []).append(target)
                continue
            key = (channel, target)
            if key in seen:
                # 两条绑定指向同一人时，同一 (渠道, 目标) 会被解析两次，只发一次
                continue
            seen.add(key)
            out.append(key)
            reachable_users.add(int(ident.user_id))

        for channel, targets in unsupported.items():
            logger.warning(
                "竞品 %s 有 %d 个绑定人身份只配了未实现的渠道 %r（%s），这些人本轮收不到预警；"
                "要投递请实现该渠道或改绑到 %s",
                cid,
                len(targets),
                channel,
                "、".join(targets[:5]) + ("…" if len(targets) > 5 else ""),
                "/".join(_IMPLEMENTED_CHANNELS),
            )

        silent_users = [uid for uid in user_ids if uid not in reachable_users]
        if silent_users:
            logger.warning(
                "竞品 %s 有 %d 个绑定人没有任何可用投递渠道（user_id: %s）",
                cid,
                len(silent_users),
                "、".join(str(u) for u in silent_users[:10]),
            )

        return out

    # --------------------------------------------------------
    # 推送
    # --------------------------------------------------------

    async def push_alert(
        self,
        tenant_id: str,
        text: str,
        item: Optional[BriefItem] = None,
        competitor_id: Optional[int] = None,
    ) -> bool:
        """推一条预警。

        `competitor_id` 不为 None 时按「竞品 → 绑定人」分发（人各收各的）；
        解析不出可用收件人时**退回**租户默认目标并出声，绝不静默丢弃。
        `competitor_id` 为 None 时与旧行为完全一致（租户级广播）。
        """
        card = render_alert_card(item) if item else None
        sent_any = False

        for channel_name, target in self._alert_targets(tenant_id, competitor_id):
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

    def _alert_targets(
        self, tenant_id: str, competitor_id: Optional[int]
    ) -> list[tuple[str, str]]:
        """预警的收件人列表。按竞品解析失败时退回租户默认并出声。"""
        if competitor_id is None:
            return self._targets(tenant_id)

        targets = self._targets_for_competitor(tenant_id, int(competitor_id))
        if targets:
            logger.info(
                "竞品 %s 的预警投递给 %d 个绑定人", competitor_id, len(targets)
            )
            return targets

        fallback = self._targets(tenant_id)
        logger.warning(
            "竞品 %s 没有可用的投递目标（无人绑定，或绑定人都没配可用渠道），"
            "退回租户默认渠道（%d 个目标）",
            competitor_id,
            len(fallback),
        )
        return fallback

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
