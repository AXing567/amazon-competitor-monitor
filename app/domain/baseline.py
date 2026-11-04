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

## 「缺」和「假」是两件事，体检必须分开说

第 1 件事只回答了「有没有值」。但 pilot 期（SP-API 未接入，用卖家精灵反推）
锚点里会同时存在三种值：

- **查到的真值**：售价 / 星级 / 评论数 / 子类目 BSR —— 公开数据，可信
- **别人家的估算**：日均销量 —— 卖家精灵按 BSR 幂律模型估的，不是订单级真实值
- **纯假设**：保本价 / FBA 可售库存 / 补货周期 —— 卖家自己的账，外部数据源给不出

「有值」不等于「这个值能拿去决策」。`assumed_fields` 非空时，
体检会额外告警并点名受影响的结论（见 `assumed_warning`）：
**没有这句告警，一个拍出来的保本价推导出的提价建议，会看起来和真的一样。**
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from app.adapters.unified import UnifiedSnapshot

# ============================================================
# 锚点来源（与 `MyProduct.source` 一列对应）
# ============================================================

#: `manual` = 人手填；`sellersprite_proxy` = 卖家精灵反推（pilot 期临时顶替 SP-API）；
#: `sp_api` = SP-API 同步。写在这里而不是散在各层：
#: 面板下拉、能力层枚举校验、体检措辞都从这一份取值，改一处到处跟着改。
SOURCE_MANUAL = "manual"
SOURCE_SELLERSPRITE = "sellersprite_proxy"
SOURCE_SP_API = "sp_api"

ANCHOR_SOURCES: tuple[str, ...] = (SOURCE_MANUAL, SOURCE_SELLERSPRITE, SOURCE_SP_API)

#: `assumed_fields` 形状不对时给人照做的一句建议。
ASSUMED_FIELDS_HINT = (
    '传字段名列表，例如 ["break_even_price", "fba_available_units", "lead_time_days"]。'
    "只写锚点字段的英文名，不要写中文名，也不要传单个字符串。"
)

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
    #: 这一维的字段**支撑哪些结论**。`why` 回答「缺了会怎样」，
    #: `informs` 回答「这些数被谁用」——假设值告警需要后者。
    #: 放在这里而不是另写一张「字段→结论」表：两者其实说的是同一件事，
    #: 分两处维护必然有一天只剩一处被更新。
    informs: str = ""


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
        informs="可售天数 DOS、库存状态判读、断货与积压预警、进攻窗口判断、补货建议",
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
        informs="跟价判断、提价与降价窗口、毛利测算",
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
        informs="名次被打掉多少的判断、对手追赶速度",
    ),
    Dimension(
        key="keywords",
        name="核心出单词",
        fields=[("core_keywords", "Top80% 流量词及占比")],
        why="缺了算不出加权词重合度，四同法则只剩一半，分层只能靠猜",
        auto_fillable=True,
        informs="四同法则的词重合度、分层结果",
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
        informs="抄 listing 检测、内容改动方向对比",
    ),
]

#: 字段名 → 中文名。给「假设值告警」用：只说"部分字段是模拟的"等于什么都没说。
FIELD_LABELS: dict[str, str] = {
    fname: label for dim in DIMENSIONS for fname, label in dim.fields
}


# ============================================================
# 假设值：形状校验与措辞
#
# 校验只有这一份实现，写入层（`capabilities.writes.set_baseline`）和
# 兜底层（`services.pool.update_baseline`，它还有两个非能力层调用方）都调它。
# 为什么不在两处各写一遍：两处一旦分叉，就会出现「面板能存、API 存不进去」
# 或者更糟的「一边校验、一边静默放过」——而后者不会报错。
# ============================================================


def coerce_assumed_fields(raw) -> list[str]:
    """把 `assumed_fields` 归一成「去重后的非空字符串列表」。

    接受 list/tuple，也接受 JSON 字符串（面板表单和 HTTP body 常常就是字符串）。
    形状不对抛 `ValueError`，消息里说清是什么形状不对 —— 调用方负责转成
    `{"ok": False, "error_kind": "param", "hint": ...}`。

    为什么非校验不可：`JSON` 列在 SQLite 里就是 TEXT，存什么进去都能读回来
    （存一个裸字符串 `"break_even_price"` 也能存）。而读取侧遍历它时，
    逐字符遍历的结果是 `["b", "r", "e", ...]`——告警会变成一串字母，
    还不会有任何一处报错。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            raise ValueError(
                f"assumed_fields 需要字符串列表，收到无法解析的字符串 {raw!r}"
            ) from None
    if isinstance(raw, tuple):
        raw = list(raw)
    if not isinstance(raw, list):
        raise ValueError(
            f"assumed_fields 需要字符串列表，收到 {type(raw).__name__}：{raw!r}"
        )

    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"assumed_fields 的每一项都必须是非空字符串（字段名），收到 {item!r}"
            )
        name = item.strip()
        if name not in out:      # 去重但保序：告警文案里重复一遍"保本价"很蠢
            out.append(name)
    return out


def read_assumed_fields(raw) -> list[str]:
    """读取侧的宽容版：形状不对就当空，绝不因为一个坏值让整次体检失败。

    写入层已经校验过，这里只为「历史脏数据 / 手工改库」兜底。
    宽容不等于掩盖：坏形状在写入时会被拒，能走到这里的只有绕过写入层的值。
    """
    try:
        return coerce_assumed_fields(raw)
    except ValueError:
        return []


def assumed_warning(fields: list[str]) -> str:
    """生成「这些字段是假设值」的告警文案。

    三件事必须说清，否则这条告警没用：
    1. **具体是哪几个字段**（中文名，不说"部分字段"）；
    2. **哪些结论会被带偏**（从 `Dimension.informs` 取，不另写一份）；
    3. **该怎么办**（结论不可信 → 只能验链路 / 去补真数据）。
    """
    labels = [FIELD_LABELS.get(f, f) for f in fields]
    affected: list[str] = []
    assumed = set(fields)
    for dim in DIMENSIONS:
        if assumed & {name for name, _ in dim.fields} and dim.informs:
            affected.append(dim.informs)

    head = f"以下锚点字段是假设值而非实测：{'、'.join(labels)}。"
    if affected:
        body = f"由它们推导出的结论（{'；'.join(affected)}）不可信"
    else:
        body = "由它们推导出的结论不可信"
    return (
        head
        + body
        + "，只能用于验证链路是否跑通。请先把真实数据补齐，"
        "再拿结论做决定——否则它看起来和真的没有区别。"
    )


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

    # 哪些字段是假设值（原样带出，面板据此给它们打「假设值」标记）
    assumed_fields: list[str] = field(default_factory=list)

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
            "assumed_fields": list(self.assumed_fields),
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
    has_path = _has_value(getattr(my_product, "category_path", None))
    has_title = _has_value(getattr(my_product, "title", None))

    # 分层只要有价格 + **任一**判定维度就够跑（`judge_tier` 的准入门槛是
    # 「能判的维度 ≥ 2」，价格带算一维，词重合与节点各算一维）。
    # 这里以前只认 `core_keywords`，节点维度接通之后就变成了一句假话：
    # 面板说「分层跑不了」，同一个库里 17 个竞品却都分好了层。
    res.can_tier = has_price and (has_kw or has_path)

    # 跟价判断必须要保本价；毛利可以后补
    res.can_judge_price_follow = has_price and (has_be_price or has_margin)

    # 进攻窗口至少要库存或日均销量之一，最好两个都有
    res.can_judge_attack_window = dos is not None

    res.can_detect_listing_copy = has_title

    # ---- 假设值（先说这一条）----
    # 放在「缺字段」之前：缺字段只是判断做不了，而假设值是**判断会给出错答案**。
    # 面板/飞书按顺序显示告警，这条排第一，用户就不会读到一半以为一切正常。
    res.assumed_fields = read_assumed_fields(getattr(my_product, "assumed_fields", None))
    if res.assumed_fields:
        res.warnings.append(assumed_warning(res.assumed_fields))

    # ---- 警告 ----
    if not has_be_price and not has_margin:
        res.warnings.append(
            "缺保本价与目标毛利率：对手降价时只能告诉你「他降了」，无法告诉你「该不该跟」。"
            "建议先去后台算一次保本价填进来。"
        )
    if not has_kw:
        res.warnings.append(
            "缺核心出单词：分层只能靠价格带 + 类目节点两项，同品但挂在别的类目下的竞品"
            "会被节点判掉（跨类目的同类品是常见情况）。"
            if has_path
            else "缺核心出单词：分层会退化为只按价格带判断，容易把无关竞品拉进池子。"
        )
    if not _has_value(getattr(my_product, "category_path", None)):
        # 这一条以前不存在，因为 `category_path` 这个字段本身不存在（硬编码 None），
        # 于是「节点维度判不了」这件事从来没被说出来过——池子里一堆「待判定」
        # 而配置页看起来一切正常。缺什么就说缺什么。
        res.warnings.append(
            "缺自家类目路径：分层判断的「节点」维度无法判定，同一类目下的竞品"
            "只能靠价格和流量词去猜，交叉品类的会被误判。填一次即可（如 "
            '["Toys & Games", "Toy Vehicles", "Play Trucks"]）。'
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
