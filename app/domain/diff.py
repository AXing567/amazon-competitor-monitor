"""步骤 2：变化监控 —— DiffEngine 与信号分级。

纯函数模块，不碰数据库、不发网络请求。输入前后两份快照，输出一组信号草稿。
这是「采集」与「推送」之间的唯一判定关口：
- 采集可以高频静默，但只有这里判定为 P0/P1 的才允许进推送队列。
- P2 一律归档，只在周报/日报里体现，不单独打扰。

阈值全部集中在顶部 RULES，方便老板按自己的类目调。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.adapters.unified import UnifiedSnapshot

# ============================================================
# 阈值表（唯一调参入口）
# ============================================================

RULES = {
    # ---- 价格 ----
    "price_p0_pct": 0.03,          # 净价变动 ≥3% → P0
    "price_p1_pct": 0.01,          # 1%~3% → P1（日报体现）
    "coupon_p0_pct": 0.05,         # 优惠券加深 ≥5% 视为一次实际降价
    "price_floor_guard": 0.005,    # 低于 0.5% 的变动视为噪声，直接丢弃

    # ---- 供给 ----
    "oos_confirm_minutes": 30,     # 断货需连续两次采集确认（由采集层传 confirmed）

    # ---- 排名 ----
    "rank_p0_pct": 0.30,           # BSR 单日变差 ≥30% → P0
    "rank_p1_pct": 0.15,
    "rank_min_abs_move": 20,       # 名次绝对移动小于 20 名不算信号（避免 2→3 判成 50%）

    # ---- 口碑 ----
    "review_p0_abs": 50,           # 单日新增评论 ≥50 条 → P0
    "review_p0_pct": 0.05,         # 或评论总量单日 +5%
    "rating_drop_p1": 0.20,        # 星级掉 ≥0.2 → P1
    "rating_floor": 4.0,           # 跌破 4.0 星单独报一次

    # ---- 季节系数 ----
    # 旺季（blast）错过的代价远高于误报的代价，阈值收紧到 0.8 倍。
    # 淡季则放宽到 1.3 倍，减少无意义打扰。
    "phase_scale": {"warmup": 1.0, "blast": 0.8, "cooldown": 1.3},
}

LEVEL_ORDER = {"P0": 0, "P1": 1, "P2": 2}


# ============================================================
# 信号草稿
# ============================================================


@dataclass
class SignalDraft:
    category: str                                # price / supply / rank / reputation / content
    signal_type: str                             # price_drop / out_of_stock / ...
    level: str                                   # P0 / P1 / P2
    description: str                             # 给人看的一句话
    before_value: Optional[str] = None
    after_value: Optional[str] = None
    change_pct: Optional[float] = None           # 带符号的变动百分比
    mainline: str = ""                           # 归属的判断主线，供步骤 3 路由
    meta: dict = field(default_factory=dict)     # 附加上下文，不落库

    def to_row(self) -> dict:
        return {
            "category": self.category,
            "signal_type": self.signal_type,
            "level": self.level,
            "description": self.description,
            "before_value": self.before_value,
            "after_value": self.after_value,
            "change_pct": self.change_pct,
        }


# ============================================================
# 主引擎
# ============================================================


class DiffEngine:
    """对比前后两份快照，产出信号列表。"""

    def __init__(self, phase: str = "warmup"):
        self.phase = phase if phase in RULES["phase_scale"] else "warmup"
        self.scale = RULES["phase_scale"][self.phase]

    def _t(self, key: str) -> float:
        return RULES[key] * self.scale

    # --------------------------------------------------------
    # 入口
    # --------------------------------------------------------

    def compare(
        self,
        prev: UnifiedSnapshot,
        curr: UnifiedSnapshot,
        *,
        tier: str = "C",
        my_price: Optional[float] = None,
        oos_confirmed: bool = True,
    ) -> list[SignalDraft]:
        """返回本次采集所有变化信号。

        tier:  竞品分层，影响信号等级（S 级的价格波动更值钱）
        my_price: 自家价格，用来判断「相对价格带漂移」
        oos_confirmed: 断货是否已被二次确认，未确认则降级为 P1
        """
        signals: list[SignalDraft] = []
        signals += self._diff_price(prev, curr, tier=tier, my_price=my_price)
        signals += self._diff_supply(prev, curr, tier=tier, oos_confirmed=oos_confirmed)
        signals += self._diff_rank(prev, curr, tier=tier)
        signals += self._diff_reputation(prev, curr, tier=tier)
        signals += self._diff_content(prev, curr)

        # 统一排序：P0 在前，同级按变动幅度绝对值降序
        signals.sort(key=lambda s: (LEVEL_ORDER.get(s.level, 9), -(abs(s.change_pct or 0))))
        return signals

    # --------------------------------------------------------
    # 价格
    # --------------------------------------------------------

    def _diff_price(
        self,
        prev: UnifiedSnapshot,
        curr: UnifiedSnapshot,
        *,
        tier: str,
        my_price: Optional[float],
    ) -> list[SignalDraft]:
        out: list[SignalDraft] = []

        p_prev = _effective_price(prev)
        p_curr = _effective_price(curr)

        if p_prev and p_curr and p_prev > 0:
            pct = (p_curr - p_prev) / p_prev
            if abs(pct) >= RULES["price_floor_guard"]:
                p0 = self._t("price_p0_pct")
                p1 = self._t("price_p1_pct")

                if pct <= -p0:
                    level = "P0"
                    txt = (
                        f"净价 ${p_prev:.2f} → ${p_curr:.2f}，降 {abs(pct):.1%}"
                    )
                    stype = "price_drop"
                elif pct <= -p1:
                    level = "P1"
                    txt = f"净价小幅下调 {abs(pct):.1%}（${p_prev:.2f} → ${p_curr:.2f}）"
                    stype = "price_drop"
                elif pct >= p0:
                    level = "P1"
                    txt = f"净价上调 {pct:.1%}（${p_prev:.2f} → ${p_curr:.2f}）"
                    stype = "price_rise"
                else:
                    level = "P2"
                    txt = f"净价微调 {pct:+.1%}（${p_prev:.2f} → ${p_curr:.2f}）"
                    stype = "price_rise" if pct > 0 else "price_drop"

                # S/A 级的 3% 与 C 级的 3% 不是一回事：越核心越必须立刻知道
                if level == "P1" and tier in ("S", "A") and abs(pct) >= p0 * 0.7:
                    level = "P0"

                out.append(
                    SignalDraft(
                        category="price",
                        signal_type=stype,
                        level=level,
                        description=txt,
                        before_value=f"{p_prev:.2f}",
                        after_value=f"{p_curr:.2f}",
                        change_pct=round(pct * 100, 2),
                        mainline="主线1-价格战" if stype == "price_drop" else "主线2-跟涨",
                        meta={"my_price_at_detect": my_price},
                    )
                )

                # 跌破自家价格带下沿，单独开一条——这是「战场位移」而不是普通降价
                if my_price and p_curr <= my_price * 0.7 and p_prev > my_price * 0.7:
                    out.append(
                        SignalDraft(
                            category="price",
                            signal_type="price_band_break",
                            level="P0",
                            description=(
                                f"对手价格跌到你价格的 {p_curr / my_price:.0%}，"
                                f"已脱离同一价格带，可能转打低端人群"
                            ),
                            before_value=f"{p_prev:.2f}",
                            after_value=f"{p_curr:.2f}",
                            change_pct=round((p_curr / my_price - 1) * 100, 2),
                            mainline="主线1-价格战",
                        )
                    )

        # ---- 优惠券加深 ----
        if prev.coupon_value is not None and curr.coupon_value is not None and p_prev:
            coupon_delta = curr.coupon_value - prev.coupon_value
            coupon_pct = coupon_delta / p_prev
            if coupon_pct >= RULES["coupon_p0_pct"]:
                out.append(
                    SignalDraft(
                        category="price",
                        signal_type="coupon_increase",
                        level="P0",
                        description=(
                            f"优惠券从 ${prev.coupon_value:.2f} 加到 ${curr.coupon_value:.2f}，"
                            f"相当于又降了 {coupon_pct:.1%}（标价没动，容易被漏看）"
                        ),
                        before_value=f"{prev.coupon_value:.2f}",
                        after_value=f"{curr.coupon_value:.2f}",
                        change_pct=round(coupon_pct * 100, 2),
                        mainline="主线1-价格战",
                    )
                )

        # ---- 新开 Deal ----
        if not prev.deal_type and curr.deal_type:
            out.append(
                SignalDraft(
                    category="price",
                    signal_type="deal_started",
                    level="P0",
                    description=f"新上活动：{curr.deal_type}",
                    before_value="无",
                    after_value=str(curr.deal_type),
                    mainline="主线1-价格战",
                )
            )
        elif prev.deal_type and not curr.deal_type:
            out.append(
                SignalDraft(
                    category="price",
                    signal_type="deal_ended",
                    level="P1",
                    description=f"活动结束：{prev.deal_type} 已下线，价格大概率回弹",
                    before_value=str(prev.deal_type),
                    after_value="无",
                    mainline="主线2-跟涨",
                )
            )

        return out

    # --------------------------------------------------------
    # 供给
    # --------------------------------------------------------

    def _diff_supply(
        self,
        prev: UnifiedSnapshot,
        curr: UnifiedSnapshot,
        *,
        tier: str,
        oos_confirmed: bool,
    ) -> list[SignalDraft]:
        out: list[SignalDraft] = []

        prev_hint = (prev.stock_hint or "").upper()
        curr_hint = (curr.stock_hint or "").upper()

        # ---- 断货 ----
        became_oos = (
            (prev.in_stock is True and curr.in_stock is False)
            or (prev_hint == "IN_STOCK" and curr_hint == "OOS")
        )
        if became_oos:
            # 断货一律 P0，不等二次确认。
            # 理由：误报一次只是让你白高兴、多花点广告费；漏报一次就错过整个真空窗。
            # 代价不对称时，就该选「宁可多报」。oos_confirmed 只影响措辞。
            out.append(
                SignalDraft(
                    category="supply",
                    signal_type="out_of_stock",
                    level="P0",
                    description=(
                        "对手已断货（连续两次采集确认），流量正在外溢"
                        if oos_confirmed
                        else "对手显示断货（首次发现，建议上亚马逊页面确认一眼）"
                    ),
                    before_value=prev_hint or "IN_STOCK",
                    after_value="OOS",
                    mainline="主线1-进攻窗口",
                    meta={"confirmed": oos_confirmed},
                )
            )

        # ---- 库存转紧 ----
        if prev_hint == "IN_STOCK" and curr_hint == "LOW_STOCK":
            out.append(
                SignalDraft(
                    category="supply",
                    signal_type="low_stock",
                    level="P1",
                    description="库存转紧（仅剩少量），供应出现缺口",
                    before_value="IN_STOCK",
                    after_value="LOW_STOCK",
                    mainline="主线1-进攻窗口",
                )
            )

        # ---- 恢复上架 ----
        became_in = (
            (prev.in_stock is False and curr.in_stock is True)
            or (prev_hint == "OOS" and curr_hint == "IN_STOCK")
        )
        if became_in:
            out.append(
                SignalDraft(
                    category="supply",
                    signal_type="back_in_stock",
                    level="P1",
                    description="对手恢复上架，进攻窗口关闭，注意他补货后的冲量动作",
                    before_value="OOS",
                    after_value="IN_STOCK",
                    mainline="主线3-防守学习",
                )
            )

        # ---- Buybox 易主 ----
        if prev.buybox_owner and curr.buybox_owner and prev.buybox_owner != curr.buybox_owner:
            out.append(
                SignalDraft(
                    category="supply",
                    signal_type="buybox_changed",
                    level="P1",
                    description=f"Buybox 易主：{prev.buybox_owner} → {curr.buybox_owner}",
                    before_value=prev.buybox_owner,
                    after_value=curr.buybox_owner,
                    mainline="主线1-进攻窗口",
                )
            )

        return out

    # --------------------------------------------------------
    # 排名
    # --------------------------------------------------------

    def _diff_rank(
        self,
        prev: UnifiedSnapshot,
        curr: UnifiedSnapshot,
        *,
        tier: str,
    ) -> list[SignalDraft]:
        out: list[SignalDraft] = []

        prev_r = prev.rank_sub or prev.rank_main
        curr_r = curr.rank_sub or curr.rank_main
        if not prev_r or not curr_r or prev_r <= 0:
            return out

        move = curr_r - prev_r            # 正数 = 名次变差
        if abs(move) < RULES["rank_min_abs_move"]:
            return out

        pct = move / prev_r
        if pct >= self._t("rank_p0_pct"):
            out.append(
                SignalDraft(
                    category="rank",
                    signal_type="rank_crash",
                    level="P0",
                    description=f"BSR 从 {prev_r} 掉到 {curr_r}（变差 {pct:.0%}），可能是掉流量或断货前兆",
                    before_value=str(prev_r),
                    after_value=str(curr_r),
                    change_pct=round(pct * 100, 2),
                    mainline="主线1-进攻窗口",
                )
            )
        elif pct <= -self._t("rank_p0_pct"):
            out.append(
                SignalDraft(
                    category="rank",
                    signal_type="rank_climb",
                    level="P1",
                    description=f"BSR 从 {prev_r} 冲到 {curr_r}（提升 {abs(pct):.0%}），他在加速",
                    before_value=str(prev_r),
                    after_value=str(curr_r),
                    change_pct=round(pct * 100, 2),
                    mainline="主线3-防守学习",
                )
            )
        elif abs(pct) >= self._t("rank_p1_pct"):
            out.append(
                SignalDraft(
                    category="rank",
                    signal_type="rank_crash" if move > 0 else "rank_climb",
                    level="P2",
                    description=f"BSR {prev_r} → {curr_r}（{pct:+.0%}）",
                    before_value=str(prev_r),
                    after_value=str(curr_r),
                    change_pct=round(pct * 100, 2),
                    mainline="归档",
                )
            )
        return out

    # --------------------------------------------------------
    # 口碑
    # --------------------------------------------------------

    def _diff_reputation(
        self,
        prev: UnifiedSnapshot,
        curr: UnifiedSnapshot,
        *,
        tier: str,
    ) -> list[SignalDraft]:
        out: list[SignalDraft] = []

        if prev.rating is not None and curr.rating is not None:
            drop = prev.rating - curr.rating
            if drop >= RULES["rating_drop_p1"]:
                out.append(
                    SignalDraft(
                        category="reputation",
                        signal_type="rating_drop",
                        level="P1",
                        description=f"星级 {prev.rating:.1f} → {curr.rating:.1f}，口碑出现裂缝",
                        before_value=f"{prev.rating:.1f}",
                        after_value=f"{curr.rating:.1f}",
                        change_pct=round(-drop / prev.rating * 100, 2),
                        mainline="主线4-差评进攻",
                    )
                )
            # 跌破 4.0 是转化率的心理分水岭，单独报一次
            if prev.rating >= RULES["rating_floor"] > curr.rating:
                out.append(
                    SignalDraft(
                        category="reputation",
                        signal_type="rating_below_floor",
                        level="P0",
                        description=f"星级跌破 4.0（现 {curr.rating:.1f}），转化率会明显下滑",
                        before_value=f"{prev.rating:.1f}",
                        after_value=f"{curr.rating:.1f}",
                        mainline="主线4-差评进攻",
                    )
                )

        if prev.review_count is not None and curr.review_count is not None:
            delta = curr.review_count - prev.review_count
            if delta > 0:
                pct = delta / max(prev.review_count, 1)
                if delta >= RULES["review_p0_abs"] or pct >= RULES["review_p0_pct"]:
                    out.append(
                        SignalDraft(
                            category="reputation",
                            signal_type="review_surge",
                            level="P0",
                            description=(
                                f"评论一天涨了 {delta} 条（{prev.review_count} → {curr.review_count}），"
                                f"大概率在站内催评或合并变体"
                            ),
                            before_value=str(prev.review_count),
                            after_value=str(curr.review_count),
                            change_pct=round(pct * 100, 2),
                            mainline="主线3-防守学习",
                        )
                    )
            elif delta < 0:
                out.append(
                    SignalDraft(
                        category="reputation",
                        signal_type="review_decrease",
                        level="P2",
                        description=f"评论数减少 {abs(delta)} 条（{prev.review_count} → {curr.review_count}），可能是差评被删或变体拆分",
                        before_value=str(prev.review_count),
                        after_value=str(curr.review_count),
                        mainline="归档",
                    )
                )

        return out

    # --------------------------------------------------------
    # 内容
    # --------------------------------------------------------

    def _diff_content(self, prev: UnifiedSnapshot, curr: UnifiedSnapshot) -> list[SignalDraft]:
        """内容改动一律 P2，进周报。只有主图换了才值得单独提一句。"""
        out: list[SignalDraft] = []

        if prev.title and curr.title and _norm(prev.title) != _norm(curr.title):
            out.append(
                SignalDraft(
                    category="content",
                    signal_type="title_changed",
                    level="P2",
                    description="标题被改动（可能换了主打词）",
                    before_value=_clip(prev.title),
                    after_value=_clip(curr.title),
                    mainline="归档",
                )
            )

        if prev.image_url and curr.image_url and prev.image_url != curr.image_url:
            out.append(
                SignalDraft(
                    category="content",
                    signal_type="main_image_changed",
                    level="P1",
                    description="主图换了——换图是最贵也最见效的动作，值得看他换成了什么",
                    before_value=prev.image_url,
                    after_value=curr.image_url,
                    mainline="主线3-防守学习",
                )
            )

        return out


# ============================================================
# 工具函数
# ============================================================


def _effective_price(s: UnifiedSnapshot) -> Optional[float]:
    """取「买家实际付的钱」：优先净价，其次标价。"""
    return s.net_price if s.net_price is not None else s.price


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _clip(text: str, limit: int = 120) -> str:
    text = text or ""
    return text[:limit] + ("…" if len(text) > limit else "")


def highest_level(signals: list[SignalDraft]) -> Optional[str]:
    if not signals:
        return None
    return min((s.level for s in signals), key=lambda x: LEVEL_ORDER.get(x, 9))


def split_by_level(signals: list[SignalDraft]) -> dict[str, list[SignalDraft]]:
    buckets: dict[str, list[SignalDraft]] = {"P0": [], "P1": [], "P2": []}
    for s in signals:
        buckets.setdefault(s.level, []).append(s)
    return buckets


def should_push(signals: list[SignalDraft], notify_level: str = "daily") -> bool:
    """推送闸门。notify_level: p0_only / daily / all。

    采集可以一天跑 24 次，但推送必须克制——这条闸门就是克制的实现。
    """
    lv = highest_level(signals)
    if lv is None:
        return False
    if notify_level == "p0_only":
        return lv == "P0"
    if notify_level == "daily":
        return lv in ("P0", "P1")
    return True
