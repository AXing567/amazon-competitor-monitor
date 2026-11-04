"""上架日期必须活过落库：否则「A 级黑马」这条判据在生产路径上永远不命中。

## 这组用例守的是什么

`tiering._is_new_black_horse()` 靠 `comp.release_date` 判「上架 90 天内 + 月销过千
→ A」。而 `release_date` 的来源链是：

    卖家精灵 core.release_date  →  UnifiedSnapshot.release_date
      → （**断在这里**：snapshots 表没有这一列）→ 库
      →  row_to_unified()  →  tiering

采集当刻内存里的 `UnifiedSnapshot` 是有值的，所以「采完立刻判一次」看起来正常。
但只要**从库里重算**——周维护、面板改阈值后重算、`demo_offline._rescore`、
pilot 脚本——`release_date` 就是 `None`，黑马永不命中。
它不会报错，只会安静地把一个刚起量的新品从 A 掉到 B（暂定）。

这和「流量词不落库 = 每轮白采」是同一类错误：**数据可取、存不下、没人报错。**

## 为什么测「落库→还原→judge_tier」整条链而不是只测 `_is_new_black_horse`

那个纯函数一直是对的（tests/test_domain.py 里有它的用例）。坏的是**中间的存储**。
只测纯函数的话，这个 bug 一条用例都红不了——这正是它能活到今天的原因。
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters.unified import UnifiedSnapshot                      # noqa: E402
from app.db import get_session, init_db                               # noqa: E402
from app.domain.tiering import judge_tier                             # noqa: E402
from app.migrations import migrate                                    # noqa: E402
from app.models import Competitor, MyProduct, Snapshot                # noqa: E402
from app.services import collector                                    # noqa: E402

MY_PATH = ["Toys & Games", "Toy Vehicles", "Play Trucks"]
MY_PRICE = 29.99
TS = "t-reldate"


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _seed(tenant: str, asin: str = "B0HORSE001") -> int:
    """造一个自家锚点 + 一个同节点的竞品，返回 competitor_id。"""
    init_db()
    with get_session() as s:
        my = MyProduct(
            tenant_id=tenant,
            asin="B0MINE0001",
            marketplace="US",
            current_price=MY_PRICE,
            category_path=MY_PATH,
        )
        s.add(my)
        s.flush()
        comp = Competitor(
            tenant_id=tenant, my_product_id=my.id, asin=asin, status="active"
        )
        s.add(comp)
        s.flush()
        return comp.id


def _judge(comp: UnifiedSnapshot):
    """按生产路径的入参跑一遍分层：自家词有、竞品词没有、节点一致。"""
    return judge_tier(
        my_price=MY_PRICE,
        my_core_keywords={"toy car": 0.5, "diecast car": 0.5},
        my_category_path=MY_PATH,
        my_daily_units=None,
        comp=comp,
    )


# ============================================================
# ① 主用例：上架 30 天 + 月销 3000 的新品，重算之后必须还是 A
# ============================================================


def test_black_horse_tier_survives_db_roundtrip():
    """落库 → `row_to_unified` → `judge_tier`，30 天新上架的新品必须判成 A。

    改之前这里拿到的是 B：`release_date` 在 `_save_snapshot` 那一步就没写进库，
    `row_to_unified` 还原出来是 `None`，`_is_new_black_horse` 直接 False，
    于是 `_decide_letter_tier` 落到「词维度没测出来 → B（暂定）」那一支。
    """
    tenant = f"{TS}-roundtrip"
    comp_id = _seed(tenant)

    snap = UnifiedSnapshot(
        asin="B0HORSE001",
        price=26.0,
        monthly_units=3000,               # 月销过千
        release_date=_days_ago(30),       # 上架 90 天内
        category_path=MY_PATH,            # 与自家同节点
        data_quality="full",
        source="sellersprite",
    )
    with get_session() as s:
        row_id = collector._save_snapshot(s, comp_id, snap).id

    with get_session() as s:
        row = s.get(Snapshot, row_id)
        # 第一道：列存住了
        assert row.release_date == snap.release_date
        # 第二道：还原得回来
        back = collector.row_to_unified(row, "US")
    assert back.release_date == snap.release_date

    res = _judge(back)
    assert res.passed is True, res.reject_reason
    assert res.insufficient is False, res.tier_reasons
    assert res.tier == "A", (
        f"重算后判成 {res.tier}，黑马判据在生产路径上失效（release_date 没活过落库）："
        f"{res.tier_reasons}"
    )
    # A 是从「新品黑马」来的，不是从「中重度重合」来的：竞品没有流量词，
    # 词重合度必须是 None。否则这条用例会在别的分支上意外变绿，守不住回归。
    assert res.keyword_overlap is None, res.tier_reasons


# ============================================================
# ② 老库 / 无数据：NULL 不许炸，也不许变成空串
# ============================================================


def test_null_release_date_is_stored_as_null_not_empty_string():
    """没有上架日期就是 NULL。**不许**补成空串。

    空串和 NULL 在这里不是等价物：`_is_new_black_horse` 用 `if not comp.release_date`
    两者都拦得住，但库里留下空串之后，任何「这行有值吗」的判断都会说是——
    而它其实从来没采到过。
    """
    tenant = f"{TS}-null"
    comp_id = _seed(tenant, asin="B0HORSE002")

    snap = UnifiedSnapshot(asin="B0HORSE002", price=26.0, monthly_units=3000)
    with get_session() as s:
        row_id = collector._save_snapshot(s, comp_id, snap).id

    with get_session() as s:
        row = s.get(Snapshot, row_id)
        assert row.release_date is None
        back = collector.row_to_unified(row, "US")
    assert back.release_date is None


def test_unknown_release_date_falls_back_to_provisional_b():
    """上架日期未知 → 不当黑马，但分层照常出结果（不许抛异常、不许判死）。

    这条钉的是「老库列是 NULL」这一种真实情况：迁移只 ADD COLUMN，
    历史快照的 `release_date` 全是 NULL。老数据重算不能崩，
    也不该被当成「刚上架」——那是把「不知道」写成「知道」。
    """
    tenant = f"{TS}-unknown"
    comp_id = _seed(tenant, asin="B0HORSE003")

    snap = UnifiedSnapshot(
        asin="B0HORSE003",
        price=26.0,
        monthly_units=3000,               # 销量够，只差上架日期
        category_path=MY_PATH,
    )
    with get_session() as s:
        row_id = collector._save_snapshot(s, comp_id, snap).id

    with get_session() as s:
        back = collector.row_to_unified(s.get(Snapshot, row_id), "US")

    res = _judge(back)
    assert res.tier == "B"
    # B 是「词维度没测出来」时的中间档，必须带暂定标注，不能让面板读成
    # 「测过了、轻度重合」。
    assert res.provisional is True, res.tier_reasons
    assert any("流量词" in w for w in [res.provisional_reason])


def test_black_horse_needs_recent_date_and_volume():
    """纯函数的两侧边界：日期够新但销量不够、销量够但日期太老 → 都不是 A。"""
    from app.domain.tiering import _is_new_black_horse

    fresh_and_big = UnifiedSnapshot(
        asin="X", monthly_units=3000, release_date=_days_ago(30)
    )
    fresh_and_small = UnifiedSnapshot(
        asin="X", monthly_units=300, release_date=_days_ago(30)
    )
    old_and_big = UnifiedSnapshot(
        asin="X", monthly_units=3000, release_date=_days_ago(200)
    )
    assert _is_new_black_horse(fresh_and_big) is True
    assert _is_new_black_horse(fresh_and_small) is False
    assert _is_new_black_horse(old_and_big) is False


# ============================================================
# ③ 迁移：老库幂等补列
# ============================================================


def test_migration_adds_release_date_column_idempotently(tmp_path):
    db = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db.as_posix()}", future=True)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE snapshots (id INTEGER PRIMARY KEY, competitor_id INTEGER)"
        )

    first = migrate(engine)
    assert any("snapshots.release_date" in a for a in first), first

    # 服务每次启动都会跑一遍迁移，第二次必须是空操作
    second = migrate(engine)
    assert second == [], f"迁移不幂等，第二次又动了：{second}"

    with engine.connect() as conn:
        cols = {row[1]: row[2] for row in conn.exec_driver_sql("PRAGMA table_info(snapshots)")}
    assert "release_date" in cols, cols
    # 存 ISO 字符串而不是 DATE：源头给的就是字符串，tiering 只做 [:10] 解析
    assert cols["release_date"].upper().startswith("VARCHAR"), cols["release_date"]
    engine.dispose()
