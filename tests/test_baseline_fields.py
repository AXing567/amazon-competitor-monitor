"""自家基线的字段级明细：面板上每一格填的到底是什么。

## 为什么这一层要单独测

`assess()` 只回答「五维各差几个」。面板还要回答「那一格现在是什么数、
这个数从哪来、能不能拿去决策」——这三件事之前没有任何接口提供，
于是「自家基线」页只能显示"财务锚点 3/3 已填"，看不到 3 是哪三个数。

本组测试守的是：**该有的格子一个不少**、**假设值一定被标出来**、
**值类型说对了**（金额 vs 比例 vs 名次，格式化由面板做）。
"""
from __future__ import annotations

import pytest

from app.domain import baseline as bl
from app.domain.baseline import DIMENSIONS, FIELD_KINDS, describe


class _FakeProduct:
    """够 `describe` 用的最小对象。不用 ORM：这层是纯函数，测它就该不连库。"""

    def __init__(self, **kw):
        for dim in DIMENSIONS:
            for name, _label in dim.fields:
                setattr(self, name, None)
        self.asin = "B0TEST0001"
        self.source = bl.SOURCE_MANUAL
        self.assumed_fields = []
        self.category_path = None
        for k, v in kw.items():
            setattr(self, k, v)


# ============================================================
# 覆盖度
# ============================================================


def test_describe_covers_every_dimension_field():
    """五维里的每个字段都必须出现在 `groups` 里。

    漏一个的后果不是报错，而是面板上少一行——人不会发现少了一行，
    只会觉得"这一维好像没什么可填的"。
    """
    d = describe(_FakeProduct())
    got = {(g["key"], f["name"]) for g in d["groups"] for f in g["fields"]}
    want = {(dim.key, name) for dim in DIMENSIONS for name, _ in dim.fields}
    assert got == want


def test_describe_exposes_category_path_as_extra():
    """`category_path` 不在五维里（不计入就绪度），但必须露出来。

    它是「节点」那一维的唯一输入。不显示的话，五维全绿会让人以为万事俱备，
    而节点维度其实还是瞎的。
    """
    d = describe(_FakeProduct(category_path=["A", "B", "C"]))
    extra = {f["name"]: f for f in d["extras"]}
    assert "category_path" in extra
    assert extra["category_path"]["value"] == ["A", "B", "C"]
    assert extra["category_path"]["filled"] is True


def test_every_covered_field_has_a_kind():
    """每个字段都得有值类型，否则面板只能一律当文本处理。

    当文本处理的后果：`0.28` 显示成 `0.28` 而不是 `28.0%`，
    `22.99` 显示成 `22.99` 而不是 `$22.99` —— 数字都对，但读起来要靠猜。
    """
    covered = {name for dim in DIMENSIONS for name, _ in dim.fields}
    covered.add("category_path")
    missing = covered - set(FIELD_KINDS)
    assert not missing, f"这些字段没有声明值类型：{sorted(missing)}"


def test_kind_mapping_for_the_tricky_ones():
    assert FIELD_KINDS["current_price"] == "money"
    assert FIELD_KINDS["target_margin_rate"] == "percent"
    assert FIELD_KINDS["sub_category_rank"] == "rank"
    assert FIELD_KINDS["core_keywords"] == "keywords"
    assert FIELD_KINDS["category_path"] == "path"


# ============================================================
# 假设值：这是这一页最该说清的事
# ============================================================


def test_assumed_fields_are_marked_on_the_row():
    p = _FakeProduct(
        current_price=24.99,
        break_even_price=13.20,
        assumed_fields=["break_even_price"],
    )
    rows = {f["name"]: f for g in describe(p)["groups"] for f in g["fields"]}
    assert rows["break_even_price"]["assumed"] is True
    assert rows["current_price"]["assumed"] is False


def test_assumed_fields_accepts_json_string():
    """库里是 JSON 列，但手工改库 / 老数据可能是字符串。

    形状不对就当空（读取侧宽容），但字符串形式的合法列表要能解析出来——
    否则「哪些是假设值」会安静地变成"一个都没有"，而那是**最危险**的那种错：
    页面看起来一切正常，结论其实全是拍出来的。
    """
    p = _FakeProduct(break_even_price=13.2, assumed_fields='["break_even_price"]')
    rows = {f["name"]: f for g in describe(p)["groups"] for f in g["fields"]}
    assert rows["break_even_price"]["assumed"] is True


def test_broken_assumed_shape_does_not_crash():
    p = _FakeProduct(assumed_fields='{"not": "a list"}')
    d = describe(p)                       # 不抛
    assert d["assumed_fields"] == []


def test_source_label_is_human_readable():
    p = _FakeProduct(source=bl.SOURCE_SELLERSPRITE)
    assert describe(p)["source_label"] == "卖家精灵反推"


def test_unset_source_says_unlabelled_not_manual():
    """没有 source 的老数据不许被说成"手工录入"——那是编造溯源。

    `source` 这一列是后加的，之前填进去的锚点真的不知道从哪来。
    猜成 manual 会让人以为"这是我自己填的，可信"，而实际可能是脚本写的。
    """
    p = _FakeProduct()
    p.source = None
    assert describe(p)["source_label"] == "未标注"


# ============================================================
# 空值与真实值的区别
# ============================================================


def test_missing_value_is_not_confused_with_zero():
    """`0` 是一个值，`None` 是没填。两者在「已填几项」里必须算得不一样。

    这一条守的是 `_has_value`：它把 `0` 当"没有"。放在库存上是对的
    （库存 0 件等于没货，业务上就是"这个维度不成立"），
    但这条边界必须被显式测过，不然下次改它就没人知道会影响什么。
    """
    p = _FakeProduct(fba_available_units=0, daily_sales_avg=85.0, lead_time_days=32)
    rows = {f["name"]: f for g in describe(p)["groups"] for f in g["fields"]}
    assert rows["fba_available_units"]["filled"] is False
    assert rows["daily_sales_avg"]["filled"] is True


def test_group_counts_match_rows():
    p = _FakeProduct(current_price=24.99, rating=4.5, review_count=100)
    for g in describe(p)["groups"]:
        assert g["filled"] == sum(1 for f in g["fields"] if f["filled"])
        assert g["total"] == len(g["fields"])


# ============================================================
# 能力层出口
# ============================================================


def test_get_baseline_capability_reports_missing_anchor():
    """没有任何锚点时返回 `exists: False` 且**不抛异常**。

    面板要据此显示"还没有自家锚点"的引导，而不是一张错误卡片——
    空库是初始状态，不是故障。
    """
    from app.capabilities.queries import get_baseline
    from app.capabilities.types import CapContext

    res = get_baseline(CapContext(tenant_id="no-such-tenant"))
    assert res["ok"] is True
    assert res["exists"] is False
    assert res["baseline"] is None
    assert res["hint"], "要告诉人下一步填什么"


def test_get_baseline_capability_is_registered():
    """能力必须在 registry 里，否则 `/api/cap/get_baseline` 会 404，
    而面板拿到的是一句"能力不存在"——看不出是自己没注册。"""
    from app.capabilities import to_schema

    schema = to_schema("get_baseline")
    assert schema["name"] == "get_baseline"
    assert schema["group"] == "query"
