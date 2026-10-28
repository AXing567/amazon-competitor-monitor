"""步骤 3：决策引擎。

所有决策函数都是纯函数——输入数据，输出结论 + 理由，不碰数据库。
这样既好测试，也方便 Agent 直接调用。

对应《竞品监控业务全景手册》步骤 3 的五条主线与两个专用算法。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ============================================================
# 通用返回结构
# ============================================================


@dataclass
class DecisionResult:
    decision: str                                   # attack / defend / learn / no_action
    headline: str                                   # 一句话结论
    reasons: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    verify_days: int = 7
    confidence: Optional[float] = None              # 0~1，判断确定性

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "headline": self.headline,
            "reasons": self.reasons,
            "actions": self.actions,
            "metrics": self.metrics,
            "verify_days": self.verify_days,
            "confidence": self.confidence,
        }


# ============================================================
# 算法一：跟价判断（盈亏平衡单量）
# ============================================================


def calc_break_even_units(
    *,
    current_price: float,
    current_daily_units: float,
    unit_profit_now: float,
    new_price: float,
    unit_cost: float,
) -> dict:
    """算降价后的盈亏平衡单量。

    unit_cost = 售价 - 单均利润（含采购/头程/平台费）
    返回：原日利润、降价后单均利润、需要达到的日销、需要的涨幅
    """
    base_daily_profit = unit_profit_now * current_daily_units
    new_unit_profit = new_price - unit_cost

    out = {
        "current_price": round(current_price, 2),
        "new_price": round(new_price, 2),
        "current_unit_profit": round(unit_profit_now, 2),
        "new_unit_profit": round(new_unit_profit, 2),
        "base_daily_profit": round(base_daily_profit, 2),
        "current_daily_units": current_daily_units,
        "break_even_units": None,
        "required_growth_pct": None,
        "viable": None,
    }

    if new_unit_profit <= 0:
        out["viable"] = False
        out["note"] = "降价后单均利润 ≤ 0，属于亏本销售，直接否决"
        return out

    be_units = base_daily_profit / new_unit_profit
    out["break_even_units"] = round(be_units, 1)
    if current_daily_units > 0:
        out["required_growth_pct"] = round((be_units / current_daily_units - 1) * 100, 1)
    return out


def judge_price_follow(
    *,
    competitor_new_price: float,
    my_current_price: float,
    my_break_even_price: float,
    my_unit_profit_now: float,
    my_daily_units: float,
    competitor_is_clearance: bool = False,
    market_players: int = 1,
    my_dos: Optional[float] = None,
) -> DecisionResult:
    """主线 1：对手降价，我该不该跟。"""
    reasons: list[str] = []
    actions: list[str] = []
    res = DecisionResult(decision="no_action", headline="", verify_days=14)

    # ---- 第一道：保本线 ----
    if competitor_new_price < my_break_even_price:
        reasons.append(
            f"对手降到 ${competitor_new_price:.2f}，已跌破你的保本价 ${my_break_even_price:.2f}"
        )
        unit_cost = my_current_price - my_unit_profit_now
        be = calc_break_even_units(
            current_price=my_current_price,
            current_daily_units=my_daily_units,
            unit_profit_now=my_unit_profit_now,
            new_price=competitor_new_price,
            unit_cost=unit_cost,
        )
        if be["new_unit_profit"] <= 0:
            reasons.append(
                f"跟到这个价位每单亏 ${abs(be['new_unit_profit']):.2f}，属于纯亏"
            )
        res.headline = "建议不跟价"
        res.decision = "no_action"
        res.metrics["break_even"] = be
        res.actions = [
            "用 PAT 商品定位广告打到对手详情页，抢占他已获得的流量",
            "检查自家差评与对手弱点的交集，在主图和 A+ 强化对应卖点",
            "如需让利，用 Coupon 而非改标价（保护价格锚点与大促资格）",
        ]
        res.confidence = 0.9
        res.reasons = reasons
        return res

    # ---- 第二道：清仓判断 ----
    if competitor_is_clearance:
        reasons.append("判定为清仓抛货，卖完就走，跟价等于陪他一起亏")
        res.headline = "建议不跟价，等他清完抓回弹"
        res.metrics["strategy"] = "wait_for_rebound"
        res.actions = [
            "不跟进价格，保持现价",
            "估算他清完库存的日期，提前备货承接回弹需求",
            "清完那一刻市场价会回升，这是提价窗口",
        ]
        res.verify_days = 30
        res.confidence = 0.8
        res.reasons = reasons
        return res

    # ---- 第三道：库存能不能接 ----
    if my_dos is not None and my_dos < 20:
        reasons.append(f"你的可售天数只有 {my_dos:.0f} 天，撑不住降价带来的增量")
        res.headline = "不建议跟价，库存撑不住"
        res.actions = ["先补货，再谈进攻", "只做低成本的广告位调整"]
        res.confidence = 0.85
        res.reasons = reasons
        return res

    # ---- 第四道：算盈亏平衡单量 ----
    unit_cost = my_current_price - my_unit_profit_now
    be = calc_break_even_units(
        current_price=my_current_price,
        current_daily_units=my_daily_units,
        unit_profit_now=my_unit_profit_now,
        new_price=competitor_new_price,
        unit_cost=unit_cost,
    )
    res.metrics["break_even"] = be

    if be["break_even_units"] and be["required_growth_pct"] is not None:
        reasons.append(
            f"跟到 ${competitor_new_price:.2f} 后单均利润 ${be['new_unit_profit']:.2f}，"
            f"日销需要涨到 {be['break_even_units']:.0f} 单（+{be['required_growth_pct']:.0f}%）才不亏"
        )

    # ---- 第五道：战场玩家数 ----
    if market_players >= 4:
        reasons.append(
            f"该战场有 {market_players} 个玩家在抢同一批词，你降价换来的份额会被第三方分走"
        )
        res.headline = "建议不跟价，跟了也不一定拿得到份额"
        res.actions = [
            "走精准长尾词，避开大词硬碰",
            "用 PAT 拦截对手详情页流量",
        ]
        res.confidence = 0.7
        res.reasons = reasons
        return res

    # ---- 通过所有关卡：可以跟 ----
    res.decision = "attack"
    res.headline = "可以跟进降价"
    reasons.append("未跌破保本线，库存充足，战场玩家不多，跟价能拿到份额")
    res.actions = [
        f"用 Coupon 让利到 ${competitor_new_price:.2f} 附近，不改标价",
        "同步观察 3~5 天的转化率与 BSR 变化",
        "若对手二次降价，立即停止跟进并重新评估",
    ]
    res.confidence = 0.6
    res.reasons = reasons
    return res


# ============================================================
# 算法二：清仓识别
# ============================================================


def judge_clearance(
    *,
    price_drop_pct: Optional[float] = None,
    stock_declining: Optional[bool] = None,
    no_restock: Optional[bool] = None,
    ads_stopped: Optional[bool] = None,
    listing_frozen: Optional[bool] = None,
    brand_shrinking: Optional[bool] = None,
    historical_seasonal: Optional[bool] = None,
    deep_coupon: Optional[bool] = None,
    variants_delisted: Optional[bool] = None,
) -> DecisionResult:
    """清仓置信度打分。只用拿得到的信号，未知项不计分。"""
    score = 0
    hits: list[str] = []
    unknowns: list[str] = []

    if historical_seasonal is True:
        # 历史同期重演 → 直接定性为季节性清仓
        return DecisionResult(
            decision="no_action",
            headline="季节性清仓，判定完成",
            reasons=["历史同期出现过相同降价并随后回升，属于常规季节节奏"],
            actions=["不跟价，按往年节奏准备回弹期的库存"],
            confidence=0.95,
            verify_days=30,
        )

    if price_drop_pct is not None:
        if price_drop_pct >= 20:
            score += 25
            hits.append(f"断崖式降价 {price_drop_pct:.0f}%")
        elif price_drop_pct >= 10:
            score += 10
            hits.append(f"降价 {price_drop_pct:.0f}%")

    if stock_declining and no_restock:
        score += 40
        hits.append("库存单边下降且不补货")
    elif stock_declining:
        score += 15
        hits.append("库存持续下降")
    elif no_restock is False:
        unknowns.append("仍在正常补货")

    if ads_stopped is True:
        score += 20
        hits.append("核心词广告位全部消失")
    elif ads_stopped is None:
        unknowns.append("广告状态未采集")

    if listing_frozen is True:
        score += 10
        hits.append("Listing 停止维护")

    if brand_shrinking is True:
        score += 20
        hits.append("同品牌其他 ASIN 同步收缩")

    if deep_coupon is True:
        score += 10
        hits.append("挂 40% 以上深券却不报 Deal")

    if variants_delisted is True:
        score += 10
        hits.append("变体逐步下架")

    confidence = min(score / 100, 1.0)
    hit_names = "、".join(hits) if hits else "无"

    if score >= 50 and (stock_declining and no_restock):
        return DecisionResult(
            decision="no_action",
            headline="大概率在清仓，甚至可能退出市场",
            reasons=[f"命中信号：{hit_names}", f"清仓置信度 {confidence:.0%}"],
            actions=[
                "不跟价",
                "估算他清完的日期，提前备货承接回弹需求",
                "确认结束后拉进 S 级监控，接手他的核心词坑位",
            ],
            metrics={"clearance_score": score, "hits": hits, "unknowns": unknowns},
            confidence=confidence,
            verify_days=30,
        )

    if score >= 30:
        return DecisionResult(
            decision="no_action",
            headline="有清仓迹象，建议观望",
            reasons=[f"命中信号：{hit_names}", f"清仓置信度 {confidence:.0%}，未达确认线"],
            actions=[
                "暂不跟价，继续观察 7~14 天",
                "补采广告位与库存数据，提高判断确定性",
            ],
            metrics={"clearance_score": score, "hits": hits, "unknowns": unknowns},
            confidence=confidence,
            verify_days=14,
        )

    return DecisionResult(
        decision="no_action",
        headline="不像清仓，按常规价格战处理",
        reasons=[
            f"命中信号：{hit_names}",
            f"清仓置信度仅 {confidence:.0%}",
            "判错代价不对称，宁可保守按价格战处理",
        ],
        actions=["按价格战逻辑走 judge_price_follow 判断是否跟价"],
        metrics={"clearance_score": score, "hits": hits, "unknowns": unknowns},
        confidence=1 - confidence,
        verify_days=14,
    )


# ============================================================
# 主线调度：拿到信号后该走哪条判断
# ============================================================


SIGNAL_TO_MAINLINE = {
    "price_drop": "主线1-价格战",
    "price_rise": "主线2-跟涨",
    "coupon_increase": "主线1-价格战",
    "deal_started": "主线1-价格战",
    "out_of_stock": "主线1-进攻窗口",
    "low_stock": "主线1-进攻窗口",
    "buybox_lost": "主线1-进攻窗口",
    "content_changed": "主线3-防守学习",
    "rating_drop": "主线4-差评进攻",
    "review_surge": "主线3-防守学习",
    "rank_crash": "主线1-进攻窗口",
    "rank_climb": "主线3-防守学习",
}


def route_signal(signal_type: str) -> str:
    return SIGNAL_TO_MAINLINE.get(signal_type, "归档")


def build_out_of_stock_decision(
    *,
    competitor_asin: str,
    my_dos: Optional[float],
    is_low_stock: bool = False,
) -> DecisionResult:
    """对手断货 / 缺货时的标准动作。"""
    reasons: list[str] = []
    actions: list[str] = []

    if is_low_stock:
        reasons.append("对手显示低库存，还没完全断，属于抢时间差的机会")
        actions.append("轻投入：核心词小额加价，观察 2~3 天他是否真断")
        res = DecisionResult(
            decision="attack",
            headline="对手快断货，可轻仓抢一波",
            reasons=reasons,
            actions=actions,
            verify_days=7,
            confidence=0.6,
        )
        return res

    reasons.append("对手已断货，流量正在外溢")

    if my_dos is not None and my_dos < 20:
        reasons.append(f"但你的可售天数只有 {my_dos:.0f} 天，不能降价冲量")
        actions = [
            "只做 PAT 商品定位广告，打到他详情页接流量",
            "断货变体的核心出单词适度加价，不降价",
            "不要改标价，避免影响历史低价记录",
        ]
        return DecisionResult(
            decision="attack",
            headline="可以抢流量，但不能降价",
            reasons=reasons,
            actions=actions,
            verify_days=7,
            confidence=0.8,
        )

    actions = [
        "① PAT 广告打到他详情页（买家看到 unavailable 会跳走）",
        "② 他断货变体的核心出单词提高竞价",
        "③ 抢关联位（看了又看 / 一起购买）",
        "④ 视毛利空间挂小幅 Coupon，不改标价",
    ]
    reasons.append("先查他断货时长：短期几天就轻仓抢，长期下架就持续加码接手词坑")
    return DecisionResult(
        decision="attack",
        headline="真空窗，全力抢位",
        reasons=reasons,
        actions=actions,
        verify_days=7,
        confidence=0.85,
    )
