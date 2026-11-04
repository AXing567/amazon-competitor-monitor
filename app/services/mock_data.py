"""Mock 数据集：一套自洽的假数据，让面板在没有真实数据源时也能完整演示。

## 它和 `scripts/demo_offline.py` 的分工

两者都造假数据，但服务对象不同：

    demo_offline.py   给**开发者验收面板**用 —— 独立库、`--clean`、能起服务
    mock_data.py      给**产品功能**用 —— 面板上的「Mock 模式」指向它

所以本模块不解析命令行、不预设环境变量、不自己起服务：它只做一件事——
在 Mock 模式下造一套数据。调用方是 API（`POST /api/dataset/mock/rebuild`）
或 `scripts/seed_mock.py`（CLI，给不想开浏览器的时候用）。

## 为什么自家锚点要「几乎填满」

Mock 数据的用途是让人看懂「数据齐了之后系统能给出什么」。锚点留一堆空的话，
面板一整屏都是「判不了」——那演示的是"系统不行"，而不是"系统能做什么"。

所以五维填满，**只留 `fba_available_units` 一个假设值**：
库存本来就是卖家自己的数，任何外部数据源都给不出。留这一个，
才能让「假设值告警」这条链路在 Mock 模式下真的走到；
如果全填真值，那条分支永远是死代码。

## 档位不是写死的，是引擎算的

`_rescore()` 调 `judge_tier()` 从库里的最新快照重新算一遍，算完与场景名逐个核对，
不一致就抛 `MockDataError`。

写死 `tier="S"` 的老路会产生「C 级对手 · 词重合 71%」这类自相矛盾的报告——
不报错、不崩溃，只是安静地教人相信一份假数据。所以修法不是"把常量改成正确的数"，
而是"不许有常量"。

## 这个模块只允许写 Mock 库

`_assert_isolated()` 在每次造数前比对两个连接串：一旦 `mock_database_url`
和 `database_url` 指向同一个文件，直接抛错。判据是**路径规范化后相等**，
不是"文件名不同"——两个不同的相对路径可以指同一个文件。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import delete, desc, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import (
    DATA_MODE_MOCK,
    db_file_path,
    ensure_mode_ready,
    get_data_mode,
    get_session,
    url_for_mode,
    using_data_mode,
)
from app.domain.diff import DiffEngine
from app.domain.tiering import judge_tier
from app.models import (
    Action,
    CollectRun,
    Competitor,
    CompetitorBinding,
    MyProduct,
    SeasonCalendar,
    Signal,
    Snapshot,
    ThresholdConfig,
    ThresholdVersion,
    User,
    UserIdentity,
)
from app.services import collector, identity, settings_store

# ============================================================
# 自家锚点
# ============================================================

#: Mock 自家的 ASIN。`B0MOCK` 前缀是这个模块的认领标记：清场只认它，
#: 别的 ASIN 一律视作「不是 Mock 造的」，见 `clear()`。
MOCK_MY_ASIN = "B0MOCK0001"

MOCK_COMP_PATH: list[str] = [
    "Home & Kitchen",
    "Kitchen & Dining",
    "Storage & Organization",
    "Racks & Holders",
    "Dish Racks",
]

#: 自家核心出单词（Top80% 流量词 → 占比）。权重和决定词重合的分母：
#: 竞品命中哪些词，就按这些权重累加再除以 1.70。
MOCK_CORE_KEYWORDS: dict[str, float] = {
    "roll up dish drying rack": 0.81,
    "over the sink dish rack": 0.55,
    "dish drying rack": 0.34,
}

MOCK_MY: dict[str, Any] = {
    "asin": MOCK_MY_ASIN,
    "platform": "amazon",
    "marketplace": "US",
    # --- 供给 ---
    # 库存是唯一标成假设值的字段：DOS = 2600 / 85 = 30.6 天，落在 normal 区间。
    "fba_available_units": 2600,
    "daily_sales_avg": 85.0,
    "lead_time_days": 32,
    # --- 财务 ---
    "current_price": 24.99,
    "break_even_price": 13.20,
    "target_margin_rate": 0.28,
    # --- 排位与口碑 ---
    "sub_category_rank": 1850,
    "rating": 4.5,
    "review_count": 2180,
    # --- Listing ---
    "title": (
        "Roll Up Dish Drying Rack, 17.5\" x 15.7\" Silicone Wrapped Over the Sink "
        "Dish Rack, Foldable Dish Drainer for Kitchen Counter"
    ),
    "image_url": "https://m.media-amazon.com/images/I/mock-roll-up-rack.jpg",
    "bullet_points": [
        "ROLL UP AND SAVE SPACE: rolls up to fit in a drawer when not in use",
        "HEAT RESISTANT TO 550F: doubles as a trivet for hot pots and pans",
        "NON-SLIP SILICONE EDGES: grips the sink rim, will not slide or scratch",
        "FITS MOST SINK SIZES: 17.5 x 15.7 in, covers standard kitchen sinks",
        "EASY TO CLEAN: dishwasher safe, rust-proof stainless steel core",
    ],
    # 产地溯源：这些都是卖家自己知道的静态知识，所以是手工录入。
    "source": "manual",
    "assumed_fields": ["fba_available_units"],
}

# ============================================================
# 竞品场景
# ============================================================

#: 与自家核心词同池的相关词。用来把竞品词表补到「一页 20 条」的真实形态——
#: 只给 3 个核心词的话，词表长度不像上游真实返回的样子。
RELATED_WORDS: tuple[str, ...] = (
    "silicone dish drying mat",
    "over sink dish drainer",
    "foldable dish rack",
    "kitchen sink organizer",
    "stainless steel dish rack",
    "dish rack with drip tray",
    "countertop dish drainer",
    "sink caddy",
    "sponge holder",
    "kitchen sink accessories",
    "drying rack for dishes",
    "rollable dish rack",
    "heat resistant dish mat",
    "silicone sink mat",
    "dish drying mat large",
    "dish rack over faucet",
    "utensils holder",
    "pot lid organizer",
    "kitchen organization",
)

#: 与自家完全无关的 60 个词。做两件事：
#: 1. 让「真不重合」的竞品有一个**够厚**的词表（60 条），
#:    否则守卫会把它判成「抽样太薄、判不了」，C 档就永远出不来；
#: 2. 演示「同节点、同价格带，但真的不抢同一个词」这种对手。
UNRELATED_WORDS: tuple[str, ...] = (
    "dog leash", "cat litter box", "yoga mat", "dumbbell set", "phone case",
    "laptop stand", "coffee grinder", "air fryer liner", "shower curtain",
    "bath towel set", "garden hose nozzle", "car phone mount", "wireless earbuds",
    "desk lamp", "notebook journal", "water bottle", "backpack", "running shoes",
    "sunscreen lotion", "hair dryer", "electric toothbrush", "kitchen knife set",
    "cutting board", "spice rack", "food storage container", "trash can",
    "toilet brush", "laundry basket", "clothes hanger", "ironing board",
    "vacuum cleaner bag", "air purifier filter", "humidifier", "space heater",
    "fan blade", "led strip light", "picture frame", "wall clock", "photo album",
    "board game", "jigsaw puzzle", "action figure", "dollhouse", "building blocks",
    "remote control car", "basketball", "tennis racket", "fishing rod",
    "camping tent", "sleeping bag", "hiking boots", "bicycle helmet", "skateboard",
    "roller skates", "swimming goggles", "surfboard wax", "snowboard bindings",
    "golf balls", "boxing gloves", "jump rope",
)

#: 9 个竞品，覆盖引擎能给出的**全部**结果形态。
#:
#: `tier` 是**期望值不是结论**：`_rescore()` 会拿它和引擎算出来的比，
#: 不一致就报错。它的作用是「这条场景本来想演示几级」，用来发现数据没调好。
#:
#: `keywords` 的取值决定词重合：
#:     all         命中 3 个核心词 → 加权重合 1.00
#:     top2        命中前 2 个     → 0.80
#:     one_low     只命中权重最低的那个 → 0.20
#:     none_thick  60 个无关词     → 0.00（样本够厚，这个 0 是结论）
#:     none_thin   20 个无关词     → 判不了（样本太薄，0 不是结论）
#:     empty       没有词          → 判不了（根本没采到）
SCENARIOS: list[dict[str, Any]] = [
    {
        "asin": "B0MOCK1001",
        "tier": "S",
        "brand": "Kitsure",
        "title": "Kitsure Roll Up Dish Drying Rack, 17.5\" x 15.7\", Silicone Coated",
        "keywords": "all",
        "kw_total": 320,
        "base_price": 26.99,
        "days": 4,
        # 今天降 2 块（-7.4%）：够 P1，让看板有一条「对手在动」的事
        "price_changes": {0: 24.99},
        "monthly_units": 3000,
        "rating": 4.7,
        "review_count": 4820,
        "sub_rank": 420,
        "status": "active",
    },
    {
        "asin": "B0MOCK1002",
        "tier": "A",
        "brand": "Surpahs",
        "title": "Surpahs Over the Sink Roll-Up Dish Drying Rack, Extra Large 20.5\"",
        "keywords": "top2",
        "kw_total": 640,
        "base_price": 23.99,
        "days": 3,
        "price_changes": {0: 22.49},
        # 体量是自家的 11 倍：词重合再高也不该当 S 主战场（打不过），
        # 所以 S 的「体量咬合」那一关故意卡掉它 → 落 A。
        "monthly_units": 28000,
        "rating": 4.6,
        "review_count": 15200,
        "sub_rank": 85,
        "status": "active",
    },
    {
        "asin": "B0MOCK1003",
        "tier": "A",
        "brand": "Tsmile",
        "title": "Tsmile Foldable Dish Drying Rack for Kitchen Sink, New 2026 Model",
        "keywords": "one_low",
        "kw_total": 210,
        "base_price": 19.99,
        "days": 3,
        # 上架 45 天 + 月销 1500 → 命中「新品黑马」判据，直接 A
        "release_days_ago": 45,
        "monthly_units": 1500,
        "rating": 4.4,
        "review_count": 310,
        "sub_rank": 1100,
        "status": "active",
    },
    {
        "asin": "B0MOCK1004",
        "tier": "B",
        "brand": "Attmu",
        "title": "Attmu Stainless Steel Dish Drying Rack with Drip Tray, 2 Tier",
        "keywords": "one_low",
        "kw_total": 480,
        "base_price": 31.99,
        "days": 3,
        # 34.49 / 24.99 = 1.380，仍在 1.4 带内。
        # 一开始写的是 34.99（= 1.4002），自检当场判出 C ——
        # 「价格出带 → 拒收」这条判据对小数点后第三位是敏感的，别凭手感写。
        "price_changes": {0: 34.49},
        "monthly_units": 900,
        "rating": 4.5,
        "review_count": 6400,
        "sub_rank": 2600,
        "status": "active",
    },
    {
        "asin": "B0MOCK1005",
        "tier": "C",
        "brand": "Home Basics",
        "title": "Home Basics Over the Sink Dish Drainer, Chrome",
        "keywords": "none_thick",
        # 60 个无关词 + 总量 150：60×3 ≥ 150 → 抽样**够厚**，
        # 于是「重合 0」是一个结论而不是「判不了」。总量写 800 时
        # 60×3 < 800 会被判成抽样太薄 → overlap=None → 这条就落不到 C 了。
        "kw_total": 150,
        "base_price": 24.99,
        "days": 2,
        "monthly_units": 700,
        "rating": 4.5,
        "review_count": 3900,
        "sub_rank": 3100,
        "status": "active",
    },
    {
        "asin": "B0MOCK1006",
        "tier": "C",
        "brand": "Bath Deco",
        "title": "Bath Deco Shower Caddy Shelf, Rustproof Stainless",
        "keywords": "none_thick",
        "kw_total": 150,
        "base_price": 49.99,
        "days": 2,
        # 类目末级不同 + 价格翻倍 → 两道门槛都过不去，拒收
        "category_path": ["Home & Kitchen", "Bath", "Bathroom Accessories", "Bathroom Shelves"],
        "monthly_units": 400,
        "rating": 4.3,
        "review_count": 1200,
        "sub_rank": 8800,
        "status": "paused",
    },
    {
        "asin": "B0MOCK1007",
        "tier": "B",
        "brand": "LILLIPURE",
        "title": "LILLIPURE Roll Up Dish Drying Rack, Mat Only",
        "keywords": "empty",
        "kw_total": None,
        "base_price": 21.99,
        "days": 2,
        "monthly_units": 1200,
        "rating": 4.2,
        "review_count": 780,
        "sub_rank": 2100,
        "status": "active",
    },
    {
        "asin": "B0MOCK1008",
        "tier": "B",
        "brand": "KitchenCloud",
        "title": "KitchenCloud Silicone Dish Drying Mat, Large",
        "keywords": "none_thin",
        "kw_total": 800,
        "base_price": 27.99,
        "days": 2,
        # 今天断货 → P0
        "stock_days": {0},
        "monthly_units": 1800,
        "rating": 4.6,
        "review_count": 2600,
        "sub_rank": 1400,
        "status": "active",
    },
    {
        "asin": "B0MOCK1009",
        "tier": "unknown",
        "brand": "Generic",
        "title": "Roll Up Dish Drying Rack (listing data unavailable)",
        "keywords": "top2",
        "kw_total": 150,
        "base_price": None,
        "days": 2,
        # 价格与类目都拿不到 → 可判维度只剩「流量池」一维 → 结论不足以下发，
        # 引擎给 unknown。这是「不知道」而不是「不合格」，两者不能混。
        "no_price": True,
        "no_category": True,
        "monthly_units": None,
        "rating": None,
        "review_count": None,
        "sub_rank": None,
        "status": "active",
    },
]


class MockDataError(RuntimeError):
    """Mock 数据造不出来 / 自检不通过。故意不吞：一套错数据比没数据危险。"""


# ============================================================
# 守卫
# ============================================================


def _db_file_of(url: str) -> Path:
    """复用 `db.db_file_path`，不在这里另写一份规范化逻辑。

    两份实现迟早会分叉，而分叉的后果恰好是「守卫以为两个库不同」——
    这个守卫存在的唯一理由就是它不能出错。
    """
    path = db_file_path(url=url)
    if path is None:
        raise MockDataError(f"Mock 模式只支持 SQLite，当前连接串是 {url!r}。")
    return path


def _assert_isolated() -> None:
    """Mock 库和真实库不能是同一个文件。

    判据是**规范化后的绝对路径相等**，不是比文件名：`data/x.db` 和
    `./data/x.db` 文件名相同但可能不是同一个文件；反过来，两个不同写法
    也可能指向同一个文件。只比字符串或只比文件名都会错一边。
    """
    mock = _db_file_of(url_for_mode(DATA_MODE_MOCK))
    live = _db_file_of(url_for_mode("live"))
    if mock == live:
        raise MockDataError(
            f"mock_database_url 与 database_url 指向同一个库（{mock}），拒绝造数。"
            "Mock 数据必须整体可删，混进真实库就再也分不清哪条是真的了。"
        )


# ============================================================
# 对外入口
# ============================================================


def build(quiet: bool = True) -> dict:
    """在 Mock 库里造一整套数据，返回统计。**会先清空 Mock 库。**

    调用方不需要自己切模式——本函数内部全程在 mock 上下文里跑，
    返回后上下文恢复。这样即使调用方是 live 模式下的 API 请求，也不会被污染。
    """
    _assert_isolated()
    ensure_mode_ready(DATA_MODE_MOCK)

    with using_data_mode(DATA_MODE_MOCK):
        if get_data_mode() != DATA_MODE_MOCK:      # pragma: no cover —— 防御性断言
            raise MockDataError("切到 Mock 模式失败，拒绝继续造数。")

        clear(quiet=True)
        my_id = _seed_anchor()
        uid = int(identity.ensure_default_user(settings.tenant_id)["id"])
        comps = _seed_competitors(my_id)
        for scen in SCENARIOS:
            _seed_history(scen, comps[scen["asin"]]["id"])
        comps = _rescore(comps, my_id, quiet=quiet)
        _seed_bindings(comps, my_id, uid)
        _seed_collect_runs(comps)
        _seed_thresholds(my_id, uid)
        _seed_actions(uid, my_id)
        stats = stats_in_current_mode()

    if not quiet:
        _report(comps, stats)
    return {"ok": True, **stats}


def clear(quiet: bool = True) -> dict:
    """清空 Mock 库的全部业务数据。**只删行，不删表。**

    删表的顺序按外键依赖反着来：`snapshots` / `signals` 引用 `competitors`，
    先删它们，否则 SQLite 的 `PRAGMA foreign_keys=ON` 会直接拒绝。
    """
    _assert_isolated()
    ensure_mode_ready(DATA_MODE_MOCK)

    with using_data_mode(DATA_MODE_MOCK):
        with get_session() as s:
            for model in (
                Signal,
                Snapshot,
                CompetitorBinding,
                CollectRun,
                Action,
                Competitor,
                ThresholdVersion,
                ThresholdConfig,
                UserIdentity,
                User,
                MyProduct,
                SeasonCalendar,
            ):
                s.execute(delete(model))
    return {"ok": True, "cleared": True}


def stats_in_current_mode() -> dict:
    """当前库的规模摘要。给面板的「Mock 数据」卡片用，也给自检用。"""
    with get_session() as s:
        my = s.execute(select(MyProduct).limit(1)).scalar_one_or_none()
        comps = list(s.execute(select(Competitor)).scalars())
        return {
            "db_file": str(_db_file_of(url_for_mode(get_data_mode()))),
            "my_asin": my.asin if my else None,
            "my_products": 1 if my else 0,
            "competitors": len(comps),
            "tiers": _count_tiers(comps),
            "signals": int(s.execute(select(Signal.id)).all().__len__()),
            "snapshots": int(s.execute(select(Snapshot.id)).all().__len__()),
            "last_capture": _latest_capture(s),
        }


def _count_tiers(comps: list[Competitor]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in comps:
        key = c.tier or "unknown"
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def _latest_capture(s: Session) -> Optional[str]:
    row = s.execute(
        select(Snapshot.captured_at).order_by(desc(Snapshot.captured_at)).limit(1)
    ).scalar_one_or_none()
    return row.isoformat(sep=" ", timespec="seconds") if row else None


# ============================================================
# 造数：锚点与用户
# ============================================================


def _seed_anchor() -> int:
    """按 ASIN 精确建自家锚点（从不读写"第一条"）。

    为什么不用 `pool.update_baseline()`：它对已存在的行是**更新**
    `.limit(1)` 的第一条。虽然这里已经是 Mock 库、理论上只有一条，
    但"按 ASIN 查、查不到才建"是更硬的约束——它把"改错对象"这件事
    从"靠调用顺序保证"变成"结构上不可能"。
    """
    with get_session() as s:
        my = s.execute(
            select(MyProduct).where(
                MyProduct.tenant_id == settings.tenant_id,
                MyProduct.asin == MOCK_MY_ASIN,
            ).limit(1)
        ).scalar_one_or_none()
        if my is None:
            my = MyProduct(
                tenant_id=settings.tenant_id,
                asin=MOCK_MY_ASIN,
                platform="amazon",
                marketplace="US",
            )
            s.add(my)
            s.flush()

        for key, value in MOCK_MY.items():
            setattr(my, key, value)
        my.core_keywords = dict(MOCK_CORE_KEYWORDS)
        my.category_path = list(MOCK_COMP_PATH)
        s.flush()
        return int(my.id)


def _seed_competitors(my_id: int) -> dict[str, dict]:
    """只写「这个竞品是谁」。判定类字段一个都不写，交给 `_rescore()`。

    **不要**在这里写 `tier` / `tier_reasons` / `price_ratio` / `keyword_overlap` /
    `node_match`。历史教训：这里曾经用一份默认字典兜底数字、`tier` 却来自场景名，
    两者不同源 → 面板上出现「C 级对手 · 词重合 71%」。那种错不报错、不崩溃，
    只是安静地教人相信一份自相矛盾的报告。
    """
    now = datetime.now()
    out: dict[str, dict] = {}
    with get_session() as s:
        for scen in SCENARIOS:
            comp = Competitor(
                tenant_id=settings.tenant_id,
                my_product_id=my_id,
                platform="amazon",
                marketplace="US",
                asin=scen["asin"],
                brand=scen["brand"],
                title=scen["title"],
                # 功能形态是人工确认输入，引擎算不出来。全部给 True（同形态），
                # 否则每条都会带一句「请人工确认是否为配件」——演示里没有必要。
                functional_match=True,
                status=scen.get("status", "active"),
                last_checked_at=now if scen.get("status") != "paused" else now - timedelta(days=2),
            )
            s.add(comp)
            s.flush()
            out[scen["asin"]] = {"id": comp.id, "scen": scen, "title": scen["title"]}
    return out


# ============================================================
# 造数：快照与信号
# ============================================================


def _mock_keywords(mode: str) -> list[dict]:
    """按模式生成词表。补到 20 条是为了贴近「上游一页 20 条」的真实形态。"""
    if mode == "empty":
        return []
    if mode == "none_thick":
        words = list(UNRELATED_WORDS[:60])
    elif mode == "none_thin":
        words = list(UNRELATED_WORDS[:20])
    else:
        if mode == "all":
            head = list(MOCK_CORE_KEYWORDS)
        elif mode == "top2":
            head = list(MOCK_CORE_KEYWORDS)[:2]
        elif mode == "one_low":
            head = [list(MOCK_CORE_KEYWORDS)[2]]
        else:                                   # pragma: no cover —— 场景写错要立刻炸
            raise MockDataError(f"未知的 keywords 模式 {mode!r}（见 SCENARIOS）")
        words = head + list(RELATED_WORDS[: 20 - len(head)])

    return [
        {"keyword": w, "traffic_share": round(max(0.01, 0.06 - i * 0.002), 4)}
        for i, w in enumerate(words)
    ]


def _pick(default: Any, changes: Optional[dict[int, Any]], days_ago: int) -> Any:
    """`changes` 的语义：从「几天前」那天起，值变成这个。

    从最老的一天开始应用，后应用的覆盖先应用的，得到一条阶梯而不是一个突变。
    """
    if not changes:
        return default
    value = default
    for key in sorted(changes, reverse=True):
        if days_ago <= key:
            value = changes[key]
    return value


def _seed_history(scen: dict, cid: int) -> int:
    """一个竞品的时间线：快照 → 走真实的 `DiffEngine` 判出信号。

    **不假造信号**：所有 level 都是 `DiffEngine.compare()` 判出来的。
    这样阈值一改，Mock 数据的结论跟着变——这才是演示该有的样子，
    否则演示只是在展示一段写死的数字。
    """
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    days = int(scen.get("days", 2))
    price_changes = scen.get("price_changes")
    stock_days = scen.get("stock_days") or set()
    kw = _mock_keywords(scen["keywords"])
    cat = scen.get("category_path", MOCK_COMP_PATH)

    release = None
    if scen.get("release_days_ago") is not None:
        release = (date.today() - timedelta(days=int(scen["release_days_ago"]))).isoformat()

    rows: list[Snapshot] = []
    for days_ago in range(days - 1, -1, -1):
        price = None if scen.get("no_price") else _pick(scen["base_price"], price_changes, days_ago)
        in_stock = days_ago not in stock_days
        rows.append(
            Snapshot(
                competitor_id=cid,
                captured_at=now - timedelta(days=days_ago),
                price=price,
                coupon_value=0.0,
                net_price=price,
                rank_main=_pick(scen.get("sub_rank"), None, days_ago),
                rank_sub=_pick(scen.get("sub_rank"), None, days_ago),
                rank_label="Dish Racks",
                monthly_units=_pick(scen.get("monthly_units"), None, days_ago),
                rating=_pick(scen.get("rating"), None, days_ago),
                review_count=_pick(scen.get("review_count"), None, days_ago),
                in_stock=in_stock,
                stock_hint="IN_STOCK" if in_stock else "OOS",
                title=scen["title"],
                release_date=release,
                keywords=kw or None,
                traffic_keywords_total=scen.get("kw_total"),
                category_path=None if scen.get("no_category") else list(cat),
                data_quality="full" if price is not None else "partial",
                source="mock",
            )
        )

    with get_session() as s:
        for r in rows:
            s.add(r)
        s.flush()

        total = 0
        for i in range(1, len(rows)):
            prev, curr = rows[i - 1], rows[i]
            prev_u = collector.row_to_unified(prev)
            curr_u = collector.row_to_unified(curr)
            oos_confirmed = (prev.stock_hint or "").upper() == "OOS" or prev.in_stock is False
            phase = "warmup" if i == 1 else "normal"
            drafts = DiffEngine(phase).compare(
                prev_u,
                curr_u,
                tier=str(scen["tier"]),
                my_price=MOCK_MY["current_price"],
                oos_confirmed=oos_confirmed,
            )
            for d in drafts:
                s.add(
                    Signal(
                        tenant_id=settings.tenant_id,
                        competitor_id=cid,
                        category=d.category,
                        signal_type=d.signal_type,
                        level=d.level,
                        before_value=d.before_value,
                        after_value=d.after_value,
                        change_pct=d.change_pct,
                        description=d.description,
                        detected_at=curr.captured_at,
                        # P0/P1 当天就推了；P2 进周报，不打扰
                        pushed_at=curr.captured_at if d.level in ("P0", "P1") else None,
                    )
                )
                total += 1
    return total


# ============================================================
# 分层：由引擎算，并自检
# ============================================================


def _latest_snapshot(s: Session, cid: int) -> Optional[Snapshot]:
    return s.execute(
        select(Snapshot)
        .where(Snapshot.competitor_id == cid)
        .order_by(desc(Snapshot.captured_at))
        .limit(1)
    ).scalar_one_or_none()


def _judge(s: Session, my: MyProduct, comp: Competitor):
    row = _latest_snapshot(s, comp.id)
    if row is None:
        return None, None
    unified = collector.row_to_unified(row, comp.marketplace)
    res = judge_tier(
        my_price=my.current_price,
        my_core_keywords=dict(my.core_keywords or {}),
        my_category_path=list(my.category_path or []) or None,
        my_daily_units=my.daily_sales_avg,
        comp=unified,
        functional_match=comp.functional_match,
        thresholds=settings_store.resolve_tiering_thresholds(settings.tenant_id, my.id),
    )
    return res, row


def _rescore(comps: dict[str, dict], my_id: int, quiet: bool = True) -> dict[str, dict]:
    """用真实引擎复算全部竞品的分层 → 回写 → **自检**。

    自检做两件事，缺一不可：

    1. **存 == 重算**：从库里读回来的 `tier` / `keyword_overlap` / `price_ratio` /
       `node_match` 必须与再跑一次引擎的结果一致。这类 bug 的特征是
       「层级对、数字错」，只看 `tier` 永远发现不了。
    2. **重算 == 场景名**：引擎算出来的必须等于 `SCENARIOS[*]["tier"]`。
       不等说明 Mock 数据没调好（价格 / 词表 / 类目路径），
       该去调数据，**不许放宽断言，更不许改引擎去迁就演示**。
    """
    with get_session() as s:
        my = s.get(MyProduct, my_id)
        if my is None:
            raise MockDataError(f"锚点 id={my_id} 不存在，Mock 数据不完整。")
        for asin, entry in comps.items():
            comp = s.get(Competitor, entry["id"])
            if comp is None:
                raise MockDataError(f"{asin} 的竞品行不存在，Mock 数据不完整。")
            res, row = _judge(s, my, comp)
            if res is None:
                raise MockDataError(f"{asin} 没有任何快照，无法判分层。")
            comp.tier = res.effective_tier
            comp.tier_reasons = dict(res.tier_reasons)
            comp.tier_updated_at = datetime.now()
            comp.price_ratio = res.price_ratio
            comp.keyword_overlap = res.keyword_overlap
            comp.node_match = res.node_match
        s.flush()

    problems: list[str] = []
    refreshed: dict[str, dict] = {}
    with get_session() as s:
        my = s.get(MyProduct, my_id)
        for asin, entry in comps.items():
            comp = s.get(Competitor, entry["id"])
            res, _row = _judge(s, my, comp)
            want = res.tier_reasons
            got = comp.tier_reasons or {}
            bad_keys = [
                k
                for k in ("price_ratio", "price_band_ok", "keyword_overlap",
                          "keyword_pool_ok", "node_match")
                if got.get(k) != want.get(k)
            ]
            if comp.tier != res.effective_tier or bad_keys:
                problems.append(
                    f"{asin} 存={comp.tier} 重算={res.effective_tier} 不一致字段={bad_keys or ['tier']}"
                )
            expected = str(entry["scen"]["tier"])
            if res.effective_tier != expected:
                problems.append(
                    f"{asin} 场景名写的是 {expected}，引擎算出来是 {res.effective_tier}"
                    f"（kw={res.keyword_overlap} ratio={res.price_ratio} "
                    f"node={res.node_match} dims={res.decidable_dims}）。"
                    "这是 Mock 数据没调好，请调 SCENARIOS 里的价格/词表/类目，"
                    "不要放宽断言、也不要改引擎。"
                )
            refreshed[asin] = {
                "id": comp.id,
                "tier": comp.tier,
                "tier_reasons": dict(got),
                "title": entry.get("title", ""),
                "scen": entry["scen"],
            }

    if problems:
        raise MockDataError("分层复算自检失败：\n" + "\n".join(f"  · {p}" for p in problems))
    return refreshed


# ============================================================
# 造数：绑定 / 采集日志 / 阈值 / 动作
# ============================================================


def _seed_bindings(comps: dict[str, dict], my_id: int, uid: int) -> None:
    """两条对标绑定。`tier` 与 `tier_reasons` **从 competitors 行读**，
    不另写字面量：绑定是「当初凭什么判成这样」的存档，与竞品行对不上
    就等于留了两份互相矛盾的证据。"""
    for asin in ("B0MOCK1001", "B0MOCK1002"):
        entry = comps.get(asin)
        if entry is None:
            continue
        with get_session() as s:
            comp = s.get(Competitor, entry["id"])
            if comp is None:
                continue
            tier = comp.tier
            reasons = dict(comp.tier_reasons or {})
        binding_res = _binding_upsert(asin, entry["id"], my_id, tier, reasons, uid)
        if not binding_res.get("ok") and not quiet_ok(binding_res):
            raise MockDataError(f"绑定 {asin} 失败：{binding_res.get('error')}")


def quiet_ok(res: dict) -> bool:
    """绑定失败多半是「已经有了」——那不是错。真正的错要抛。"""
    msg = str(res.get("error") or "")
    return "已存在" in msg or "exists" in msg.lower()


def _binding_upsert(asin: str, cid: int, my_id: int, tier: str, reasons: dict, uid: int) -> dict:
    from app.services import binding as binding_svc

    return binding_svc.upsert_from_judgment(
        tenant_id=settings.tenant_id,
        competitor_id=cid,
        my_product_id=my_id,
        tier=tier,
        tier_reasons=reasons,
        user_id=uid,
        confidence=0.82,
    )


def _seed_collect_runs(comps: dict[str, dict]) -> None:
    """采集日志：既有成功也有失败。没有失败的健康页看不出"失败长什么样"。"""
    now = datetime.now()
    fail_plan = {
        "B0MOCK1007": {0: "关键词接口超时（12s）", 1: "关键词接口超时（12s）"},
        "B0MOCK1009": {0: "商品详情返回 404，可能是 ASIN 已下架"},
    }
    with get_session() as s:
        for scen in SCENARIOS:
            cid = comps[scen["asin"]]["id"]
            fails = fail_plan.get(scen["asin"], {})
            for days_ago in range(min(3, int(scen.get("days", 2)))):
                err = fails.get(days_ago)
                s.add(
                    CollectRun(
                        tenant_id=settings.tenant_id,
                        competitor_id=cid,
                        asin=scen["asin"],
                        started_at=now - timedelta(days=days_ago, minutes=7),
                        finished_at=now - timedelta(days=days_ago, minutes=6),
                        ok=err is None,
                        data_quality="failed" if err else "full",
                        signal_count=0 if err else 1,
                        trigger="schedule",
                        errors=[err] if err else None,
                        duration_ms=30000 if err else 1450,
                    )
                )


def _seed_thresholds(my_id: int, uid: int) -> None:
    """两层各写一点覆盖值，并**故意重叠同一个键**（`price_p1_pct`）。

    不写的话阈值页全部显示"系统默认"、健康页的「阈值来源」是一张空表——
    而这两处恰恰最该被看懂：改了哪个键、现在生效的是哪一层。
    重叠的那个键让人一眼看到"产品层盖住了公司层"。
    """
    from app.services import settings_store as store

    company = store.save(
        scope="company",
        group_name="diff",
        values={"price_p0_pct": 0.025, "price_p1_pct": 0.008},
        tenant_id=settings.tenant_id,
        operator="Mock 数据",
        note="Mock：旺季收紧价格判定",
    )
    product = store.save(
        scope="product",
        group_name="diff",
        values={"price_p1_pct": 0.012},
        scope_id=str(my_id),
        tenant_id=settings.tenant_id,
        operator="Mock 数据",
        note="Mock：这个产品对小幅降价更敏感",
    )
    for label, res in (("company", company), ("product", product)):
        if not res.get("ok"):
            raise MockDataError(f"阈值覆盖写入失败（{label}）：{res.get('error')}")


def _seed_actions(uid: int, my_id: int) -> None:
    """三条动作记录：让「动作记录」页不是空的。

    动作是**人做过的事**，不是系统结论。所以这里写的都是"我做了什么"，
    而不是"系统建议我做什么"——后者属于 Decision，不在这张表里。

    `executed_at` 只能事后补：`record()` 没有这个参数（它记的是"此刻做的事"）。
    直接改行的原因不是图省事——Mock 数据要演示"一周前做了什么"，而
    `record()` 的时间戳永远是现在；不改的话三条动作会挤在同一分钟里，
    时间线看起来像批量导入。改动范围只有本模块自己造的那三行。
    """
    from app.services import action_log as log_svc

    with get_session() as s:
        comp = s.execute(
            select(Competitor).where(Competitor.asin == "B0MOCK1001").limit(1)
        ).scalar_one_or_none()
        cid = comp.id if comp else None

    entries = [
        ("跟价", "对手降到 24.99，我们跟到 24.49 保住 buybox", 2),
        ("改主图", "把「可折叠」拍到第一张主图上", 5),
        ("补货", "按 32 天补货周期下了 3000 件", 9),
    ]
    created: list[int] = []
    for action_type, desc, _days in entries:
        res = log_svc.record(
            tenant_id=settings.tenant_id,
            action_type=action_type,
            description=desc,
            user_id=uid,
            my_product_id=my_id,
            competitor_id=cid,
            source="panel",
        )
        if not res.get("ok"):
            raise MockDataError(f"动作记录写入失败：{res.get('error')}")
        created.append(int(res["action_id"]))

    with get_session() as s:
        for action_id, (_t, _d, days_ago) in zip(created, entries):
            row = s.get(Action, action_id)
            if row is not None:
                row.executed_at = datetime.now() - timedelta(days=days_ago)


# ============================================================
# 自检输出
# ============================================================


def _report(comps: dict[str, dict], stats: dict) -> None:
    print(f"Mock 数据已写入 {stats['db_file']}")
    print(f"  自家 ASIN：{stats['my_asin']} · 竞品 {stats['competitors']} 个 · "
          f"快照 {stats['snapshots']} 条 · 信号 {stats['signals']} 条")
    print("  档位分布：" + " · ".join(f"{k}×{v}" for k, v in stats["tiers"].items()))
    for asin, entry in comps.items():
        print(f"    {asin}  {entry['tier']:<8} {entry['title'][:52]}")
