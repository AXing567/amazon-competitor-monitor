"""分层判断的两个缺失维度：竞品流量词（keywords）与类目路径（category_path）。

## 这组用例守的是什么

`app/domain/tiering.py` 的判据一直是对的（三个维度：价格带 / 流量池 / 节点），
但**数据进不去**：`snapshots` 表没有 `keywords` 列、`my_products` 没有
`category_path` 列，于是 `decidable_dims` 常年只有 1，整池被标成 `unknown`。

所以这里不测"算法对不对"，测的是**链路通不通**：
适配器拿到的词 → 落库 → 还原成 UnifiedSnapshot → 进得了 judge_tier → 维度可判。
这四步断任何一步，面板上就永远显示"待判定"，而没有任何地方会报错。

## 为什么用独立租户

`conftest.py` 已经给了本模块一份空库，但 `set_baseline` 是"租户唯一的自家锚点"，
多条用例共用一个租户会互相覆盖。每个用例自己造一个租户，才能独立断言。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters.unified import UnifiedSnapshot                      # noqa: E402
from app.capabilities import writes as writes_cap                     # noqa: E402
from app.capabilities.types import SOURCE_PANEL, CapContext           # noqa: E402
from app.db import get_session, init_db                               # noqa: E402
from app.domain.tiering import judge_tier                             # noqa: E402
from app.models import Competitor, MyProduct, Snapshot                # noqa: E402
from app.services import collector, pool                              # noqa: E402


def _ctx(tag: str) -> CapContext:
    return CapContext(tenant_id=f"t-dims-{tag}", user_id=1, source=SOURCE_PANEL)


def _seed_my(tenant: str, **fields) -> int:
    """造一个自家锚点，返回 id。"""
    init_db()
    base = {"asin": "B0MINE0001", "current_price": 29.99, "break_even_price": 18.0}
    base.update(fields)
    res = pool.update_baseline(tenant, **base)
    assert res.get("ok", True), res
    with get_session() as s:
        my = s.query(MyProduct).filter(MyProduct.tenant_id == tenant).one()
        return my.id


def _seed_competitor(tenant: str, my_id: int, asin: str = "B0COMP0001") -> int:
    with get_session() as s:
        comp = Competitor(tenant_id=tenant, asin=asin, my_product_id=my_id, status="active")
        s.add(comp)
        s.flush()
        return comp.id


# ============================================================
# ① 落库 → 还原：关键词和类目路径不能在路上丢掉
# ============================================================


def test_snapshot_keywords_and_category_survive_roundtrip():
    """写进 snapshots 的两列，必须能被 `row_to_unified` 原样还原。

    这条看着像常识，但它正是原来的断点：适配器明明取到了 keywords，
    `row_to_unified()` 却只能还原价格/排名/评论这些"有列"的字段，
    于是第二天重算分层时 `comp.keywords` 是空的——采了、没用上、还没人发现。
    """
    tenant = "t-dims-roundtrip"
    my_id = _seed_my(tenant)
    comp_id = _seed_competitor(tenant, my_id)

    snap = UnifiedSnapshot(
        asin="B0COMP0001",
        price=25.0,
        category_path=["Toys & Games", "Toy Vehicles", "Play Trucks"],
        keywords=[
            {"keyword": "toy car", "traffic_share": 0.42, "search_volume": 90000},
            {"keyword": "diecast car", "traffic_share": 0.31, "search_volume": 40000},
        ],
    )
    with get_session() as s:
        row = collector._save_snapshot(s, comp_id, snap)
        row_id = row.id

    with get_session() as s:
        row = s.get(Snapshot, row_id)
        back = collector.row_to_unified(row, "US")

    assert back.category_path == ["Toys & Games", "Toy Vehicles", "Play Trucks"]
    assert [k["keyword"] for k in back.keywords] == ["toy car", "diecast car"]
    # 加权词重合度的输入必须是可用的——存了但算不出权重，等于没存
    weights = back.core_keyword_weights(coverage=0.8)
    assert weights, "关键词还原后算不出权重，tiering 的流量池维度仍然是空的"
    assert weights["toy car"] == pytest.approx(0.42, abs=1e-6)


def test_empty_keywords_stay_empty_not_fabricated():
    """没采到就是空，不许补成"采到了但为空"的假数据。"""
    tenant = "t-dims-empty"
    my_id = _seed_my(tenant)
    comp_id = _seed_competitor(tenant, my_id, asin="B0COMP0002")

    snap = UnifiedSnapshot(asin="B0COMP0002", price=19.99)
    with get_session() as s:
        row_id = collector._save_snapshot(s, comp_id, snap).id

    with get_session() as s:
        raw = s.get(Snapshot, row_id)
        assert raw.keywords is None and raw.category_path is None
        back = collector.row_to_unified(raw, "US")
    assert back.keywords == [] and back.category_path == []


# ============================================================
# ② 三维齐了 → decidable_dims 必须是 3，insufficient 必须消失
# ============================================================


def test_three_dims_make_tier_decidable():
    """价格 + 流量词 + 类目路径都给齐 → `unknown` 不再出现。

    这是本轮改动的**目的**：原来 `decidable_dims` 恒 < 2，
    `effective_tier` 一律被压成 `unknown`，面板上一整列都是"待判定"。
    """
    comp = UnifiedSnapshot(
        asin="B0COMP0001",
        price=26.0,
        category_path=["Toys & Games", "Toy Vehicles", "Play Trucks"],
        keywords=[
            {"keyword": "toy car", "traffic_share": 0.5},
            {"keyword": "diecast car", "traffic_share": 0.3},
        ],
    )
    res = judge_tier(
        my_price=29.99,
        my_core_keywords={"toy car": 0.4, "diecast car": 0.35},
        my_category_path=["Toys & Games", "Toy Vehicles", "Play Trucks"],
        my_daily_units=15,
        comp=comp,
    )
    assert res.price_ratio is not None
    assert res.keyword_overlap is not None
    assert res.node_match is not None
    assert res.decidable_dims == 3, res.tier_reasons
    assert res.insufficient is False
    assert res.effective_tier != "unknown"


def test_missing_both_still_reports_insufficient():
    """只有价格 → 仍然 `unknown`。兜底分支不许因为这次补齐就被删掉。

    未来还会加维度，而"维度少到不足以判定"这件事永远可能发生。
    """
    comp = UnifiedSnapshot(asin="B0COMP0001", price=26.0)
    res = judge_tier(
        my_price=29.99,
        my_core_keywords={},
        my_category_path=None,
        my_daily_units=15,
        comp=comp,
    )
    assert res.decidable_dims == 1
    assert res.insufficient is True
    assert res.effective_tier == "unknown"


# ============================================================
# ③ 锚点能写进去（以前这两个字段根本没有写入口）
# ============================================================


def test_set_baseline_accepts_keywords_and_category_path():
    """`set_baseline` 必须能设置这两个字段。

    在此之前 `core_keywords` 连参数声明都没有——除了直接改库，
    没有任何途径让「自家核心出单词」有值。而它是流量池维度的另一半。
    """
    ctx = _ctx("baseline")
    res = writes_cap.set_baseline(
        ctx,
        asin="B0MINE0009",
        current_price=29.99,
        core_keywords={"toy car": 0.31, "diecast car": 22},   # 22 表示 22%
        category_path=["Toys & Games", "Toy Vehicles", "Play Trucks"],
    )
    assert res.get("ok") is True, res
    assert res["category_path"] == ["Toys & Games", "Toy Vehicles", "Play Trucks"]

    with get_session() as s:
        my = s.query(MyProduct).filter(MyProduct.tenant_id == ctx.tenant_id).one()
        # 22 被归一成 0.22：不归一的话权重放大 100 倍，词重合度直接失真
        assert my.core_keywords["diecast car"] == pytest.approx(0.22)
        assert my.core_keywords["toy car"] == pytest.approx(0.31)


def test_set_baseline_rejects_bad_shapes():
    """形状不对要当场拒，不能存进去之后让分层判断静默失灵。"""
    ctx = _ctx("badshape")
    bad_path = writes_cap.set_baseline(
        ctx, asin="B0MINE0008", category_path="Toys & Games > Toy Vehicles"
    )
    assert bad_path["ok"] is False and bad_path.get("hint")

    bad_kw = writes_cap.set_baseline(
        ctx, asin="B0MINE0008", core_keywords={"toy car": "很多"}
    )
    assert bad_kw["ok"] is False and bad_kw.get("hint")


# ============================================================
# ④ 老库迁移：三列要被补上，且重复跑不报错
# ============================================================


def test_migration_adds_new_columns_idempotently(tmp_path):
    """老库（没有这三列）跑一次迁移就能补上，第二次是空操作。"""
    import sqlite3

    from sqlalchemy import create_engine

    from app.migrations import migrate

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE snapshots (id INTEGER PRIMARY KEY, competitor_id INTEGER);
        CREATE TABLE my_products (id INTEGER PRIMARY KEY, tenant_id VARCHAR(64));
        """
    )
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{db.as_posix()}", future=True)
    first = migrate(engine)
    assert any("snapshots.keywords" in a for a in first), first
    assert any("snapshots.category_path" in a for a in first), first
    assert any("my_products.category_path" in a for a in first), first

    second = migrate(engine)
    assert second == [], f"迁移不幂等，第二次又动了：{second}"
