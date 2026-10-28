"""步骤 0：定自家锚点。

竞品监控的所有判断，都是「和谁比、比什么、比多少算异常」。
没有自家锚点，后面全部算法都是空转：
- 没有自家价 → 算不出价格比 → 四同法则失效
- 没有自家核心词 → 算不出词重合度 → 分层失效
- 没有自家保本价 → 跟价判断无法给结论
- 没有自家日均销量与库存 → 可售天数算不出来 → 进攻窗口不敢下手

本模块只做三件事：
1. 体检：告诉你锚点缺了什么，缺了会导致哪个判断失灵
2. 补全：能从公开数据自动填的，自动填
3. 派生：算 DOS / 库存状态 / 可判断的能力边界
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.adapters.unified import UnifiedSnapshot

# ============================================================
# 五大维度定义
# ============================================================


@dataclass
class Dimension:
    key: str
    name: str
    fields: list[tuple[str, str]]        # [(字段名, 中文名)]
    why: str                             # 缺了会导致什么判断失效
    auto_fillable: bool                  # 能否从公开数据自动填


DIMENSIONS: list[Dimension] = [
    Dimension(
        key="supply",
        name="供给锚点",
        fields=[
            ("fba_available_units", "FBA 可售库存"),
            ("daily_sales_avg", "日均销量"),
            ("lead_time_days", "补货周期（天）"),
        ],
        why="缺库存与日均销量就算不出可售天数 DOS，进攻窗口不敢下手，跟价可能把货卖断",
        auto_fillable=False,
    ),
    Dimension(
        key="finance",
        name="财务锚点",
        fields=[
            ("current_price", "当前售价"),
            ("break_even_price", "保本价"),
            ("target_margin_rate", "目标毛利率"),
        ],
        why="缺保本价，跟价判断只能瞎猜；这是价格战里唯一不能妥协的红线",
        auto_fillable=True,
    ),
    Dimension(
        key="rank",
        name="排位与口碑",
        fields=[
            ("sub_category_rank", "子类目 BSR"),
            ("rating", "星级"),
            ("review_count", "评论数"),
        ],
        why="缺了无法判断自己被打掉了多少名次，也判断不了对手追赶速度",
        auto_fillable=True,
    ),
    Dimension(
        key="keywords",
        name="核心出单词",
        fields=[("core_keywords", "Top80% 流量词及占比")],
        why="缺了算不出加权词重合度，四同法则只剩一半，分层只能靠猜",
        auto_fillable=True,
    ),
    Dimension(
        key="listing",
        name="Listing 基线",
        fields=[
            ("title", "标题"),
            ("image_url", "主图"),
            ("bullet_points", "五点描述"),
        ],
        why="缺了无法发现对手抄你 listing，也无法对比内容改动方向",
        auto_fillable=True,
    ),
]


# ============================================================
# 体检结果
# ============================================================


@dataclass
class DimensionStatus:
    key: str
    name: str
    filled: int
    total: int
    missing: list[str] = field(default_factory=list)
    why: str = ""
    auto_fillable: bool = False

    @property
    def complete(self) -> bool:
        return self.filled == self.total

    @property
    def ratio(self) -> float:
        return self.filled / self.total if self.total else 0.0


@dataclass
class BaselineAssessment:
    asin: str
    dimensions: list[DimensionStatus] = field(default_factory=list)

    # 派生指标
    days_of_supply: Optional[float] = None
    inventory_status: str = "unknown"

    # 能力边界：这些判断现在能不能做
    can_tier: bool = False
    can_judge_price_follow: bool = False
    can_judge_attack_window: bool = False
    can_detect_listing_copy: bool = False

    warnings: list[str] = field(default_factory=list)

    @property
    def readiness(self) -> float:
        """锚点就绪度 0~100。"""
        if not self.dimensions:
            return 0.0
        return round(sum(d.ratio for d in self.dimensions) / len(self.dimensions) * 100, 1)

    @property
    def grade(self) -> str:
        r = self.readiness
        if r >= 90:
            return "A"
        if r >= 70:
            return "B"
        if r >= 40:
            return "C"
        return "D"

    def to_dict(self) -> dict:
        return {
            "asin": self.asin,
            "readiness": self.readiness,
            "grade": self.grade,
            "days_of_supply": self.days_of_supply,
            "inventory_status": self.inventory_status,
            "capabilities": {
                "can_tier": self.can_tier,
                "can_judge_price_follow": self.can_judge_price_follow,
                "can_judge_attack_window": self.can_judge_attack_window,
                "can_detect_listing_copy": self.can_detect_listing_copy,
            },
            "dimensions": [
                {
                    "key": d.key,
                    "name": d.name,
                    "filled": d.filled,
                    "total": d.total,
                    "missing": d.missing,
                    "why": d.why,
                    "auto_fillable": d.auto_fillable,
                }
                for d in self.dimensions
            ],
            "warnings": self.warnings,
        }


# ============================================================
# 体检
# ============================================================


def assess(my_product) -> BaselineAssessment:
    """对自家产品做锚点体检。my_product 传 ORM 对象或同名属性的对象皆可。"""
    asin = getattr(my_product, "asin", "") or ""
    res = BaselineAssessment(asin=asin)

    for dim in DIMENSIONS:
        missing: list[str] = []
        filled = 0
        for attr, label in dim.fields:
            val = getattr(my_product, attr, None)
            if _has_value(val):
                filled += 1
            else:
                missing.append(label)
        res.dimensions.append(
            DimensionStatus(
                key=dim.key,
                name=dim.name,
                filled=filled,
                total=len(dim.fields),
                missing=missing,
                why=dim.why,
                auto_fillable=dim.auto_fillable,
            )
        )

    # ---- 派生指标 ----
    dos = _safe_dos(my_product)
    res.days_of_supply = dos
    if dos is None:
        res.inventory_status = "unknown"
    elif dos < 20:
        res.inventory_status = "alert"
    elif dos <= 60:
        res.inventory_status = "normal"
    else:
        res.inventory_status = "surplus"

    # ---- 能力边界 ----
    has_price = _has_value(getattr(my_product, "current_price", None))
    has_be_price = _has_value(getattr(my_product, "break_even_price", None))
    has_margin = _has_value(getattr(my_product, "target_margin_rate", None))
    has_kw = bool(getattr(my_product, "core_keywords", None))
    has_title = _has_value(getattr(my_product, "title", None))

    # 分层只要有价格 + 核心词就能跑（节点匹配是加分项）
    res.can_tier = has_price and has_kw

    # 跟价判断必须要保本价；毛利可以后补
    res.can_judge_price_follow = has_price and (has_be_price or has_margin)

    # 进攻窗口至少要库存或日均销量之一，最好两个都有
    res.can_judge_attack_window = dos is not None

    res.can_detect_listing_copy = has_title

    # ---- 警告 ----
    if not has_be_price and not has_margin:
        res.warnings.append(
            "缺保本价与目标毛利率：对手降价时只能告诉你「他降了」，无法告诉你「该不该跟」。"
            "建议先去后台算一次保本价填进来。"
        )
    if not has_kw:
        res.warnings.append(
            "缺核心出单词：分层会退化为只按价格带判断，容易把无关竞品拉进池子。"
        )
    if dos is None:
        res.warnings.append(
            "缺库存或日均销量：算不出可售天数，进攻窗口的判断会被跳过（不会出错，但会漏机会）。"
        )
    if not _has_value(getattr(my_product, "lead_time_days", None)):
        res.warnings.append(
            "缺补货周期：无法估算「现在补货多久能到」，DOS 只能看短期。"
        )

    return res


# ============================================================
# 自动补全
# ============================================================


def prefill_from_snapshot(my_product, snap: UnifiedSnapshot) -> list[str]:
    """用自家 ASIN 的公开快照补齐能自动填的字段。只填空位，不覆盖人工数据。

    返回被填写的字段名列表。
    """
    filled: list[str] = []

    mapping = [
        ("current_price", snap.price),
        ("sub_category_rank", snap.rank_sub or snap.rank_main),
        ("rating", snap.rating),
        ("review_count", snap.review_count),
        ("title", snap.title),
        ("image_url", snap.image_url),
    ]
    for attr, val in mapping:
        if not _has_value(getattr(my_product, attr, None)) and _has_value(val):
            setattr(my_product, attr, val)
            filled.append(attr)

    # 核心词：只有原本为空时才整体写入
    if not getattr(my_product, "core_keywords", None) and snap.keywords:
        core = snap.core_keyword_weights(0.8)
        if core:
            my_product.core_keywords = core
            filled.append("core_keywords")

    return filled


def estimate_break_even_price(*, price: float, margin_rate: float) -> Optional[float]:
    """从目标毛利率反推保本价。

    保本价 = 售价 × (1 - 毛利率)  —— 这是「一单不赚」的临界点，
    实际经营还应留出退货与仓储的缓冲，所以建议再上浮 3%~5%。
    """
    if not price or price <= 0 or margin_rate is None:
        return None
    if not (0 < margin_rate < 1):
        return None
    return round(price * (1 - margin_rate), 2)


# ============================================================
# 工具
# ============================================================


def _has_value(v) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, dict, tuple, set)):
        return len(v) > 0
    if isinstance(v, (int, float)):
        return v > 0
    return True


def _safe_dos(my_product) -> Optional[float]:
    units = getattr(my_product, "fba_available_units", None)
    daily = getattr(my_product, "daily_sales_avg", None)
    if not units or not daily or daily <= 0:
        return None
    return round(units / daily, 1)
