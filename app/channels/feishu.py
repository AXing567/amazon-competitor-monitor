"""飞书自建应用客户端。

只依赖 httpx，不引官方 SDK——官方 SDK 依赖重、版本容易打架，
而这里需要的接口一共就三个：拿 token、发消息、回消息。

接口文档：
- 取 token      POST /open-apis/auth/v3/tenant_access_token/internal
- 发消息        POST /open-apis/im/v1/messages?receive_id_type=chat_id
- 回消息        POST /open-apis/im/v1/messages/{message_id}/reply
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from typing import Any, Optional

import httpx

from app.channels.base import Channel
from app.config import settings

logger = logging.getLogger(__name__)

FEISHU_BASE = "https://open.feishu.cn/open-apis"


class FeishuChannel(Channel):
    name = "feishu"

    def __init__(
        self,
        app_id: Optional[str] = None,
        app_secret: Optional[str] = None,
        default_chat_id: Optional[str] = None,
    ) -> None:
        self.app_id = app_id or settings.feishu_app_id
        self.app_secret = app_secret or settings.feishu_app_secret
        self.default_chat_id = default_chat_id or settings.feishu_default_chat_id

        self._token: Optional[str] = None
        self._token_expire_at: float = 0.0

    # --------------------------------------------------------
    # 配置检查
    # --------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret)

    # --------------------------------------------------------
    # Token
    # --------------------------------------------------------

    async def _get_token(self, force: bool = False) -> Optional[str]:
        if not self.configured:
            return None
        if not force and self._token and time.time() < self._token_expire_at - 60:
            return self._token

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
                    json={"app_id": self.app_id, "app_secret": self.app_secret},
                )
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.error("获取飞书 token 失败: %s", exc)
            return None

        if data.get("code") != 0:
            logger.error("飞书 token 返回异常: %s", data)
            return None

        self._token = data.get("tenant_access_token")
        self._token_expire_at = time.time() + int(data.get("expire", 7200))
        return self._token

    # --------------------------------------------------------
    # 发送
    # --------------------------------------------------------

    async def _post_message(
        self,
        *,
        receive_id: str,
        msg_type: str,
        content: dict,
        receive_id_type: str = "chat_id",
    ) -> bool:
        token = await self._get_token()
        if not token:
            logger.warning("飞书未配置或取 token 失败，消息未发送")
            return False

        payload = {
            "receive_id": receive_id,
            "msg_type": msg_type,
            "content": json.dumps(content, ensure_ascii=False),
        }
        url = f"{FEISHU_BASE}/im/v1/messages?receive_id_type={receive_id_type}"

        for attempt in (1, 2):
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    resp = await client.post(
                        url,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json; charset=utf-8",
                        },
                        json=payload,
                    )
                    data = resp.json()
            except Exception as exc:  # noqa: BLE001
                logger.error("飞书发送异常（第 %d 次）: %s", attempt, exc)
                if attempt == 2:
                    return False
                continue

            code = data.get("code")
            if code == 0:
                return True

            # 99991663 / 99991668 = token 失效，换一次
            if code in (99991663, 99991668) and attempt == 1:
                await self._get_token(force=True)
                continue

            logger.error("飞书发送失败 code=%s msg=%s", code, data.get("msg"))
            return False

        return False

    async def send_text(self, to: str, text: str) -> bool:
        target = to or self.default_chat_id
        if not target:
            logger.warning("没有目标 chat_id，文本未发送")
            return False
        return await self._post_message(
            receive_id=target, msg_type="text", content={"text": text}
        )

    async def send_card(self, to: str, card: dict) -> bool:
        target = to or self.default_chat_id
        if not target:
            logger.warning("没有目标 chat_id，卡片未发送")
            return False
        return await self._post_message(
            receive_id=target, msg_type="interactive", content=card
        )

    async def reply_text(self, message_id: str, text: str) -> bool:
        """回复消息。飞书要求 3 秒内响应，所以 Agent 处理慢时必须走这条路。"""
        token = await self._get_token()
        if not token or not message_id:
            return False

        url = f"{FEISHU_BASE}/im/v1/messages/{message_id}/reply"
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json={
                        "msg_type": "text",
                        "content": json.dumps({"text": text}, ensure_ascii=False),
                    },
                )
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.error("飞书回复异常: %s", exc)
            return False

        if data.get("code") != 0:
            logger.error("飞书回复失败: %s", data)
            return False
        return True

    async def reply_card(self, message_id: str, card: dict) -> bool:
        token = await self._get_token()
        if not token or not message_id:
            return False

        url = f"{FEISHU_BASE}/im/v1/messages/{message_id}/reply"
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json={
                        "msg_type": "interactive",
                        "content": json.dumps(card, ensure_ascii=False),
                    },
                )
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.error("飞书回卡片异常: %s", exc)
            return False

        return data.get("code") == 0

    # --------------------------------------------------------
    # 自检
    # --------------------------------------------------------

    async def health(self) -> dict:
        if not self.configured:
            return {"channel": self.name, "status": "not_configured"}
        token = await self._get_token()
        return {
            "channel": self.name,
            "status": "ok" if token else "auth_failed",
            "default_chat_id": self.default_chat_id or None,
        }


# ============================================================
# 事件解密（飞书后台开了 Encrypt Key 才需要）
# ============================================================


def decrypt_event(encrypt_key: str, encrypted: str) -> Optional[dict]:
    """AES-256-CBC 解密。key = sha256(encrypt_key)，IV 取密文前 16 字节。"""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        logger.error("检测到加密事件但未安装 cryptography，请 pip install cryptography")
        return None

    try:
        key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
        raw = base64.b64decode(encrypted)
        iv, ciphertext = raw[:16], raw[16:]
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        plain = decryptor.update(ciphertext) + decryptor.finalize()
        # 去 PKCS7 padding
        pad = plain[-1]
        plain = plain[:-pad] if 0 < pad <= 16 else plain
        return json.loads(plain.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.error("飞书事件解密失败: %s", exc)
        return None


def verify_token(payload: dict) -> bool:
    """校验 verification token，防止伪造回调。未配置 token 时放行（本地调试）。"""
    expected = settings.feishu_verification_token
    if not expected:
        return True
    token = payload.get("token") or (payload.get("header") or {}).get("token")
    return token == expected


# ============================================================
# 事件解析
# ============================================================


def parse_message_event(event: dict) -> Optional[dict]:
    """从 im.message.receive_v1 事件里抽出「谁说了什么」。

    返回 {open_id, chat_id, chat_type, message_id, text, user_name}
    """
    try:
        message = event.get("message") or {}
        sender = event.get("sender") or {}

        msg_type = message.get("message_type")
        if msg_type != "text":
            return {
                "open_id": (sender.get("sender_id") or {}).get("open_id", ""),
                "chat_id": message.get("chat_id", ""),
                "chat_type": message.get("chat_type", "p2p"),
                "message_id": message.get("message_id", ""),
                "text": "",
                "unsupported": msg_type,
            }

        content = json.loads(message.get("content") or "{}")
        text = (content.get("text") or "").strip()
        # 群聊里 @机器人 会带 @_user_1 之类的占位符，去掉
        for token in ("@_user_1", "@_user_2", "@_user_3"):
            text = text.replace(token, "").strip()

        return {
            "open_id": (sender.get("sender_id") or {}).get("open_id", ""),
            "chat_id": message.get("chat_id", ""),
            "chat_type": message.get("chat_type", "p2p"),
            "message_id": message.get("message_id", ""),
            "text": text,
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("解析飞书消息事件失败: %s", exc)
        return None


# ============================================================
# 单例
# ============================================================

_channel: Optional[FeishuChannel] = None


def get_feishu() -> FeishuChannel:
    global _channel
    if _channel is None:
        _channel = FeishuChannel()
    return _channel
