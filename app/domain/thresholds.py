"""L2 阈值规格：全系统阈值的**唯一真相来源**。

## 为什么这个文件放在 app/domain/ 而不是 app/services/

它同时被 `diff.py` 和 `tiering.py` 使用。放进其中任何一个都会造成
`diff ↔ tiering` 互相 import。所以它必须是第三个中立模块。

它只含 dataclass + 一张元数据表（键、中文名、类型、默认值、范围），
**零 I/O、零时钟、零环境变量**，所以它仍然属于 L2：

    L0  threshold_configs 表      —— 用它取默认值、校验范围
    L4  面板渲染阈值表单           —— 用它生成输入框（面板不认识任何字段名）
    L2  DiffEngine / judge_tier   —— 用它构造 dataclass

一条 schema 定义，三边共用。这是「配置必须从外面传」这条红线能落地的关键：
只要新增参数 = 在 `THRESHOLD_SPEC` 加一行，面板自动出现该输入框，前端零改动。

## 为什么不导出 DEFAULT_DIFF / DEFAULT_TIERING 之类的模块级单例

dataclass 是可变的。一个共享实例被某处 `merge` 就地改掉，全进程的阈值
就一起漂了，而这种 bug 极难复现。要默认值请调 `defaults(group)` / `for_group(group)()`，
每次拿一份新的。

## 为什么 `phase_scale` 从 dict 拆成三个数字字段

`RULES["phase_scale"]` 原本是 `{"warmup": 1.0, "blast": 0.8, "cooldown": 1.3}`。
配置化的目标是「每个可调项在面板上是一个输入框」，而 dict 类型在表单里没法渲染。
所以 SPEC 里拆成 `phase_scale_warmup / phase_scale_blast / phase_scale_cooldown`
三个 float 键，dataclass 也相应拆成三个字段，再通过 `phase_scale` 属性
拼回原来的 dict —— 这样 `DiffEngine` 里的 `self.th.phase_scale[self.phase]`
一行都不用改，而「dataclass 字段 ↔ SPEC 键」保持严格一一对应（可机械断言，不靠肉眼）。
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, ClassVar, Mapping, TypeVar

# ============================================================
# 组名（与 threshold_configs.group_name 一致）
# ============================================================

GROUP_DIFF = "diff"
GROUP_TIERING = "tiering"
GROUP_NOTIFY = "notify"

GROUP_TITLES: dict[str, str] = {
    GROUP_DIFF: "变化判定阈值",
    GROUP_TIERING: "分层与入池阈值",
    GROUP_NOTIFY: "打扰节奏阈值",
}

GROUPS: tuple[str, ...] = (GROUP_DIFF, GROUP_TIERING, GROUP_NOTIFY)


# ============================================================
# 公共行为
# ============================================================


def _coerce(default: Any, value: Any) -> Any:
    """按默认值的类型转换外部传入值。

    为什么以「默认值的类型」为准而不是自带类型声明：
    SPEC 里再写一遍类型，就会出现「dataclass 改成 int、SPEC 还写着 float」的漂移。
    默认值本身就是类型的证据。
    """
    if value is None:
        return default
    if isinstance(default, bool):          # 先于 int 判断：bool 是 int 的子类
        return bool(value)
    if isinstance(default, int):
        return int(float(value))           # 允许 "20" / 20.0 这类输入
    if isinstance(default, float):
        return float(value)
    return value


_T = TypeVar("_T", bound="_ThresholdBase")


@dataclass
class _ThresholdBase:
    """三个阈值 dataclass 的公共行为：from_dict / to_dict / merge / unknown_keys。"""

    _group: ClassVar[str] = ""

    # ---- 构造 ----

    @classmethod
    def _expand_nested(cls, data: dict[str, Any]) -> dict[str, Any]:
        """把历史遗留的嵌套键展开成扁平字段。子类按需覆写。"""
        return data

    @classmethod
    def from_dict(cls: type[_T], data: Mapping[str, Any] | None = None) -> _T:
        """稀疏覆盖 → 完整对象。**未提供的键一律落回默认值。**

        这是「存稀疏不存全量」能成立的那一半：读取时用默认值兜底，
        于是「新增一个参数」对所有历史配置天然兼容。

        未知键被忽略（而不是抛异常）：读路径上的未知键通常来自「旧代码读新配置」
        或者已被删除的历史参数，为此让整个采集流程失败不值得。
        要查未知键请显式调 `unknown_keys()`，写路径（settings_store.save）
        正是这么做的——那里才该拒绝。
        """
        raw = cls._expand_nested(dict(data or {}))
        default = cls()
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            kwargs[f.name] = _coerce(getattr(default, f.name), raw.get(f.name))
        return cls(**kwargs)

    # ---- 输出 ----

    def to_dict(self) -> dict[str, Any]:
        """完整键值对（含默认值）。落库前请自行挑出「被覆盖的键」。"""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    # ---- 合并 ----

    def merge(self, overrides: Mapping[str, Any] | "_ThresholdBase" | None) -> "_ThresholdBase":
        """以 self 为底、overrides 为覆盖，返回**新对象**（不修改 self）。

        为什么返回新对象而不是就地改：
        阈值对象会被同一个进程里的多个调用方共享（采集、试算、面板）。
        就地改会让「试算」顺手改掉生产阈值——这是本轮最不能出的错。
        """
        if overrides is None:
            return self.from_dict(self.to_dict())
        if isinstance(overrides, _ThresholdBase):
            overrides = overrides.to_dict()
        data = self.to_dict()
        data.update({k: v for k, v in dict(overrides).items() if v is not None})
        return self.from_dict(data)

    # ---- 自检 ----

    @classmethod
    def unknown_keys(cls, data: Mapping[str, Any] | None) -> set[str]:
        """返回落在本组之外的键（含拼写错误）。写路径用它拒绝脏数据。"""
        known = {f.name for f in fields(cls)}
        return {k for k in (data or {}) if k not in known}


# ============================================================
# diff：变化判定阈值
# ============================================================


@dataclass
class DiffThresholds(_ThresholdBase):
    """DiffEngine 的全部可调阈值。

    默认值 = 重构前 `app/domain/diff.py::RULES` 的取值，逐键相等。
    `tests/test_thresholds.py` 用断言锁死这个等价关系：
    31 个领域测试的期望值依赖它，一旦漂移，回归基线就废了。
    """

    _group: ClassVar[str] = GROUP_DIFF

    # ---- 价格 ----
    price_p0_pct: float = 0.03
    price_p1_pct: float = 0.01
    coupon_p0_pct: float = 0.05
    price_floor_guard: float = 0.005

    # ---- 供给 ----
    # 当前只作声明（断货一律 P0，不等确认），留作采集层确认窗口口径。
    oos_confirm_minutes: int = 30

    # ---- 排名 ----
    rank_p0_pct: float = 0.30
    rank_p1_pct: float = 0.15
    rank_min_abs_move: int = 20

    # ---- 口碑 ----
    review_p0_abs: int = 50
    review_p0_pct: float = 0.05
    rating_drop_p1: float = 0.20
    rating_floor: float = 4.0

    # ---- 季节系数（RULES["phase_scale"] 的 dict 拆成三个可编辑数字）----
    phase_scale_warmup: float = 1.0
    phase_scale_blast: float = 0.8
    phase_scale_cooldown: float = 1.3

    @property
    def phase_scale(self) -> dict[str, float]:
        """拼回 `{"warmup": .., "blast": .., "cooldown": ..}`。

        保留这个属性（而不是让调用方自己去拼）是为了让 DiffEngine 里
        `self.th.phase_scale[self.phase]` 与原 `RULES["phase_scale"][...]` 完全同形。
        """
        return {
            "warmup": self.phase_scale_warmup,
            "blast": self.phase_scale_blast,
            "cooldown": self.phase_scale_cooldown,
        }

    @classmethod
    def _expand_nested(cls, data: dict[str, Any]) -> dict[str, Any]:
        """兼容 `RULES` 的嵌套写法：`{"phase_scale": {...}}` → 三个扁平键。"""
        nested = data.get("phase_scale")
        if isinstance(nested, Mapping):
            for phase in ("warmup", "blast", "cooldown"):
                if nested.get(phase) is not None:
                    data.setdefault(f"phase_scale_{phase}", nested[phase])
        data.pop("phase_scale", None)
        return data


# ============================================================
# tiering：分层与入池阈值
# ============================================================


@dataclass
class TieringThresholds(_ThresholdBase):
    """四同法则的可调阈值。默认值 = 重构前 `app/domain/tiering.py` 的模块常量。"""

    _group: ClassVar[str] = GROUP_TIERING

    price_ratio_min: float = 0.7
    price_ratio_max: float = 1.4
    keyword_overlap_gate: float = 0.40
    overlap_s: float = 0.60
    overlap_a: float = 0.40
    overlap_b: float = 0.20
    # 当前未被消费（加权词重合度用的是 UnifiedSnapshot.core_keyword_weights(0.8)）。
    # 仍要进 SPEC：否则下一个写「核心词池覆盖度」的人会重新硬编码一个 0.8，
    # 而配置页上永远看不到它。死键就是下一批散落魔法数字的胚胎。
    core_coverage: float = 0.80


# ============================================================
# notify：打扰节奏阈值
# ============================================================


@dataclass
class NotifyThresholds(_ThresholdBase):
    """冷却去重窗口。默认值 = 重构前 `collector.COOLDOWN_HOURS`。"""

    _group: ClassVar[str] = GROUP_NOTIFY

    cooldown_p0_hours: int = 4
    cooldown_p1_hours: int = 12
    cooldown_p2_hours: int = 48

    def cooldown_hours(self, level: str) -> int:
        """按信号等级取冷却窗口。未知等级按 P2 处理（宁可少打扰）。"""
        return {
            "P0": self.cooldown_p0_hours,
            "P1": self.cooldown_p1_hours,
            "P2": self.cooldown_p2_hours,
        }.get(level, self.cooldown_p2_hours)


# ============================================================
# SPEC 元数据
# ============================================================


@dataclass(frozen=True)
class ThresholdSpecItem:
    """一个可调参数的规格。面板靠它渲染输入框，服务端靠它校验。"""

    key: str
    label: str                     # 中文标签，直接显示在表单上
    group: str
    type: str                      # float / int
    default: float | int
    unit: str = ""
    minimum: float | None = None
    maximum: float | None = None
    help: str = ""                 # 「改了会影响什么」

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "group": self.group,
            "type": self.type,
            "default": self.default,
            "unit": self.unit,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "help": self.help,
        }


def _build_items(
    group: str,
    cls: type[_ThresholdBase],
    rows: tuple[dict[str, Any], ...],
) -> tuple[ThresholdSpecItem, ...]:
    """按 (字段名, 中文名, 范围, 说明) 生成 SPEC 条目。

    **默认值一律从 dataclass 实例读**，不在 SPEC 里再写一遍数字。
    写两遍 = 两份真相，早晚不一致，而这类不一致的后果是
    「面板显示 3%，实际判定 5%」这种最难查的 bug。
    """
    default = cls()
    items = []
    for row in rows:
        key = row["key"]
        items.append(
            ThresholdSpecItem(
                key=key,
                label=row["label"],
                group=group,
                type=row.get("type", "float"),
                default=getattr(default, key),
                unit=row.get("unit", ""),
                minimum=row.get("minimum"),
                maximum=row.get("maximum"),
                help=row.get("help", ""),
            )
        )
    return tuple(items)


_DIFF_ROWS: tuple[dict[str, Any], ...] = (
    dict(
        key="price_p0_pct",
        label="净价变动 ≥ 此值判 P0",
        unit="比例",
        minimum=0.001,
        maximum=0.5,
        help="越核心的竞品越该收紧；旺季建议 0.02",
    ),
    dict(
        key="price_p1_pct",
        label="净价变动 ≥ 此值判 P1（进日报）",
        unit="比例",
        minimum=0.0005,
        maximum=0.5,
        help="必须小于 P0 门槛，否则 P1 永远不触发",
    ),
    dict(
        key="coupon_p0_pct",
        label="优惠券加深 ≥ 此值判 P0",
        unit="比例",
        minimum=0.001,
        maximum=0.5,
        help="标价不动只加券，是同行最容易漏看的降价",
    ),
    dict(
        key="price_floor_guard",
        label="价格变动低于此值视为噪声",
        unit="比例",
        minimum=0.0,
        maximum=0.1,
        help="太小会把首图刷新造成的价格抖动当信号",
    ),
    dict(
        key="oos_confirm_minutes",
        label="断货二次确认窗口",
        unit="分钟",
        type="int",
        minimum=0,
        maximum=1440,
        help="当前未直接消费（断货一律 P0），留作采集层确认窗口口径",
    ),
    dict(
        key="rank_p0_pct",
        label="BSR 变差 ≥ 此值判 P0",
        unit="比例",
        minimum=0.01,
        maximum=2.0,
        help="名次类指标波动大，门槛给得比价格松",
    ),
    dict(
        key="rank_p1_pct",
        label="BSR 变动 ≥ 此值判 P2",
        unit="比例",
        minimum=0.005,
        maximum=2.0,
        help="介于 P0 门槛与 P1 门槛之间的一档，只归档",
    ),
    dict(
        key="rank_min_abs_move",
        label="名次绝对移动下限",
        unit="名",
        type="int",
        minimum=0,
        maximum=10000,
        help="防止 2 名变 3 名被判成 50% 变动",
    ),
    dict(
        key="review_p0_abs",
        label="单日新增评论 ≥ 此值判 P0",
        unit="条",
        type="int",
        minimum=1,
        maximum=100000,
        help="同时满足「比例门槛」或「绝对值门槛」即触发",
    ),
    dict(
        key="review_p0_pct",
        label="单日评论增幅 ≥ 此值判 P0",
        unit="比例",
        minimum=0.001,
        maximum=1.0,
        help="小基数产品靠这条触发",
    ),
    dict(
        key="rating_drop_p1",
        label="星级下跌 ≥ 此值判 P1",
        unit="星",
        minimum=0.01,
        maximum=2.0,
        help="星级是转化率最敏感的指标",
    ),
    dict(
        key="rating_floor",
        label="星级跌破此值单独报 P0",
        unit="星",
        minimum=0.0,
        maximum=5.0,
        help="4.0 是转化的心理分水岭，跌破一次就值得知道",
    ),
    dict(
        key="phase_scale_warmup",
        label="季节系数·淡季（warmup）",
        unit="倍",
        minimum=0.1,
        maximum=3.0,
        help="乘在所有比例类阈值上，1.0 表示不改",
    ),
    dict(
        key="phase_scale_blast",
        label="季节系数·旺季（blast）",
        unit="倍",
        minimum=0.1,
        maximum=3.0,
        help="<1 表示收紧（旺季错过代价高）",
    ),
    dict(
        key="phase_scale_cooldown",
        label="季节系数·退潮（cooldown）",
        unit="倍",
        minimum=0.1,
        maximum=3.0,
        help=">1 表示放宽（减少无意义打扰）",
    ),
)

_TIERING_ROWS: tuple[dict[str, Any], ...] = (
    dict(
        key="price_ratio_min",
        label="价格带下限（竞品价 / 自家价）",
        unit="倍",
        minimum=0.1,
        maximum=2.0,
        help="低于此值认为不是一个价位段的人",
    ),
    dict(
        key="price_ratio_max",
        label="价格带上限（竞品价 / 自家价）",
        unit="倍",
        minimum=0.5,
        maximum=5.0,
        help="高于此值认为他打的是更高价位人群",
    ),
    dict(
        key="keyword_overlap_gate",
        label="入池门槛·加权词重合度",
        unit="比例",
        minimum=0.0,
        maximum=1.0,
        help="低于此值直接判无关，不进池",
    ),
    dict(
        key="overlap_s",
        label="S 级词重合门槛",
        unit="比例",
        minimum=0.0,
        maximum=1.0,
        help="还要求价格带重叠、节点一致、体量咬合",
    ),
    dict(
        key="overlap_a",
        label="A 级词重合门槛",
        unit="比例",
        minimum=0.0,
        maximum=1.0,
        help="中重度重合，重点盯",
    ),
    dict(
        key="overlap_b",
        label="B 级词重合门槛",
        unit="比例",
        minimum=0.0,
        maximum=1.0,
        help="轻度重合，相邻战场",
    ),
    dict(
        key="core_coverage",
        label="核心词池覆盖流量比例",
        unit="比例",
        minimum=0.1,
        maximum=1.0,
        help="当前未直接消费（取值写死在 snapshot.core_keyword_weights 默认参数），留作词池口径",
    ),
)

_NOTIFY_ROWS: tuple[dict[str, Any], ...] = (
    dict(
        key="cooldown_p0_hours",
        label="P0 冷却窗口",
        unit="小时",
        type="int",
        minimum=0,
        maximum=720,
        help="同一竞品同一信号类型在窗口内只报一次",
    ),
    dict(
        key="cooldown_p1_hours",
        label="P1 冷却窗口",
        unit="小时",
        type="int",
        minimum=0,
        maximum=720,
        help="P1 进日报，不需要那么频繁",
    ),
    dict(
        key="cooldown_p2_hours",
        label="P2 冷却窗口",
        unit="小时",
        type="int",
        minimum=0,
        maximum=720,
        help="P2 只归档，窗口给长一点减少写库",
    ),
)


#: 唯一真相来源：group → 该组全部可调项
THRESHOLD_SPEC: dict[str, tuple[ThresholdSpecItem, ...]] = {
    GROUP_DIFF: _build_items(GROUP_DIFF, DiffThresholds, _DIFF_ROWS),
    GROUP_TIERING: _build_items(GROUP_TIERING, TieringThresholds, _TIERING_ROWS),
    GROUP_NOTIFY: _build_items(GROUP_NOTIFY, NotifyThresholds, _NOTIFY_ROWS),
}

#: 扁平索引：key → 规格（键名在三组之间不重复，重复会在下面的自检里炸掉）
SPEC_BY_KEY: dict[str, ThresholdSpecItem] = {
    item.key: item for items in THRESHOLD_SPEC.values() for item in items
}

#: group → dataclass 类型。settings_store / simulator 都在用这张表构造对象。
GROUP_CLASSES: dict[str, type[_ThresholdBase]] = {
    GROUP_DIFF: DiffThresholds,
    GROUP_TIERING: TieringThresholds,
    GROUP_NOTIFY: NotifyThresholds,
}

# 键名跨组重复会让 SPEC_BY_KEY 静默丢一条，所以这里是启动期硬断言，不是注释。
assert len(SPEC_BY_KEY) == sum(len(v) for v in THRESHOLD_SPEC.values()), "阈值键名跨组重复"


# ============================================================
# 便捷入口
# ============================================================


def spec_items(group: str | None = None) -> tuple[ThresholdSpecItem, ...]:
    """取某组（或全部）SPEC 条目。"""
    if group is None:
        return tuple(item for items in THRESHOLD_SPEC.values() for item in items)
    return THRESHOLD_SPEC.get(group, ())


def spec_groups() -> list[dict[str, Any]]:
    """面板用的分组结构：`GET /api/config/thresholds/schema` 直接返回它。"""
    return [
        {
            "group": group,
            "title": GROUP_TITLES.get(group, group),
            "items": [item.to_dict() for item in THRESHOLD_SPEC[group]],
        }
        for group in GROUPS
    ]


def defaults(group: str) -> dict[str, Any]:
    """某组的默认值字典（每次都是新 dict，可安全修改）。"""
    return GROUP_CLASSES[group]().to_dict()


def for_group(group: str, overrides: Mapping[str, Any] | None = None) -> _ThresholdBase:
    """构造某组阈值对象：默认值 + 稀疏覆盖。"""
    return GROUP_CLASSES[group].from_dict(overrides or {})


def values_to_groups(flat: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """把「跨组扁平键值」按组拆开。

    面板可能一次提交三组的键（表单是整页的），而落库是按组存的，
    所以需要一个「按键名反查所属组」的入口，别让调用方自己抄一份分组规则。
    """
    out: dict[str, dict[str, Any]] = {group: {} for group in GROUPS}
    for key, value in flat.items():
        item = SPEC_BY_KEY.get(key)
        if item is not None:
            out[item.group][key] = value
    return out


def validate(group: str, values: Mapping[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """校验并规范化一组稀疏覆盖值。返回 `(干净值, 错误列表)`。

    只做 L2 能做的事：键是否属于本组、类型能否转换、是否落在范围内。
    **不查库、不读时钟**——「这个键有没有被别的层覆盖」是 L3 的事。
    """
    cleaned: dict[str, Any] = {}
    errors: list[str] = []
    if not values:
        return cleaned, errors

    spec = THRESHOLD_SPEC.get(group)
    if spec is None:
        return cleaned, [f"未知阈值分组：{group}（可选：{'/'.join(GROUPS)}）"]
    by_key = {item.key: item for item in spec}

    default_instance = GROUP_CLASSES[group]()
    for key, raw in values.items():
        item = by_key.get(key)
        if item is None:
            errors.append(f"{key} 不属于分组 {group}")
            continue
        if raw is None:
            continue
        try:
            coerced = _coerce(getattr(default_instance, key), raw)
        except (TypeError, ValueError):
            errors.append(f"{key} 不是合法的{('整数' if item.type == 'int' else '数字')}：{raw!r}")
            continue
        if item.minimum is not None and coerced < item.minimum:
            errors.append(f"{key} 超出允许范围 {_fmt(item.minimum)}~{_fmt(item.maximum)}")
            continue
        if item.maximum is not None and coerced > item.maximum:
            errors.append(f"{key} 超出允许范围 {_fmt(item.minimum)}~{_fmt(item.maximum)}")
            continue
        cleaned[key] = coerced

    return cleaned, errors


def warnings(group: str, values: Mapping[str, Any] | None) -> list[str]:
    """**非阻断**的配置提示。写进保存结果，面板上渲染成黄条。

    为什么不像范围校验那样直接拒绝：这些组合「看起来怪」但可能正是老板要的。
    比如把 price_p0_pct 调到 0.001（比 price_p1_pct 还低），意味着「降价方向上的
    P1 永不触发、全都升成 P0」——粗暴，但对清仓监控是合理配置。
    配置层只负责提醒后果，不替老板做决定。
    """
    if not values:
        return []
    merged = defaults(group)
    merged.update({k: v for k, v in values.items() if v is not None})

    out: list[str] = []
    if group == GROUP_DIFF:
        if merged["price_p1_pct"] > merged["price_p0_pct"]:
            out.append(
                "price_p1_pct 大于 price_p0_pct：降价方向上的 P1 永远不会触发，"
                "所有超过 P1 门槛的降价都会升成 P0"
            )
        if merged["rank_p1_pct"] > merged["rank_p0_pct"]:
            out.append("rank_p1_pct 大于 rank_p0_pct：排名方向的 P2 档基本失效")
    elif group == GROUP_TIERING:
        if merged["overlap_s"] < merged["overlap_a"]:
            out.append("overlap_s 小于 overlap_a：S 级门槛比 A 级还松，会让 A 级永远评不上")
        if merged["keyword_overlap_gate"] > merged["overlap_a"]:
            out.append("入池门槛高于 A 级门槛：进池的竞品最多只能评到 B 级")
    return out


def _fmt(value: float | int | None) -> str:
    if value is None:
        return "∞"
    if isinstance(value, float) and value != int(value):
        return f"{value:g}"
    return str(value)
