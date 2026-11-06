"""日报/预警的内容组装。

两条输出路径共用同一份数据：
- 飞书卡片（结构化）
- 纯文本（卡片发不出去时的兜底，也方便 Agent 直接读）

一条设计原则：**每条信号后面都必须跟一个「然后呢」**。
只报「对手降价了」而不给动作，等于把活又推回给老板。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.domain import baseline as baseline_mod
from app.domain.decision import (
    DecisionResult,
    build_out_of_stock_decision,
    judge_price_follow,
)
from app.models import Competitor, MyProduct, Signal, Snapshot
from app.services import action_log, decision_log

# ============================================================
# 结构
# ============================================================


@dataclass
class BriefItem:
    asin: str
    tier: str
    title: str
    signal_type: str
    level: str
    description: str
    decision: str = "no_action"          # attack / defend / learn / no_action
    headline: str = ""
    #: 判断理由，逐条。它和 `headline` 的分工：headline 是一句话结论（给推送标题），
    #: reasons 是「凭什么」（给三天后来问为什么的人）。
    reasons: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    #: 判断时算出来的中间量（盈亏平衡单量、清仓得分…）。
    #: 结论可以重算，这些数不能——售价/成本/日均销量都会变。
    metrics: dict = field(default_factory=dict)
    confidence: Optional[float] = None
    #: 建议几天后回看这个判断。None = 这个判断没有可验证的时间点
    #: （比如「锚点缺数据，判断不了」——回看它没有意义）。
    verify_days: Optional[int] = None
    competitor_id: Optional[int] = None
    #: 这条变化是**什么时候**发生的。
    #: 面板看板要显示「最近变化时间」；飞书卡片用不到，但少了它面板就得对
    #: 每个竞品再查一次信号（N+1），而且在飞书里回"这是几点的事"也答不上来。
    detected_at: Optional[datetime] = None
    #: 这条判断依据的原始信号 ID。**它是信号 → 决策 → 动作三段链的唯一连接键**：
    #: 没有它，「这条动作为什么这么做」只能靠人回忆；有了它，
    #: 点开动作就能拉出当时的判断原文。
    signal_id: Optional[int] = None
    #: 落库之后的决策行 ID。装配阶段是 None，`_persist_decisions()` 之后才有值。
    #: 刻意保持「可能为 None」：审计写失败时消息照样要发出去，此时它就是 None。
    decision_id: Optional[int] = None


@dataclass
class DailyBrief:
    date: str
    anchor: dict = field(default_factory=dict)
    p0: list[BriefItem] = field(default_factory=list)
    p1: list[BriefItem] = field(default_factory=list)
    p2_count: int = 0
    pool: dict = field(default_factory=dict)
    data_health: dict = field(default_factory=dict)
    quiet: bool = False
    #: 到期该复盘的动作。放在日报里而不是单独一个提醒渠道，理由很实际：
    #: 单开一个「复盘提醒」通道，一周后它就会变成被静音的那个。
    #: 而日报每天都在看，末尾多三行不会让人烦，反而形成习惯。
    reviews: list[dict] = field(default_factory=list)

    # --------------------------------------------------------
    # 文字版
    # --------------------------------------------------------

    def to_text(self) -> str:
        lines: list[str] = []
        lines.append(f"竞品日报 · {self.date}")
        lines.append("")

        # 锚点
        a = self.anchor or {}
        dos = a.get("days_of_supply")
        inv = {
            "alert": "偏紧",
            "normal": "正常",
            "surplus": "充裕",
            "unknown": "未知",
        }.get(a.get("inventory_status", "unknown"), "未知")
        lines.append(
            f"自家锚点：就绪度 {a.get('readiness', 0)}%（{a.get('grade', 'D')} 级）"
            f" · 可售 {f'{dos:.0f} 天' if dos else '未知'}（{inv}）"
        )
        for w in (a.get("warnings") or [])[:2]:
            lines.append(f"  ⚠ {w}")
        lines.append("")

        if self.quiet:
            lines.append("今日无 P0/P1 级变化。")
        else:
            if self.p0:
                lines.append(f"【P0 立即处理】{len(self.p0)} 条")
                for i, it in enumerate(self.p0, 1):
                    lines += _item_lines(i, it)
                lines.append("")

            if self.p1:
                lines.append(f"【P1 今日关注】{len(self.p1)} 条")
                for i, it in enumerate(self.p1, 1):
                    lines += _item_lines(i, it)
                lines.append("")

        if self.p2_count:
            lines.append(f"另有 {self.p2_count} 条 P2 变化已归档（内容改动、排名微调等），不进日报。")
            lines.append("")

        if self.reviews:
            lines.append(f"【到期该复盘】{len(self.reviews)} 条")
            for r in self.reviews[:5]:
                overdue = r.get("overdue_days") or 0
                tail = f"（超期 {overdue} 天）" if overdue > 0 else "（今日到期）"
                at = (r.get("at") or "")[:10]
                lines.append(
                    f"· {at} {r.get('asin') or '无竞品'} "
                    f"{r.get('action_label') or r.get('action_type')} {tail}"
                )
                # 摆事实、不给结论：有效/无效是人的判断，系统只负责把前后数并排放好。
                cmp = r.get("comparison") or {}
                changes = cmp.get("changes") or r.get("changes") or []
                for ch in changes[:3]:
                    lines.append(f"    {_fmt_change(ch)}")
                if cmp.get("state") == action_log.STATE_NO_DATA or (not changes and not cmp):
                    # 这一句不能省。空着会让人以为「没变化 = 动作无效」，
                    # 实际更可能是这几天压根没采到数、或者锚点一直没更新。
                    lines.append("    （没有可比的前后数据——是没采集，不是没变化）")
                elif not changes:
                    # 有数据但一点没动。这本身是个结论，不能和「没数据」混为一谈。
                    lines.append("    （动作前后指标没有任何变化）")
                if cmp.get("mine_stale"):
                    lines.append("    （自家锚点自那次动作以来没更新过，我的数不能当证据）")
                lines.append("    回面板「动作记录」页填复盘结论")
            if len(self.reviews) > 5:
                lines.append(f"（另有 {len(self.reviews) - 5} 条，见面板）")
            lines.append("")

        pool = self.pool or {}
        if pool:
            lines.append(
                "竞品池："
                + " · ".join(f"{k} 级 {pool.get(k, 0)} 个" for k in ("S", "A", "B", "C"))
            )

        dh = self.data_health or {}
        if dh.get("failed") or dh.get("partial"):
            lines.append("")
            if dh.get("failed"):
                lines.append(f"⚠ 采集失败：{', '.join(dh['failed'])}（这些竞品今日无数据）")
            if dh.get("partial"):
                lines.append(f"数据不全：{', '.join(dh['partial'])}（结论仅供参考）")

        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "anchor": self.anchor,
            "p0": [it.__dict__ for it in self.p0],
            "p1": [it.__dict__ for it in self.p1],
            "p2_count": self.p2_count,
            "pool": self.pool,
            "data_health": self.data_health,
            "quiet": self.quiet,
            "reviews": self.reviews,
        }


def _item_lines(idx: int, it: BriefItem) -> list[str]:
    out = [f"{idx}. [{it.tier}] {it.asin} {_short(it.title)}"]
    out.append(f"   {it.description}")
    if it.headline:
        out.append(f"   → {it.headline}")
    for a in it.actions[:3]:
        out.append(f"   · {a}")
    return out


def _short(text: str, limit: int = 28) -> str:
    text = (text or "").strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _fmt_change(ch: dict) -> str:
    """把一条结构化变化排成纯文本的一行。

    ## 为什么格式化放在这里，而不是 action_log 里

    `action_log.diff_metrics()` 只给 `{value, kind, delta, delta_pct}`，
    因为它同时喂给面板（JS 格式化）和纯文本（这里）。
    如果它在后端就把 `$` 拼进去，面板再按 kind 格式化一次，
    屏幕上就会出现 `$$19.99`——而这个错只有截图能发现，单测不会失败。

    所以「值类型由下游说、格式由展示层定」。这里是展示层。
    """
    kind = ch.get("kind")
    before = ch.get("before")
    after = ch.get("after")
    label = ch.get("label") or ch.get("key") or ""

    def one(v):
        if v is None:
            return "无数据"
        if kind == "money":
            return f"${v:,.2f}"
        if kind == "rank":
            return f"{int(v):,}"
        if kind == "rating":
            return f"{v:.1f}"
        if kind == "bool":
            return "有货" if v else "无货"
        if kind == "float1":
            return f"{v:.1f}"
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return f"{int(v):,}" if float(v).is_integer() else f"{v}"
        return str(v)

    tail = ""
    pct = ch.get("delta_pct")
    delta = ch.get("delta")
    if pct is not None and abs(pct) >= 0.1:
        tail = f"（{'+' if pct > 0 else ''}{pct:.1f}%）"
    elif delta is not None and delta != 0:
        sign = "+" if delta > 0 else ""
        tail = f"（{sign}{int(delta) if float(delta).is_integer() else delta}）"

    return f"{label} {one(before)} → {one(after)}{tail}"


# ============================================================
# 生成日报
# ============================================================


def build_daily_brief(
    tenant_id: Optional[str] = None,
    *,
    hours: int = 24,
    include_p1: bool = True,
    user_id: int | str | None = None,
) -> DailyBrief:
    """组装日报。

    `user_id` 本轮**不参与任何计算**，只是一个零成本的伏笔：
    下一轮要做「10 个人各按自己的阈值收不同的日报」时，
    推送入口只需要开始传这个参数，调用方签名不用再改一遍。

    为什么收 `int | str`：面板传的是 header 里的字符串，脚本传的是 int。
    在这里做兜底解析比让每个调用方各自 int() 一次更省事，也少一处不一致。

    也正因如此，本轮**不要**在这里读 `settings_store` 的个人覆盖——
    定时采集/日报是系统行为，没有自然人（见 DESIGN 待确认 #2）。
    """
    tenant_id = tenant_id or settings.tenant_id
    since = datetime.now() - timedelta(hours=hours)

    with get_session() as s:
        my = s.execute(
            select(MyProduct).where(MyProduct.tenant_id == tenant_id).limit(1)
        ).scalar_one_or_none()

        assessment = baseline_mod.assess(my) if my else None

        rows = list(
            s.execute(
                select(Signal, Competitor)
                .join(Competitor, Signal.competitor_id == Competitor.id)
                .where(Signal.tenant_id == tenant_id, Signal.detected_at >= since)
                .order_by(desc(Signal.detected_at))
            ).all()
        )

        pool: dict[str, int] = {}
        for c in s.execute(
            select(Competitor).where(
                Competitor.tenant_id == tenant_id, Competitor.status == "active"
            )
        ).scalars():
            pool[c.tier] = pool.get(c.tier, 0) + 1

        health = _data_health(s, tenant_id)

    brief = DailyBrief(
        date=datetime.now().strftime("%Y-%m-%d"),
        anchor=assessment.to_dict() if assessment else {},
        pool=pool,
        data_health=health,
    )

    # 同竞品同类信号只保留最新一条，避免同一件事报三次
    oos_comps = {c.id for sig, c in rows if sig.signal_type in ("out_of_stock", "low_stock")}
    seen: set[tuple[int, str]] = set()
    for sig, comp in rows:
        key = (comp.id, sig.signal_type)
        if key in seen:
            continue
        seen.add(key)

        if sig.level == "P2":
            brief.p2_count += 1
            continue

        item = BriefItem(
            asin=comp.asin,
            tier=comp.tier,
            title=comp.title or "",
            signal_type=sig.signal_type,
            level=sig.level,
            description=sig.description,
            competitor_id=comp.id,
            detected_at=sig.detected_at,
            signal_id=sig.id,
        )
        _attach_action(item, sig, comp, my, oos_comps)

        if sig.level == "P0":
            brief.p0.append(item)
        elif include_p1:
            brief.p1.append(item)
        else:
            brief.p2_count += 1

    brief.quiet = not brief.p0 and not brief.p1

    # 落库必须在会话关闭之后：`_attach_action` 内部不碰数据库，
    # 而这里再开一个写会话，就不会和上面的只读会话嵌套（SQLite 嵌套写会锁）。
    _persist_decisions(brief.p0 + brief.p1, tenant_id, origin=decision_log.ORIGIN_BRIEF)

    # 复盘队列和日报一起取：两件事都在「今天该看什么」这个范围里，
    # 分两次调用只会让面板多一个可能失败的请求。
    brief.reviews = action_log.due_reviews(tenant_id, limit=20)
    return brief


# ============================================================
# 给每条信号接上决策
# ============================================================


def _attach_action(
    item: BriefItem,
    sig: Signal,
    comp: Competitor,
    my: Optional[MyProduct],
    oos_comps: Optional[set[int]] = None,
) -> None:
    """把信号交给决策引擎，产出「然后呢」。

    ## 为什么每个分支都构造 `DecisionResult`，而不是直接往 item 上赋值

    第一版有六个分支直接写 `item.headline = ...`，只有两个分支走 `_apply()`。
    这两条路径的差别不在代码风格，在**带不带 `reasons` 和 `metrics`**：
    走 `_apply` 的能落进审计，直接赋值的落进去是一行没有理由的结论。

    症状会很隐蔽——`decisions` 表里每条都有 headline、都有动作建议，
    看起来很正常；只有真去查「断货那次为什么建议抢流量」时，才发现理由那一栏是空的。
    而这恰恰是最常被问起的一类（P0 大部分是断货和降价）。

    所以统一成一个出口：所有分支都产出 `DecisionResult`，由 `_apply` 一次性拷进 item。
    多写几行构造代码，换掉一整类「有的记录有理由、有的没有」的不一致。
    """
    dos = None
    if my:
        a = baseline_mod.assess(my)
        dos = a.days_of_supply

    # ---- 最高优先级：他断货了，价格信号全部让路 ----
    # 业务洞察：对手没货可卖时，他标多低都成交不了。这时候跟价是纯粹的自损，
    # 正确动作是抢流量，而不是把利润也降下去。
    if (
        sig.signal_type in ("price_drop", "coupon_increase", "deal_started", "price_band_break")
        and oos_comps
        and comp.id in oos_comps
    ):
        _apply(
            item,
            DecisionResult(
                decision="attack",
                headline="他同时断货了——价格已经不重要，直接抢流量",
                reasons=[
                    sig.description,
                    "对手无货可卖，他的低价成交不了；这时跟价只是白丢自己的利润",
                ],
                actions=[
                    "不要跟价。他没货，降价成交不了，你跟价只是白丢利润",
                    "优先 PAT 商品定位广告打他详情页，接住跳走的买家",
                    "他断货变体的核心出单词加竞价，抢自然位",
                    "观察他补货时间点，补货前是你的窗口",
                ],
                verify_days=7,
                confidence=0.85,
            ),
        )
        return

    # ---- 降价 / 深券 / Deal → 走跟价判断 ----
    if sig.signal_type in ("price_drop", "coupon_increase", "deal_started", "price_band_break"):
        if not my or not my.current_price or not my.break_even_price or not my.daily_sales_avg:
            missing = []
            if not my or not my.current_price:
                missing.append("当前售价")
            if not my or not my.break_even_price:
                missing.append("保本价")
            if not my or not my.daily_sales_avg:
                missing.append("日均销量")
            _apply(
                item,
                DecisionResult(
                    decision="no_action",
                    headline="判断不了该不该跟价——自家锚点缺数据",
                    reasons=[
                        sig.description,
                        f"缺 {('、'.join(missing)) or '关键字段'}，算不出盈亏平衡单量",
                    ],
                    actions=[
                        "补三样东西就能自动判断：当前售价、保本价、日均销量",
                        "在补齐之前，先按「不跟」处理，跟价是最贵的错误",
                    ],
                    # 缺数据不是「过几天再看」，是「现在补数据」。不设回看日。
                    verify_days=None,
                ),
            )
            return

        try:
            new_price = float(sig.after_value) if sig.after_value else my.current_price
        except (TypeError, ValueError):
            new_price = my.current_price

        unit_profit = my.current_price * (my.target_margin_rate or 0.2)
        _apply(
            item,
            judge_price_follow(
                competitor_new_price=new_price,
                my_current_price=my.current_price,
                my_break_even_price=my.break_even_price,
                my_unit_profit_now=unit_profit,
                my_daily_units=my.daily_sales_avg,
                competitor_is_clearance=False,
                my_dos=dos,
            ),
        )
        return

    # ---- 断货 / 缺货 → 进攻窗口 ----
    if sig.signal_type in ("out_of_stock", "low_stock", "rank_crash", "buybox_changed"):
        res = build_out_of_stock_decision(
            competitor_asin=comp.asin,
            my_dos=dos,
            is_low_stock=sig.signal_type == "low_stock",
        )
        if sig.signal_type != "out_of_stock":
            # 非断货场景复用动作清单，但把理由补准确
            res.reasons = [sig.description] + res.reasons
        _apply(item, res)
        return

    # ---- 涨价 / 活动结束 → 提价窗口 ----
    if sig.signal_type in ("price_rise", "deal_ended"):
        _apply(
            item,
            DecisionResult(
                decision="no_action",
                headline="他在退出价格战，是你的提价窗口",
                reasons=[
                    sig.description,
                    "对手先松价，说明他也不想继续让利；这时先跟涨的是你，风险同样在你",
                ],
                actions=[
                    "先不动，观察 3 天确认他不再回降",
                    "确认后小幅提价试探（每次 ≤3%），盯转化率与 BSR",
                    "同步下调你的 Coupon 力度，把利润拿回来",
                ],
                # 观察 3 天 + 提价后再看几天，7 天正好。
                verify_days=7,
                confidence=0.5,
            ),
        )
        return

    # ---- 口碑裂缝 → 差评进攻 ----
    if sig.signal_type in ("rating_drop", "rating_below_floor"):
        _apply(
            item,
            DecisionResult(
                decision="attack",
                headline="他口碑出裂缝，可从差评里挖卖点",
                reasons=[
                    sig.description,
                    "口碑下滑往往是可描述的缺陷，而缺陷能被反向写成卖点",
                ],
                actions=[
                    "把他近 30 天差评全拉出来，按「可解决 / 不可解决」分类",
                    "可解决的那类，直接写进你的图片与五点",
                    "不可解决的（如尺寸、材质），在主图区先做预期管理",
                ],
                # listing 改动的效果要一到两周才反映到转化上。
                verify_days=14,
                confidence=0.55,
            ),
        )
        return

    # ---- 他在加速 → 学习 ----
    if sig.signal_type in ("rank_climb", "review_surge", "main_image_changed", "back_in_stock"):
        _apply(
            item,
            DecisionResult(
                decision="learn",
                headline="他在做有效动作，先抄作业再谈对抗",
                reasons=[
                    sig.description,
                    "对手数据在变好，说明他最近的动作有效——先弄清楚是什么",
                ],
                actions=[
                    "拆他的新主图/新标题，找出他主打的那个词",
                    "查这个词的搜索量与竞争度，判断值不值得跟",
                    "7 天后回看他的 BSR，验证这招是否真有效",
                ],
                verify_days=7,
                confidence=0.5,
            ),
        )
        return

    # ---- 兜底 ----
    _apply(
        item,
        DecisionResult(
            decision="no_action",
            headline="已记录，无标准动作",
            reasons=[sig.description],
            actions=["在 Agent 里问「这条该怎么处理」可获得针对性建议"],
            verify_days=None,
        ),
    )


def _apply(item: BriefItem, res: DecisionResult) -> None:
    """把判断结果拷到 item 上。**这是唯一出口**，见 `_attach_action` 的文档。"""
    item.decision = res.decision
    item.headline = res.headline
    item.reasons = list(res.reasons)
    item.actions = list(res.actions)
    item.metrics = dict(res.metrics or {})
    item.confidence = res.confidence
    item.verify_days = res.verify_days


# ============================================================
# 把判断写进库
# ============================================================


def _persist_decisions(
    items: list[BriefItem], tenant_id: str, *, origin: str
) -> dict:
    """把装配好的判断落库，并把 `decision_id` 回填到 item 上。

    ## 为什么放在调用方做，而不是塞进 `_attach_action`

    `_attach_action` 在 `build_alerts_since` 里是**在数据库会话内部**被调用的
    （同一个会话里还要写 `sig.pushed_at`）。它内部再去开会话写 decisions，
    就是嵌套写，SQLite(WAL) 下会撞 `database is locked`——而且这种错只在
    空库首启、或者定时任务与面板撞车时才出现，测试里很难复现。
    所以装配保持纯函数，落库统一放在会话关闭之后。

    ## 为什么两条出口都要调

    「实时预警」和「日报」共用 `_attach_action`，如果只在日报里落库，
    P0 推送出去的那些判断就永远查不到——而 P0 恰恰是最需要交代「凭什么」的那批。
    两条路径都调的代价是两行代码，漏掉的代价是审计表有一个看起来正常的空洞。

    ## 回填 `decision_id` 的意义

    落库之后 item 上有了决策 ID，动作记录就能挂上它。
    没有回填的话，`decisions` 和 `actions` 是两张互不相认的表，
    「这条动作依据的是哪次判断」仍然要靠时间戳猜。
    """
    if not items:
        return {"ok": True, "written": 0, "duplicate": 0, "skipped": 0}

    result = decision_log.persist(
        [
            {
                "signal_id": it.signal_id,
                "competitor_id": it.competitor_id,
                "decision": it.decision,
                "headline": it.headline,
                "reasons": list(it.reasons),
                "actions": list(it.actions),
                "metrics": dict(it.metrics),
                "confidence": it.confidence,
                "verify_days": it.verify_days,
                "signal_type": it.signal_type,
            }
            for it in items
        ],
        tenant_id=tenant_id,
        origin=origin,
    )

    # 回填：按信号取「最近一次」的 ID。用一次批量查询而不是 N 次，
    # 因为这一步在面板刷新时也会走。
    ids = result.get("ids") or {}
    for it in items:
        if it.signal_id is not None:
            it.decision_id = ids.get(it.signal_id)
    return result


# ============================================================
# 数据健康度
# ============================================================


def _data_health(s: Session, tenant_id: str) -> dict:
    """最近一次采集的质量分布，让老板知道「今天的结论有多可信」。"""
    failed: list[str] = []
    partial: list[str] = []

    comps = list(
        s.execute(
            select(Competitor).where(
                Competitor.tenant_id == tenant_id, Competitor.status == "active"
            )
        ).scalars()
    )
    for c in comps:
        last = s.execute(
            select(Snapshot)
            .where(Snapshot.competitor_id == c.id)
            .order_by(desc(Snapshot.captured_at))
            .limit(1)
        ).scalar_one_or_none()
        if last is None:
            failed.append(c.asin)
        elif last.data_quality == "failed":
            failed.append(c.asin)
        elif last.data_quality == "partial":
            partial.append(c.asin)
        # 超过 36 小时没更新也算异常
        elif last.captured_at and (datetime.now() - last.captured_at) > timedelta(hours=36):
            partial.append(f"{c.asin}(数据过期)")

    return {"failed": failed, "partial": partial}


# ============================================================
# 单条预警（P0 实时推送用）
# ============================================================


def build_alerts_since(tenant_id: str, minutes: int = 90) -> list[BriefItem]:
    """取出最近 N 分钟内新产生的 P0 信号，带上决策与动作。

    与日报共用同一套 `_attach_action`，保证「实时预警」和「日报」
    对同一件事给出的建议不会打架。
    """
    since = datetime.now() - timedelta(minutes=minutes)
    out: list[BriefItem] = []

    with get_session() as s:
        my = s.execute(
            select(MyProduct).where(MyProduct.tenant_id == tenant_id).limit(1)
        ).scalar_one_or_none()

        rows = list(
            s.execute(
                select(Signal, Competitor)
                .join(Competitor, Signal.competitor_id == Competitor.id)
                .where(
                    Signal.tenant_id == tenant_id,
                    Signal.level == "P0",
                    Signal.detected_at >= since,
                    Signal.pushed_at.is_(None),
                )
                .order_by(desc(Signal.detected_at))
            ).all()
        )

        seen: set[tuple[int, str]] = set()
        oos_comps = {c.id for sg, c in rows if sg.signal_type in ("out_of_stock", "low_stock")}
        for sig, comp in rows:
            key = (comp.id, sig.signal_type)
            if key in seen:
                continue
            seen.add(key)

            item = BriefItem(
                asin=comp.asin,
                tier=comp.tier,
                title=comp.title or "",
                signal_type=sig.signal_type,
                level=sig.level,
                description=sig.description,
                competitor_id=comp.id,
                detected_at=sig.detected_at,
                signal_id=sig.id,
            )
            _attach_action(item, sig, comp, my, oos_comps)
            out.append(item)
            sig.pushed_at = datetime.now()

    # 会话已关，再落库——理由见 `_persist_decisions` 的文档。
    # P0 是最需要交代「凭什么」的那批判断，所以这个出口必须落库。
    _persist_decisions(out, tenant_id, origin=decision_log.ORIGIN_ALERT)
    return out


def build_alert_text(item: BriefItem) -> str:
    lines = [
        f"⚡ {item.level} · {item.tier} 级竞品 {item.asin}",
        "",
        item.description,
    ]
    if item.headline:
        lines += ["", f"→ {item.headline}"]
    if item.actions:
        lines.append("")
        lines += [f"· {a}" for a in item.actions[:4]]
    if item.confidence is not None:
        lines += ["", f"（判断确定性 {item.confidence:.0%}）"]
    return "\n".join(lines)
