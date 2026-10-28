"""把业务内容渲染成渠道卡片。

同一份 DailyBrief / BriefItem，在这里被翻译成飞书卡片 JSON。
以后加钉钉：照着这个文件的思路再写一个 render_dingtalk 即可，
briefer 与 decision 层一个字都不用动。
"""
from __future__ import annotations

from typing import Optional

from app.services.briefer import BriefItem, DailyBrief

# 飞书卡片头部配色
LEVEL_COLOR = {"P0": "red", "P1": "orange", "P2": "grey"}
DECISION_BADGE = {
    "attack": "🟥 建议进攻",
    "defend": "🟦 建议防守",
    "learn": "🟨 建议学习",
    "no_action": "⬜ 暂不动",
}


# ============================================================
# 日报卡片
# ============================================================


def render_daily_card(brief: DailyBrief) -> dict:
    elements: list[dict] = []

    # ---- 锚点区 ----
    a = brief.anchor or {}
    dos = a.get("days_of_supply")
    inv_map = {"alert": "偏紧", "normal": "正常", "surplus": "充裕", "unknown": "未知"}
    inv = inv_map.get(a.get("inventory_status", "unknown"), "未知")
    inv_emoji = {"alert": "🔴", "normal": "🟢", "surplus": "🔵", "unknown": "⚪"}.get(
        a.get("inventory_status", "unknown"), "⚪"
    )

    anchor_md = (
        f"**自家锚点** 就绪度 **{a.get('readiness', 0)}%**（{a.get('grade', 'D')} 级）\n"
        f"{inv_emoji} 可售天数：{f'{dos:.0f} 天' if dos else '未知'}（{inv}）"
    )
    warnings = (a.get("warnings") or [])[:2]
    if warnings:
        anchor_md += "\n" + "\n".join(f"⚠️ {w}" for w in warnings)

    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": anchor_md}})
    elements.append({"tag": "hr"})

    # ---- 无变化 ----
    if brief.quiet:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "**今日无 P0 / P1 级变化**，池子安静。"},
            }
        )

    # ---- P0 ----
    if brief.p0:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"🔴 **P0 · 立即处理（{len(brief.p0)}）**",
                },
            }
        )
        for it in brief.p0:
            elements.append(_item_block(it))

    # ---- P1 ----
    if brief.p1:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"🟠 **P1 · 今日关注（{len(brief.p1)}）**",
                },
            }
        )
        for it in brief.p1:
            elements.append(_item_block(it))

    # ---- 池子与数据健康 ----
    pool = brief.pool or {}
    if pool:
        pool_md = " · ".join(f"{k} 级 {pool.get(k, 0)}" for k in ("S", "A", "B", "C"))
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**竞品池** {pool_md}"}})

    dh = brief.data_health or {}
    notes: list[str] = []
    if dh.get("failed"):
        notes.append(f"采集失败：{', '.join(dh['failed'])}")
    if dh.get("partial"):
        notes.append(f"数据不全：{', '.join(dh['partial'])}")
    if brief.p2_count:
        notes.append(f"另有 {brief.p2_count} 条 P2 已归档")
    if notes:
        elements.append(
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": " | ".join(notes)}],
            }
        )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"竞品日报 · {brief.date}"},
            "template": "red" if brief.p0 else ("orange" if brief.p1 else "blue"),
        },
        "elements": elements,
    }


def _item_block(it: BriefItem) -> dict:
    badge = DECISION_BADGE.get(it.decision, "")
    lines = [f"**[{it.tier}] {it.asin}** {_short(it.title, 24)}", it.description]
    if it.headline:
        lines.append(f"→ *{it.headline}* {badge}")
    for act in it.actions[:3]:
        lines.append(f"• {act}")
    return {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}


# ============================================================
# P0 预警卡片
# ============================================================


def render_alert_card(item: BriefItem) -> dict:
    badge = DECISION_BADGE.get(item.decision, "")
    body = [item.description]
    if item.headline:
        body += ["", f"**→ {item.headline}** {badge}"]
    if item.actions:
        body.append("")
        body += [f"{i}. {a}" for i, a in enumerate(item.actions[:4], 1)]

    confidence = ""
    if item.confidence is not None:
        confidence = f"判断确定性 {item.confidence:.0%}"

    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(body)}}
    ]
    if confidence:
        elements.append(
            {"tag": "note", "elements": [{"tag": "plain_text", "content": confidence}]}
        )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"{item.level} · {item.tier} 级竞品 {item.asin} 有变动",
            },
            "template": LEVEL_COLOR.get(item.level, "grey"),
        },
        "elements": elements,
    }


# ============================================================
# 通用 Markdown 卡片（Agent 对话回复用）
# ============================================================


def render_markdown_card(title: str, markdown: str, template: str = "blue") -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": markdown}}],
    }


def _short(text: Optional[str], limit: int = 30) -> str:
    text = (text or "").strip()
    return text[:limit] + ("…" if len(text) > limit else "")
