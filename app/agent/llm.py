"""DeepSeek 对话层（Function Calling）。

模型只负责「听懂人话 + 决定调哪个工具 + 把结果说人话」，
所有判断逻辑都在 tools → domain 里，模型不许自己算数。

这是刻意的设计：如果让模型自己算盈亏平衡点，它会算错，而且错得很自信。
"""
from __future__ import annotations

import json
import logging
from collections import deque
from typing import Optional

from openai import AsyncOpenAI, OpenAI

from app.agent.tools import TOOL_SCHEMAS, execute_tool
from app.config import settings

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 5          # 最多来回调 5 轮工具，防止死循环
MAX_HISTORY_TURNS = 12       # 保留最近 12 轮对话


# ============================================================
# 系统提示词
# ============================================================

SYSTEM_PROMPT = """你是「竞品监控助手」，一个给亚马逊卖家做竞品监控的专职运营分析师。

## 你的工作方式
你手上有工具，工具连着真实的数据库和卖家精灵数据。
**任何涉及具体数字、具体竞品、具体变化的问题，必须先调工具拿数据，不许凭印象回答。**

## 业务框架（这是你判断的底层逻辑）
竞品监控 = 2 个地基 + 6 步循环：
- 地基0 定自家锚点：五大维度——供给、财务、排位口碑、核心出单词、Listing。
  缺哪个锚点，对应的判断就做不了，要如实说「做不了」并告诉用户补什么。
- 地基1 划战场分层：四同法则（同价格带 0.7~1.4 倍、同流量池词重合>40%、同功能形态、同节点），
  通过后按加权词重合度分 S/A/B/C。不是所有看着像的都是竞品。
- 步骤2 变化监控：只盯四类信号（价格、供给、排名口碑、内容）。P0 立刻报，P1 进日报，P2 归档。
- 步骤3 解读攻防：他跌我攻、他涨我防、他病我要命、他强我学习。
- 步骤4 对标学习：他做得好的地方先抄作业，再谈对抗。
- 步骤5 动手验证：任何动作定了验证周期，到期看数据说话。
- 步骤6 池子维护：分层定期重算，掉队的降级。
- 步骤7 时间校准：旺季加密、淡季放松，别用同一套节奏跑全年。

## 几条铁律（违反会害死用户）
1. **跟价前必须看保本价**。对手卖得比你的成本低，你跟就是陪他一起死。
2. **库存撑不住就不能降价**。可售天数 <20 天时降价冲量 = 卖断货丢排名。
3. **清仓不要跟**。清仓方卖完就走，你跟价只是把自己的利润也清掉。
4. **算不清就说算不清**。缺数据时明确说「缺 X，补上我就能判断」，绝不编数字。
5. **不要重复用户的话**。用户已经知道"对手降价了"，你要给的是"然后呢"。

## 说话风格
- 直接、短、有结论。先给结论再给理由。
- 用中文，不用"根据您提供的信息"这类废话开头。
- 每次回答控制在 200 字内，除非用户在问方法论。
- 给建议时给具体动作（"用 Coupon 让到 19.99，别改标价"），不给空话（"建议密切关注"）。

## 边界
- 拿不到的数据：对手的广告预算、出价、ACOS、真实库存、成本结构、补货计划。被问到要直说拿不到。
- 你不做亚马逊后台操作，只给建议。
"""


# ============================================================
# 会话记忆
# ============================================================

_history: dict[str, deque] = {}


def _get_history(session_id: str) -> deque:
    if session_id not in _history:
        _history[session_id] = deque(maxlen=MAX_HISTORY_TURNS * 2)
    return _history[session_id]


def reset_history(session_id: str) -> None:
    _history.pop(session_id, None)


def history_size(session_id: str) -> int:
    return len(_history.get(session_id, ()))


# ============================================================
# 同步对话（脚本 / Web 用）
# ============================================================


def chat(user_text: str, session_id: str = "default") -> dict:
    """同步版：跑完整个工具调用循环，返回最终答复。"""
    if not settings.deepseek_api_key:
        return {
            "ok": False,
            "reply": "还没配置 DEEPSEEK_API_KEY，Agent 对话用不了。"
            "在 .env 里填上就能用；不过日报和 P0 预警不需要它。",
            "tool_calls": [],
        }

    client = OpenAI(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url)
    messages = _build_messages(session_id, user_text)
    trace: list[dict] = []

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            resp = client.chat.completions.create(
                model=settings.deepseek_model,
                messages=messages,
                tools=TOOL_SCHEMAS,
                tool_choice="auto",
                temperature=0.3,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("调用 DeepSeek 失败")
            return {"ok": False, "reply": f"模型调用失败：{exc}", "tool_calls": trace}

        msg = resp.choices[0].message

        if not msg.tool_calls:
            reply = (msg.content or "").strip()
            _remember(session_id, user_text, reply)
            return {"ok": True, "reply": reply, "tool_calls": trace}

        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
        )

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            result = execute_tool(name, args)
            trace.append({"tool": name, "args": args, "ok": result.get("ok", True)})

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            )

    fallback = "工具调用次数超限，问题可能需要拆得更具体一些。"
    _remember(session_id, user_text, fallback)
    return {"ok": True, "reply": fallback, "tool_calls": trace}


# ============================================================
# 异步对话（FastAPI / 飞书回调用）
# ============================================================


async def achat(user_text: str, session_id: str = "default") -> dict:
    if not settings.deepseek_api_key:
        return {
            "ok": False,
            "reply": "还没配置 DEEPSEEK_API_KEY，Agent 对话用不了。"
            "在 .env 里填上就能用；不过日报和 P0 预警不需要它。",
            "tool_calls": [],
        }

    client = AsyncOpenAI(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url)
    messages = _build_messages(session_id, user_text)
    trace: list[dict] = []

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            resp = await client.chat.completions.create(
                model=settings.deepseek_model,
                messages=messages,
                tools=TOOL_SCHEMAS,
                tool_choice="auto",
                temperature=0.3,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("调用 DeepSeek 失败")
            return {"ok": False, "reply": f"模型调用失败：{exc}", "tool_calls": trace}

        msg = resp.choices[0].message

        if not msg.tool_calls:
            reply = (msg.content or "").strip()
            _remember(session_id, user_text, reply)
            return {"ok": True, "reply": reply, "tool_calls": trace}

        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
        )

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            # 工具是同步 IO（DB + HTTP），丢到线程池里跑，别卡事件循环
            import asyncio

            result = await asyncio.to_thread(execute_tool, name, args)
            trace.append({"tool": name, "args": args, "ok": result.get("ok", True)})

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            )

    fallback = "工具调用次数超限，问题可能需要拆得更具体一些。"
    _remember(session_id, user_text, fallback)
    return {"ok": True, "reply": fallback, "tool_calls": trace}


# ============================================================
# 内部
# ============================================================


def _build_messages(session_id: str, user_text: str) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += list(_get_history(session_id))
    messages.append({"role": "user", "content": user_text})
    return messages


def _remember(session_id: str, user_text: str, reply: str) -> None:
    h = _get_history(session_id)
    h.append({"role": "user", "content": user_text})
    h.append({"role": "assistant", "content": reply})
