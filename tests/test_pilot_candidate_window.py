"""pilot 候选窗口：截断吃掉候选这件事，必须被钉住。

## 为什么单独一个文件

这不是「一个参数没配好」，是**同一类 bug 第三次出现**：

1. 采集时按 $18~$36 价格窗拉数据 → 低价段没被采样，报告里却说「池子里没有低价冲击品」；
2. `can_tier = has_price and has_kw` → 锚点没词就说「分层跑不了」，而库里竞品都分了层；
3. `--limit 20` 按 BSR 截断 → 补了低价段数据之后，池子从 17 掉到 **12**。

共同点：**采样窗口被读成了判定结论**。数据变多而结果变少，是这种 bug 最典型的外观——
正常系统不会这样。第 3 条就是本文件的由来。

## 钉的是哪两条

- `limit <= 0` 表示**不截断**（默认值），候选数随数据单调不减；
- `limit > 0` 时按 BSR 截断，且**截掉的确实是带内的好候选**——这条用反例固定，
  防止以后有人把默认值改回 20 还觉得「反正只差几个」。

导入顺序有讲究：`scripts.pilot_real` 在 import 期就会读 `sys.argv`、
必要时写 `os.environ["DATABASE_URL"]`（见 `_preset_database_url`）。
所以必须**先占位**一个临时库再 import，否则它会把环境变量指到 `data/pilot.db`。
这个套路和 `test_demo_isolation.py` 一样。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

# ---- 必须在 import scripts.pilot_real 之前占位 ----
_PLACEHOLDER_DB = Path(tempfile.mkdtemp(prefix="pilot_window_")) / "unit.db"
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_PLACEHOLDER_DB.as_posix()}")

import scripts.pilot_real as pilot  # noqa: E402

ANCHOR = "B0ANCHORX1"
ANCHOR_PRICE = 22.99


def _row(asin: str, price: float, bsr: int, sales: int = 1000, parent: str | None = None) -> dict:
    """造一条选品库返回行。字段名照抄真实返回，别改。"""
    return {
        "asin": asin,
        "parent_asin": parent or asin,
        "price": price,
        "bsr_rank": bsr,
        "monthly_sales": sales,
    }


def _anchor_row() -> dict:
    return _row(ANCHOR, ANCHOR_PRICE, 2440, 3345)


def _cheap_flood(n: int) -> list[dict]:
    """`n` 个便宜货，但 BSR 比真候选好——这正是当时把带内候选挤出去的那批。"""
    return [_row(f"CHEAP{i:04d}", 6.99, 100 + i) for i in range(n)]


def _in_band(n: int) -> list[dict]:
    """`n` 个价格比落在 0.7~1.4 带内、BSR 稍差的候选——真实竞品。"""
    return [_row(f"REAL{i:04d}", round(ANCHOR_PRICE * 0.9, 2), 5000 + i) for i in range(n)]


class TestLimitSemantics:
    def test_zero_means_no_truncation(self):
        items = [_anchor_row()] + _in_band(30)
        got = pilot._pick_candidates(items, ANCHOR, 0)
        assert len(got) == 30

    def test_negative_limit_also_means_no_truncation(self):
        """负数不该被当成「前 -5 个」——`picked[:-5]` 会静默砍掉尾巴。"""
        items = [_anchor_row()] + _in_band(30)
        assert len(pilot._pick_candidates(items, ANCHOR, -5)) == 30

    def test_positive_limit_truncates_to_exactly_that_many(self):
        items = [_anchor_row()] + _in_band(30)
        assert len(pilot._pick_candidates(items, ANCHOR, 7)) == 7

    def test_truncation_is_ordered_by_bsr_ascending(self):
        items = [
            _anchor_row(),
            _row("SLOW00001", 22.99, 9000),
            _row("FAST00001", 22.99, 300),
            _row("MID000001", 22.99, 4000),
        ]
        got = pilot._pick_candidates(items, ANCHOR, 2)
        assert [r["asin"] for r in got] == ["FAST00001", "MID000001"]


class TestTruncationCanEatRealCandidates:
    """回归用例：数据变多、池子变小的那个 bug。"""

    def test_cheap_high_bsr_rows_displace_in_band_candidates_under_limit(self):
        items = [_anchor_row()] + _cheap_flood(20) + _in_band(5)

        limited = pilot._pick_candidates(items, ANCHOR, 20)
        unlimited = pilot._pick_candidates(items, ANCHOR, 0)

        # 截到 20 时，20 个位置全被便宜货占满，5 个真候选一个都没进
        assert len(limited) == 20
        assert not any(r["asin"].startswith("REAL") for r in limited)

        # 不截断才看得见它们——这就是「没评估」和「不合格」的区别
        assert sum(1 for r in unlimited if r["asin"].startswith("REAL")) == 5
        assert len(unlimited) == 25

    def test_default_limit_is_unlimited(self):
        """默认值写死成 20 的话，本文件全部用例都该红。"""
        import inspect

        src = inspect.getsource(pilot.main)
        assert 'default=0' in src, "main() 的 --limit 默认值必须是 0（不限）"


class TestCandidateFiltersStillHold:
    """顺手确认没被这次改动碰坏的三条：锚点排除、销量门槛、BSR 缺失不放行。"""

    def test_anchor_and_its_variants_are_excluded(self):
        items = [
            _anchor_row(),
            _row("VARIANT01", 20.99, 100, parent=ANCHOR),
            _row("OTHER0001", 22.99, 200),
        ]
        got = pilot._pick_candidates(items, ANCHOR, 0)
        assert [r["asin"] for r in got] == ["OTHER0001"]

    def test_low_sales_rows_are_excluded(self):
        items = [_anchor_row(), _row("DEAD00001", 22.99, 200, sales=pilot.MIN_MONTHLY_SALES - 1)]
        assert pilot._pick_candidates(items, ANCHOR, 0) == []

    def test_missing_bsr_is_not_let_through(self):
        """`bsr_rank` 缺失说明这条数据本身残缺，不能拿它满足「有观察价值」。"""
        row = _row("NOBSR0001", 22.99, 200)
        row["bsr_rank"] = None
        assert pilot._pick_candidates([_anchor_row(), row], ANCHOR, 0) == []
