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

#: 来源的中文名。面板「自家基线」页要显示「这个数是查来的还是你填的」，
#: 显示成 `sellersprite_proxy` 等于没显示。
SOURCE_LABELS: dict[str, str] = {
    SOURCE_MANUAL: "手工录入",
    SOURCE_SELLERSPRITE: "卖家精灵反推",
    SOURCE_SP_API: "SP-API 同步",
}

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
# 每个字段该从哪来
#
# 这张表是本轮的核心：**自家数据只有两个合法来源 —— SP-API 与手工**。
# 卖家精灵反推能拿到价格/星级/评价/类目这些公开数据，但那是「别人家的商品页」，
# 不是「我家后台的账」；把两者混在一张表里，一个月后没人分得清哪个数是查来的。
#
# 所以每个字段都写清三件事：
#   1. SP-API 能不能给（给的话是哪一段接口）
#   2. 给不了的话，手工从哪张单据/哪个后台取
#   3. 公开数据能不能反推（只用于 Mock 的构造方法，不作为真实模式的数据来源）
#
# 表里漏一个字段，面板上就会空一格「该从哪来」——所以有一条测试逐字段扫。
# ============================================================

#: 字段级来源取值（与 `MyProduct.field_sources` 存的值一致）
ORIGIN_SP_API = "sp_api"
ORIGIN_MANUAL = "manual"
ORIGIN_SELLERSPRITE = "sellersprite"
ORIGIN_SIMULATED = "simulated"

ORIGIN_LABELS: dict[str, str] = {
    ORIGIN_SP_API: "SP-API 同步",
    ORIGIN_MANUAL: "手工录入",
    ORIGIN_SELLERSPRITE: "卖家精灵反推",
    ORIGIN_SIMULATED: "模拟补充",
}

#: 真实模式接受的字段来源。反推与模拟只能出现在 Mock。
#:
#: 这不是洁癖：反推出来的库存与保本价会直接进「该不该跟价」的判断，
#: 而它在面板上和真值长得一模一样——错的不是数值，是它的身份。
LIVE_FIELD_ORIGINS: tuple[str, ...] = (ORIGIN_SP_API, ORIGIN_MANUAL)

#: 真实模式接受的**整行**来源（`MyProduct.source`）。两者是两层：
#: 整行 source 说明这批数主要怎么来的，字段级 origin 说明每一格怎么来的。
LIVE_SOURCES: tuple[str, ...] = (SOURCE_MANUAL, SOURCE_SP_API)


@dataclass(frozen=True)
class FieldOrigin:
    """一个锚点字段的来路。

    `spapi` 为 None 表示 **SP-API 结构上给不了**（不是这轮失败了）：
    成本、保本价、目标毛利、补货周期是卖家自己的账，任何接口都拿不到；
    星级和评价数只在商品页上，SP-API 不提供。
    """

    field: str
    spapi: Optional[str]
    manual: str
    #: 公开数据（商品页/第三方工具）能不能反推出来。**只用于 Mock 的构造方法**：
    #: 真实模式下反推值不算自家数据，见 `live_block_reason`。
    sellersprite: bool

    @property
    def label(self) -> str:
        return FIELD_LABELS.get(self.field, self.field)

    @property
    def live_hint(self) -> str:
        """真实模式下这个字段该怎么填——面板上直接显示这句话。"""
        if self.spapi:
            return f"SP-API：{self.spapi}；或{self.manual}"
        return f"只能手工：{self.manual}"


FIELD_ORIGINS: dict[str, FieldOrigin] = {
    o.field: o
    for o in (
        FieldOrigin(
            "fba_available_units",
            "FBA Inventory · getInventorySummaries（fulfillableQuantity）",
            "库存台账（FBA 在途/待入仓要另外加）",
            sellersprite=False,
        ),
        FieldOrigin(
            "daily_sales_avg",
            "Sales · getOrderMetrics（近 30 日下单件数 ÷ 30）",
            "订单报表按日汇总",
            sellersprite=True,   # 卖家精灵按 BSR 幂律模型估的，是估算不是订单
        ),
        FieldOrigin(
            "lead_time_days",
            None,
            "采购合同交期 + 头程时效（工厂到 FBA 仓）",
            sellersprite=False,
        ),
        FieldOrigin(
            "current_price",
            "Pricing · getItemOffers（取本店那条的 ListingPrice；到手价用 BuyBox 价）",
            "后台售价（含券后价要说清是哪个）",
            sellersprite=True,
        ),
        FieldOrigin(
            "break_even_price",
            None,
            "成本表：采购价 + 头程 + FBA 配送费 + 佣金 + 退货损耗",
            sellersprite=False,  # 选品库的毛利率是它自己那套成本模型的估算
        ),
        FieldOrigin(
            "target_margin_rate",
            None,
            "经营目标（老板定的，不是算出来的）",
            sellersprite=False,
        ),
        FieldOrigin(
            "sub_category_rank",
            "Catalog Items · salesRanks（注意：返回的是某个排名组，未必是子类）",
            "商品页「Best Sellers Rank」里的子类那一行",
            sellersprite=True,
        ),
        FieldOrigin(
            "rating",
            None,                # SP-API 不提供
            "商品页星级（自家 ASIN 的公开页）",
            sellersprite=True,
        ),
        FieldOrigin(
            "review_count",
            None,
            "商品页评价数（自家 ASIN 的公开页）",
            sellersprite=True,
        ),
        FieldOrigin(
            "core_keywords",
            None,
            "广告「搜索词报告」按曝光/点击排序取 Top80%；或卖家精灵反查后人工核",
            sellersprite=True,
        ),
        FieldOrigin(
            "title",
            "Catalog Items · getCatalogItem（summaries[].itemName）",
            "后台 listing 文案",
            sellersprite=True,
        ),
        FieldOrigin(
            "image_url",
            "Catalog Items · getCatalogItem（images[].link）",
            "后台主图",
            sellersprite=True,
        ),
        FieldOrigin(
            "bullet_points",
            "Catalog Items · getCatalogItem（attributes.bullet_point）",
            "后台五点描述",
            sellersprite=True,
        ),
        FieldOrigin(
            "category_path",
            "Catalog Items · getCatalogItem（classifications，不保证是完整链）",
            "后台类目 + 商品页面包屑",
            sellersprite=True,
        ),
    )
}


def field_origin(name: str) -> Optional[FieldOrigin]:
    return FIELD_ORIGINS.get(name)


def live_field_block_reason(origin: Optional[str]) -> Optional[str]:
    """字段级：这个来源在真实模式下能不能用。不能用时给出原因。"""
    if origin in (None, ""):
        return None
    if origin in LIVE_FIELD_ORIGINS:
        return None
    label = ORIGIN_LABELS.get(origin, origin)
    return (
        f"这个值是{label}来的。真实模式只接受 SP-API 同步或手工录入——"
        "反推出来的数看着和真值一样，但它不是你家后台的账。"
    )


def live_block_reason(source: Optional[str]) -> Optional[str]:
    """整行锚点在真实模式下能不能用。能用返回 None，不能用返回原因。

    为什么要在**整行**这一层再拦一次，而字段级已经拦过：
    整行 `source` 决定了「这批数的身份」，字段级 `origin` 是逐格标注。
    两者都可能是脏的（历史数据只填了 source 没填 field_sources），
    任何一层放过都会让反推值进判断。
    """
    if source in (None, "") or source in LIVE_SOURCES:
        return None
    if source == SOURCE_SELLERSPRITE:
        return (
            "这组数是用卖家精灵反推的：价格、排名、星级、评价数来自别人家的商品页，"
            "库存与保本价是估的。真实模式不接受这份锚点——"
            "它看起来和真值没有区别，但它不是你家后台的账。"
        )
    label = SOURCE_LABELS.get(source, source)
    return f"锚点来源是「{label}」，真实模式只接受 SP-API 同步或手工录入。"


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
# 字段级明细（给面板「自家基线」页用）
#
# 体检回答「五维各差几个」，本段回答「那一格现在到底填了什么」。
# 两者缺一不可：只给就绪度，人不知道自己该改哪个数；只给值，
# 人不知道这些值凑起来够不够跑一次判断。
# ============================================================

#: 字段的**值类型**，不是显示格式。
#:
#: 为什么类型由 L2 说、格式由 L5 定：`current_price` 是金额、`target_margin_rate`
#: 是 0~1 的比例、`sub_category_rank` 是名次——这三件事是领域事实，放哪层都一样；
#: 而「金额显示成 $22.99 还是 22.99 美元」是排版决定。
#: 让 API 只给 kind、面板自己格式化，能避免「后端拼了 `$` 前缀、前端又 money() 一次
#: 变成 $$22.99」这种只有截图才看得出来的错。
FIELD_KINDS: dict[str, str] = {
    "fba_available_units": "int",
    "daily_sales_avg": "float1",
    "lead_time_days": "int",
    "current_price": "money",
    "break_even_price": "money",
    "target_margin_rate": "percent",
    "sub_category_rank": "rank",
    "rating": "rating",
    "review_count": "int",
    "core_keywords": "keywords",
    "title": "text",
    "image_url": "image",
    "bullet_points": "list",
    "category_path": "path",
}


def _json_safe(value):
    """把 ORM 里读出来的值转成能进 JSON 的形状。

    只处理两种真实存在的意外：JSON 列被手工改库写成了字符串、
    以及未来可能出现的日期类型。其余原样返回——**不做任何格式化**。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


def _field_row(
    my_product,
    name: str,
    label: str,
    assumed: set[str],
    *,
    field_sources: Optional[dict] = None,
) -> dict:
    raw = getattr(my_product, name, None)
    origin = (field_sources or {}).get(name)
    meta = FIELD_ORIGINS.get(name)
    return {
        "name": name,
        "label": label,
        "kind": FIELD_KINDS.get(name, "text"),
        "value": _json_safe(raw),
        "filled": _has_value(raw),
        "assumed": name in assumed,
        # ---- 这一格的数据是从哪来的 ----
        # 整行 `source` 只说明「这批数主要怎么来的」，说明不了这一格。
        # 没有 origin 时**不猜成 manual**：猜错了就是给一个反推值盖了个真章。
        "origin": origin,
        "origin_label": ORIGIN_LABELS.get(origin) if origin else None,
        "origin_known": origin is not None,
        # ---- 这一格在真实模式下该怎么填 ----
        "spapi": meta.spapi if meta else None,
        "manual_hint": meta.manual if meta else None,
        "live_hint": meta.live_hint if meta else "来源未定义（FIELD_ORIGINS 漏了这个字段）",
        "sellersprite_can": meta.sellersprite if meta else False,
    }


def describe(my_product, *, field_sources: Optional[dict] = None) -> dict:
    """把锚点摊平成「每个字段一行」：值、类型、是否已填、是不是假设值、从哪来。

    ## 整行 `source` 与字段级 `assumed_fields` 谁说话

    表里只有一个 `source` 列，它是**这一批数据的主要来源**，不是每个字段各自的出身。
    字段级的真话由 `assumed_fields` 承担，而且它优先级更高：

        source = "sellersprite_proxy"     ← 主要来自卖家精灵
        assumed_fields = ["break_even_price", ...]  ← 但保本价不是

    卖家精灵给不出保本价、FBA 库存、补货周期——那些只能是拍的。
    所以一个字段先看它在不在 `assumed_fields` 里，再退回看 `source`。
    反过来（先信 source）会让保本价显示成「卖家精灵反推」，那是**假溯源**，
    比不标来源更糟。

    ## `field_sources` 是第三层，也是最细的一层

    `assumed_fields` 只回答「是不是拍的」，回答不了「这个数是 SP-API 同步来的
    还是我手填的」。真实模式下这两者的可信度完全不同（API 来的可以重跑，
    手填的会过时），所以逐字段要多一个 `origin`。
    """
    assumed = set(read_assumed_fields(getattr(my_product, "assumed_fields", None)))
    source = getattr(my_product, "source", None)
    kwargs = {"field_sources": field_sources}

    groups: list[dict] = []
    for dim in DIMENSIONS:
        rows = [
            _field_row(my_product, name, label, assumed, **kwargs)
            for name, label in dim.fields
        ]
        groups.append(
            {
                "key": dim.key,
                "name": dim.name,
                "why": dim.why,
                "informs": dim.informs,
                "auto_fillable": dim.auto_fillable,
                "filled": sum(1 for r in rows if r["filled"]),
                "total": len(rows),
                "fields": rows,
            }
        )

    # `category_path` 不属于五大维度（它不计入就绪度，见 DIMENSIONS 的注释），
    # 但它是分层节点维度的**唯一**输入，属于「不填就有一整个维度判不了」的那种字段。
    # 放在 extras 而不是硬塞进某个维度：塞进去会改变就绪度分母，
    # 让历史报告里的 73.3 变成一个和当时不同的分数。
    extras = [
        _field_row(my_product, "category_path", "自家类目路径", assumed, **kwargs)
    ]

    return {
        "asin": getattr(my_product, "asin", "") or "",
        "source": source,
        "source_label": SOURCE_LABELS.get(source) or ("未标注" if not source else source),
        "assumed_fields": sorted(assumed),
        "groups": groups,
        "extras": extras,
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
