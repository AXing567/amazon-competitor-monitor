"""消息渠道抽象层。

多平台的承诺就落在这一个文件上：Agent 与业务层只说「发一条日报」，
具体是飞书、钉钉还是企业微信，由路由层决定。加渠道 = 加一个实现类，
上层代码一行都不用改。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class Channel(ABC):
    """渠道接口。所有实现必须是幂等失败的——发不出去不能拖垮采集。"""

    name: str = "base"

    @abstractmethod
    async def send_text(self, to: str, text: str) -> bool:
        """发纯文本。to 的含义由渠道自己解释（chat_id / open_id / webhook）。"""

    @abstractmethod
    async def send_card(self, to: str, card: dict) -> bool:
        """发富文本卡片。渠道不支持时降级成 send_text。"""

    async def reply_text(self, message_id: str, text: str) -> bool:
        """回复某条消息。默认退化成主动发送。"""
        return False

    @abstractmethod
    async def health(self) -> dict:
        """自检，供 /health 端点聚合。"""


class NullChannel(Channel):
    """未配置渠道时的占位实现：只打日志，不报错。

    这样即使没配飞书，整个系统也能跑起来调试——
    日报会写进日志而不是发出去，方便验证逻辑。
    """

    name = "null"

    def __init__(self) -> None:
        import logging

        self._log = logging.getLogger("channel.null")

    async def send_text(self, to: str, text: str) -> bool:
        self._log.info("[未配置渠道，输出到日志]\n%s", text)
        return True

    async def send_card(self, to: str, card: dict) -> bool:
        self._log.info("[未配置渠道，卡片降级为日志] %s", card.get("header", {}).get("title", {}))
        return True

    async def health(self) -> dict:
        return {"channel": self.name, "status": "disabled"}


class SendResult:
    """发送结果，带失败原因，便于排查。"""

    def __init__(self, ok: bool, channel: str, detail: Optional[Any] = None):
        self.ok = ok
        self.channel = channel
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover
        return f"SendResult(ok={self.ok}, channel={self.channel})"
