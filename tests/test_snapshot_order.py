"""`Competitor.snapshots` 必须是有序的 —— 否则「最新快照」只是个猜测。

## 这组用例守的是什么

`Competitor.snapshots` 是一根**没有 order_by 的关系**，而
`app/services/scheduler.py:job_pool_maintenance()` 用 `comp.snapshots[-1]` 取
「最新快照」，再用它重算整个池子的 `tier` / `price_ratio` / `keyword_overlap`。

`[-1]` 拿到的是**数据库返回顺序**的最后一条，不是 `captured_at` 最新的那条。
今天它恰好等于正确结果，只因为每日采集是顺序插入、主键自增，插入顺序正好
等于时间顺序。一旦出现下面任一情况，这个「恰好对」就会静默失效：

- **补采历史**：发现某天漏采，事后补一条 `captured_at` 是过去的快照；
- **导入外部数据 / CSV 回填**；
- **修数据缺口**（同样的补写动作）。

失效后的表现是：周维护拿一条**过期快照**重算整池，把结论覆盖成旧数据算出的值。
全程无报错、无日志异常——这正是本项目最贵的一类 bug（「看起来是最新、实际不是」）。
同类问题已经发生过两次：`battlefield.my_asin` 被当绑定判据、`_effective_price`
拿标价顶替到手价。

所以这里的用例**故意让 `captured_at` 顺序与插入顺序不一致**。这样断言就不
可能因为「插入顺序恰好正确」而恒真——它只在关系真的按时间排序时才通过。

## 为什么第二个用例直接测周维护

关系排序只是手段。真正会被这条 bug 伤到的是 `job_pool_maintenance`：
它把「用哪条快照」的结果写回竞品身上，然后所有人（面板、日报、动作建议）
都以为那是当下的事实。所以第二个用例断言的是**写回值来自哪条快照**，
而不是「关系列表长什么样」。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters.unified import UnifiedSnapshot                      # noqa: E402
from app.config import settings                                       # noqa: E402
from app.db import get_session, init_db                               # noqa: E402
from app.models import Competitor, MyProduct                          # noqa: E402
from app.services import collector, scheduler                         # noqa: E402


# ============================================================
# 造数
# ============================================================


def _seed_pool(tenant: str, my_price: float = 50.0) -> tuple[int, int]:
    """一个自家锚点 + 一个竞品，返回 `(my_id, comp_id)`。

    锚点把三个维度都配齐（价格 / 核心词 / 类目路径），
    于是 `judge_tier` 的 `decidable_dims` 恒为 3，不会落进 `insufficient` 分支。
    这一点对第二个用例是必需的：`insufficient` 时周维护**一个字段都不写回**
    （刻意设计，见 `job_pool_maintenance` 里的注释），写不回来就没法断言
    「用的是哪条快照」。
    """
    init_db()
    with get_session() as s:
        my = MyProduct(
            tenant_id=tenant,
            asin="B0MINE0001",
            current_price=my_price,
            core_keywords={"toy car": 0.5, "diecast car": 0.3},
            category_path=["Toys & Games", "Toy Vehicles", "Play Trucks"],
        )
        s.add(my)
        s.flush()

        comp = Competitor(
            tenant_id=tenant,
            my_product_id=my.id,
            asin="B0COMP0001",
            status="active",
        )
        s.add(comp)
        s.flush()
        return my.id, comp.id


def _add_snapshot(comp_id: int, captured_at: datetime, net_price: float) -> int:
    """走真实写入路径（`collector._save_snapshot`）追加一条快照，返回 id。

    刻意用生产代码而不是裸 `Snapshot(...)`：这样 `keywords` / `category_path`
    也会按生产的规则落库，周维护里的 `row_to_unified` 才能还原出可判的维度。
    """
    with get_session() as s:
        row = collector._save_snapshot(
            s,
            comp_id,
            UnifiedSnapshot(
                asin="B0COMP0001",
                price=net_price,
                net_price=net_price,
                captured_at=captured_at,
                category_path=["Toys & Games", "Toy Vehicles", "Play Trucks"],
                keywords=[
                    {"keyword": "toy car", "traffic_share": 0.5},
                    {"keyword": "diecast car", "traffic_share": 0.3},
                ],
            ),
        )
        return row.id


def _today() -> datetime:
    """秒级精度的「今天」，避免微秒在 SQLite 往返时对不齐。"""
    return datetime.now().replace(microsecond=0)


# ============================================================
# ① 乱序插入 → `[-1]` 必须是最新那条
# ============================================================


def test_relationship_last_item_is_latest_despite_insert_order():
    """故意打乱插入顺序，`snapshots[-1]` 仍必须是 `captured_at` 最新的那条。

    插入顺序：今天 → 今天-3天 → 今天-1天。
    关系没有 order_by 时，`[-1]` 拿到的是**最后插入**的那条（今天-1天），
    于是下面第一句断言就会失败——这条用例正是为了「修之前会红」而写的。
    """
    tenant = "t-snaporder-latest"
    _, comp_id = _seed_pool(tenant)
    today = _today()

    _add_snapshot(comp_id, today, 60.0)
    _add_snapshot(comp_id, today - timedelta(days=3), 10.0)
    _add_snapshot(comp_id, today - timedelta(days=1), 100.0)

    with get_session() as s:
        comp = s.get(Competitor, comp_id)
        # 整列都要按 (captured_at, id) 升序，不只是最后一条。
        # 只断言 `[-1]` 会漏掉「顺序整体是乱的、末尾恰好对」这种更隐蔽的形态。
        assert [snap.captured_at for snap in comp.snapshots] == [
            today - timedelta(days=3),
            today - timedelta(days=1),
            today,
        ], [(snap.id, snap.captured_at, snap.net_price) for snap in comp.snapshots]
        assert comp.snapshots[-1].captured_at == today
        assert comp.snapshots[-1].net_price == pytest.approx(60.0)


def test_relationship_breaks_tie_by_id_when_captured_at_equal():
    """`captured_at` 相同（同一天重采）时，`[-1]` 必须是 `id` 较大的那条。

    只按 `captured_at` 排序的话，两条同秒快照的先后**不确定**——
    而「不确定」在周维护里的表现就是「同一份数据跑两次，结论不一样」，
    这种问题几乎不可能被复现和归因。
    """
    tenant = "t-snaporder-tie"
    _, comp_id = _seed_pool(tenant)
    today = _today()

    first_id = _add_snapshot(comp_id, today, 11.0)
    second_id = _add_snapshot(comp_id, today, 22.0)
    assert second_id > first_id, "前置条件：自增主键必须按插入顺序递增"

    with get_session() as s:
        comp = s.get(Competitor, comp_id)
        assert [snap.id for snap in comp.snapshots] == [first_id, second_id]
        assert comp.snapshots[-1].id == second_id
        assert comp.snapshots[-1].net_price == pytest.approx(22.0)


# ============================================================
# ② 周维护必须用最新快照重算（真正会受伤的地方）
# ============================================================


def test_pool_maintenance_uses_latest_snapshot():
    """`job_pool_maintenance` 写回的 `price_ratio` 必须按最新快照算出。

    三条快照的到手价差异故意拉大，让 `price_ratio` 落在不同区间：

    | captured_at | net_price | price_ratio = net_price / 50 |
    |-------------|-----------|------------------------------|
    | 今天        | 60.0      | 1.2  ← 价格带内，正确结论应来自这条 |
    | 今天-1天    | 100.0     | 2.0  ← 价格带外（这也是修复前会被误用的那条）|
    | 今天-3天    | 10.0      | 0.2  ← 价格带外 |

    断言的是「用了哪条快照」，所以写死的是**比值**而不是一个魔法数字：
    1.2 只有「今天」那条能算出来，2.0 是「今天-1天」那条（最后插入、修复前
    `[-1]` 会拿到它），0.2 是「今天-3天」那条。
    """
    # 周维护只处理 `settings.tenant_id` 这个租户，所以这里必须用同一个租户。
    # 本模块其余用例用独立租户，避免被这个任务的池子扫描带上。
    tenant = settings.tenant_id
    _, comp_id = _seed_pool(tenant, my_price=50.0)
    today = _today()

    _add_snapshot(comp_id, today, 60.0)                     # 最新：ratio 1.2
    _add_snapshot(comp_id, today - timedelta(days=3), 10.0)  # ratio 0.2
    _add_snapshot(comp_id, today - timedelta(days=1), 100.0)  # 最后插入：ratio 2.0

    before = _read_comp(comp_id)
    assert before.status == "active", "前置条件：只有 active 竞品会被重算"
    assert before.price_ratio is None, "前置条件：初始没有判定结果，写回值一定是本次算的"

    # 该函数是 async（调度器要 await 它）；测试里直接跑完一个事件循环。
    asyncio.run(scheduler.job_pool_maintenance())

    after = _read_comp(comp_id)
    assert after.price_ratio == pytest.approx(1.2, abs=1e-6), (
        f"周维护用了过期的快照：写回 price_ratio={after.price_ratio}。"
        f"1.2 来自今天那条（net_price=60 / 自家 50）；"
        f"2.0 说明它用了今天-1天那条（最后插入），"
        f"0.2 说明它用了今天-3天那条。"
    )


def _read_comp(comp_id: int) -> Competitor:
    """在新会话里读一遍竞品，避开身份映射缓存（拿到的是库里真实落下的值）。"""
    with get_session() as s:
        row = s.get(Competitor, comp_id)
        s.expunge(row)
        return row
