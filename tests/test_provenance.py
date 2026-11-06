"""锚点溯源：把「查来的数」和「拍的数」分开。

## 这组用例守的是什么

pilot 期 SP-API 还没接入，临时拿卖家精灵上一个真实商品的公开数据顶替「自家锚点」。
问题在于锚点里各字段的**信誉不一样**：

| 字段 | 来源 | 能不能信 |
|---|---|---|
| `current_price` / `rating` / `review_count` / `sub_category_rank` | 卖家精灵真实公开数据 | 可信 |
| `break_even_price` / `fba_available_units` / `lead_time_days` | 卖家精灵根本没有，只能填假设值 | 不可信 |
| `daily_sales_avg` | 卖家精灵的 BSR 幂律估算，不是订单级真值 | 打折看 |

如果这些混在一张表里、面板上一视同仁，一个月后没人分得清哪个数是查来的。
更糟的是「由拍出来的保本价推导出的提价建议」会**看起来和真的一样**——
这就是本项目此前反复修过的同一类问题：*看着像证据的字段*。

所以这里测的不是算法，是**链路**：写进去 → 存得住 → 读得出来 → 体检会喊。
任何一段断掉，这件事就退化成"只在库里存在"，面板照旧把假设值当真值显示，
而且**全程不会有一处报错**。

## 为什么每条用独立租户

`conftest.py` 已给本模块一份空库，但锚点是「租户唯一」的，
同一租户跑两条用例会互相覆盖。每条用例自己造一个租户才能独立断言。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.db as app_db                                            # noqa: E402
from app.capabilities import call as cap_call                      # noqa: E402
from app.capabilities import queries as queries_cap                # noqa: E402
from app.capabilities import writes as writes_cap                  # noqa: E402
from app.capabilities.types import SOURCE_PANEL, CapContext        # noqa: E402
from app.db import (                                              # noqa: E402
    ensure_mode_ready,
    get_session,
    init_db,
    using_data_mode,
)
from app.domain import baseline as bl                              # noqa: E402
from app.migrations import migrate                                 # noqa: E402
from app.models import MyProduct                                   # noqa: E402
from app.services import pool                                      # noqa: E402

TS = "t-prov"


def _ctx(tag: str) -> CapContext:
    return CapContext(tenant_id=f"{TS}-{tag}", user_id=1, source=SOURCE_PANEL)


def _row(tenant: str) -> MyProduct:
    with get_session() as s:
        return s.query(MyProduct).filter(MyProduct.tenant_id == tenant).one()


# ============================================================
# ① 迁移：两列要被补上，而且必须幂等（服务每次启动都会跑一遍）
# ============================================================


def test_migration_adds_provenance_columns_idempotently(tmp_path):
    """老库（没有这两列）跑一次就补上，第二次是空操作、不抛异常。"""
    db = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db.as_posix()}", future=True)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE my_products (id INTEGER PRIMARY KEY, tenant_id VARCHAR(64))"
        )

    first = migrate(engine)
    assert any("my_products.source" in a for a in first), first
    assert any("my_products.assumed_fields" in a for a in first), first

    # 不幂等的话，服务每次启动都会在这里炸一次；而"启动跑两遍"是常态
    # （uvicorn reload、pytest 的 init_db()、脚本各跑一次）。
    second = migrate(engine)
    assert second == [], f"迁移不幂等，第二次又动了：{second}"

    # 列真的在库里，不是只有返回值说加了
    with engine.connect() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(my_products)")}
    assert {"source", "assumed_fields"} <= cols, cols
    engine.dispose()


def test_migrate_on_live_schema_is_noop():
    """当前 schema（create_all 建出来的）上再跑迁移：什么都不该做。"""
    init_db()
    assert migrate(app_db.engine) == []


# ============================================================
# ② 写入往返：从能力层入口进，从库里读回来，值与类型都得对
# ============================================================



@pytest.fixture()
def mock_mode():
    """把这条用例切到 Mock 库。

    卖家精灵反推是 **Mock 的构造方式**，不是真实模式的数据来源：
    `pool.update_baseline` 在真实模式下会直接拒掉 `source=sellersprite_proxy`。
    所以要验「反推锚点能不能存、能不能读回来」的用例，必须站在 Mock 模式下跑——
    放在真实模式下，它验的是一个已经被禁止的写入。
    """
    ensure_mode_ready("mock")
    with using_data_mode("mock"):
        yield

def test_set_baseline_roundtrips_source_and_assumed_fields(mock_mode):
    """走 `capabilities.call`（不是直接调 handler）——这条路径才证明 spec 补对了。

    `_coerce_params` 只认 `Capability.params` 里声明过的键，未声明的**静默丢弃**。
    所以如果只在 handler 里加了参数、忘了改 spec，本用例会看到库里
    `source` 还是默认值 `manual`、`assumed_fields` 是 `None`：传了，白传。
    """
    ctx = _ctx("roundtrip")
    res = cap_call(
        "set_baseline",
        ctx=ctx,
        asin="B0PILOT001",
        current_price=24.99,
        rating=4.4,
        review_count=812,
        sub_category_rank=1280,
        break_even_price=17.5,
        fba_available_units=300,
        lead_time_days=35,
        source="sellersprite_proxy",
        assumed_fields=["break_even_price", "fba_available_units", "lead_time_days"],
    )
    assert res.get("ok") is True, res

    my = _row(ctx.tenant_id)
    assert my.source == "sellersprite_proxy"
    # 类型必须是 list，不是 JSON 字符串：面板要遍历它、能力层要拿它做集合运算，
    # 字符串会让 `for x in ...` 逐字符遍历，而没有任何一处报错。
    assert isinstance(my.assumed_fields, list), my.assumed_fields
    assert my.assumed_fields == [
        "break_even_price",
        "fba_available_units",
        "lead_time_days",
    ]
    # 查来的真值原样保留，不会因为标注了溯源就丢字段
    assert my.current_price == pytest.approx(24.99)
    assert my.review_count == 812


def test_assumed_fields_accepts_json_string_from_api():
    """面板表单 / HTTP body 常常传 JSON 字符串，要能收并归一成 list。"""
    ctx = _ctx("jsonstr")
    res = writes_cap.set_baseline(
        ctx,
        asin="B0PILOT002",
        current_price=19.99,
        assumed_fields='["break_even_price"]',
    )
    assert res.get("ok") is True, res
    assert _row(ctx.tenant_id).assumed_fields == ["break_even_price"]


def test_inferred_break_even_is_marked_as_assumed():
    """按毛利率**推算**出来的保本价，本身就是一个假设值，必须自己承认。

    以前这里只是把推算结果塞进库里，`out["inferred"]` 一闪而过；
    之后体检把它当实测值，接着「该不该跟价 / 要不要提价」就建立在一个拍出来的数上。
    """
    ctx = _ctx("inferred")
    res = writes_cap.set_baseline(
        ctx, asin="B0PILOT003", current_price=24.99, target_margin_rate=0.3
    )
    assert res.get("ok") is True, res
    assert res["inferred"]["break_even_price"] == pytest.approx(17.49, abs=0.01)

    my = _row(ctx.tenant_id)
    assert "break_even_price" in (my.assumed_fields or [])
    assert any("保本价" in w for w in res["warnings"]), res["warnings"]


# ============================================================
# ③ 形状校验：坏值当场拒，而且**不许写库**
# ============================================================


@pytest.mark.parametrize(
    "bad, tag",
    [
        ("break_even_price", "str"),      # 裸字符串：读回来会被逐字符遍历
        (["break_even_price", 1], "int"), # 混了非字符串
        ([1, 2], "nums"),                 # 全非字符串
        ({"a": 1}, "dict"),               # 对象不是列表
        (3, "scalar"),                    # 标量
    ],
)
def test_assumed_fields_rejects_bad_shape_and_writes_nothing(bad, tag):
    ctx = _ctx(f"bad-{tag}")
    res = writes_cap.set_baseline(
        ctx, asin="B0PILOT004", current_price=24.99, assumed_fields=bad
    )
    assert res["ok"] is False, res
    assert res.get("error_kind") == "param", res
    assert res.get("hint"), res

    # 关键断言：形状不对时**连锚点都不许建**。
    # 否则库里会留下一个 current_price=24.99、assumed_fields 是垃圾的行，
    # 而用户只看到一句红字，以为"没生效"。
    with get_session() as s:
        assert s.query(MyProduct).filter(MyProduct.tenant_id == ctx.tenant_id).count() == 0


def test_source_rejects_unknown_value():
    ctx = _ctx("bad-src")
    res = writes_cap.set_baseline(ctx, asin="B0PILOT005", source="sp_api_v2")
    assert res["ok"] is False, res
    assert res.get("error_kind") == "param", res
    assert "manual" in res["hint"]


# ============================================================
# ④ 体检：假设值必须被喊出来，且不许过度告警
# ============================================================


def test_check_anchor_warns_about_assumed_fields(mock_mode):
    """`check_anchor` 的告警要说清三件事：哪几个字段、哪些结论、去哪办。"""
    ctx = _ctx("warn")
    res = writes_cap.set_baseline(
        ctx,
        asin="B0PILOT006",
        current_price=24.99,
        rating=4.4,
        break_even_price=17.5,
        fba_available_units=300,
        lead_time_days=35,
        source="sellersprite_proxy",
        assumed_fields=["break_even_price", "fba_available_units", "lead_time_days"],
    )
    assert res.get("ok") is True, res

    data = queries_cap.check_anchor(ctx)
    assert data["ok"] is True, data
    joined = "\n".join(data["warnings"])

    # 具体字段的中文名，不是"部分字段是模拟的"
    assert "保本价" in joined, joined
    assert "FBA 可售库存" in joined, joined
    assert "补货周期" in joined, joined
    # 后果要说出来，否则用户读完不知道该怎么办
    assert "不可信" in joined, joined
    # 受影响的结论要具体（复用 DIMENSIONS[].informs，不另写一份）
    assert "跟价" in joined or "提价" in joined, joined
    # 查来的真值**不许**被一起标成假设值，否则这句告警就被稀释成噪音
    assert "当前售价" not in joined, joined

    # 面板之外，飞书/文字版也要能看到——这才是它真的"显示出来"的证据
    assert "不可信" in data["text"], data["text"]
    assert "保本价" in data["text"], data["text"]


def test_check_anchor_silent_when_nothing_is_assumed():
    """`assumed_fields` 为空 → 不许出这条告警（防过度告警）。

    过度告警的代价是真实的：一旦"什么都是假设值"，这条告警就会被无视，
    于是真正假设的那一次也没人看。
    """
    ctx = _ctx("clean")
    res = writes_cap.set_baseline(
        ctx,
        asin="B0PILOT007",
        current_price=24.99,
        break_even_price=17.5,
        fba_available_units=300,
        daily_sales_avg=12,
        lead_time_days=35,
        source="manual",
        assumed_fields=[],
    )
    assert res.get("ok") is True, res

    data = queries_cap.check_anchor(ctx)
    assert not any("假设值" in w for w in data["warnings"]), data["warnings"]
    assert not any("不可信" in w for w in data["warnings"]), data["warnings"]
    assert "假设值" not in data["text"], data["text"]


def test_assess_without_provenance_attributes_does_not_break():
    """没有 `assumed_fields` 属性的对象（旧 fake / 别的调用方）不能被体检搞崩。"""

    class _Bare:
        asin = "B0BARE"
        current_price = 20.0
        break_even_price = 15.0

    a = bl.assess(_Bare())
    assert a.assumed_fields == []
    assert not any("假设值" in w for w in a.warnings)


# ============================================================
# ⑤ 防回退：面板拿不到就等于没做
# ============================================================


def test_my_to_dict_exposes_provenance(mock_mode):
    """`_my_to_dict` 必须带出 `source` 与 `assumed_fields`。

    没有这条断言，很容易出现"库里有了、面板没有"：
    检测逻辑全对，只是没人看得见——下次有人只看面板，照样把假设值当真值。
    """
    ctx = _ctx("dict")
    writes_cap.set_baseline(
        ctx,
        asin="B0PILOT008",
        current_price=24.99,
        break_even_price=17.5,
        source="sellersprite_proxy",
        assumed_fields=["break_even_price"],
    )

    out = pool.get_my_product(ctx.tenant_id)      # 面板走的就是这条
    assert out is not None
    assert "source" in out and "assumed_fields" in out, sorted(out)
    assert out["source"] == "sellersprite_proxy"
    assert out["assumed_fields"] == ["break_even_price"]
