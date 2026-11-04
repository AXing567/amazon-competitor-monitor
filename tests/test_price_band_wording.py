"""价格带文案：数字不许和自己的结论打架。

## 这组用例守的是什么

真实数据第一次跑就顶出来 4 条这样的行（全是 $15.99 的竞品）：

    价格比 0.70 超出 0.7~1.4 价格带

`15.99 / 22.99 = 0.6955`，两位小数四舍五入成 `0.70`，正好等于下限。
于是同一句话里「0.70」既是被判的值、又是门槛——读者只能理解成系统坏了。

这类错误比缺数据更贵：缺数据时人知道要等，**自相矛盾的文案会让人不再相信整张表**。
同一处代码的 docstring 里早就写着「写死一个 40% 的后果……一句自相矛盾的废话，
比没有理由更糟」——结果精度这一头又踩了一次。

## 怎么守

不测死具体字符串，测**性质**：把文案里的数字重新解析出来，它必须真的落在
结论所说的那一侧。这样以后改措辞、改精度都不会误报，但一旦又开始自相矛盾就会红。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters.unified import UnifiedSnapshot          # noqa: E402
from app.domain.tiering import (                          # noqa: E402
    fmt_ratio_to_show_side,
    judge_tier,
)
from app.domain.thresholds import TieringThresholds       # noqa: E402
from app.services.binding import build_basis_summary      # noqa: E402

#: 锚点价。$15.99 / 22.99 = 0.6955——就是那条把坑踩出来的真实数据。
MY_PRICE = 22.99
EDGE_PRICE = 15.99

RATIO_RE = re.compile(r"(\d+\.\d+)")


def _shown_ratio(text: str) -> float:
    """从文案里把「价格比 X」的数字抠出来——测的就是**读者看到的那个数**。"""
    m = RATIO_RE.search(text)
    assert m, f"文案里没有可解析的数字：{text!r}"
    return float(m.group(1))


class TestFmtRatioToShowSide:
    def test_edge_case_gains_precision_until_direction_is_visible(self):
        """0.6955 用两位小数是 0.70，与下限相等 → 必须自动加到三位。"""
        assert fmt_ratio_to_show_side(15.99 / MY_PRICE, 0.7, 1.4) == "0.696"

    def test_normal_case_keeps_two_decimals(self):
        """绝大多数值两位就够，不要为了边界值把每条都写成 0.69552。"""
        assert fmt_ratio_to_show_side(1.52, 0.7, 1.4) == "1.52"

    def test_in_band_value_keeps_two_decimals(self):
        """带内的值没有方向要说，不许被上面那套逻辑撑成 0.8700。"""
        assert fmt_ratio_to_show_side(0.87, 0.7, 1.4) == "0.87"
        assert fmt_ratio_to_show_side(0.70, 0.7, 1.4) == "0.70"
        assert fmt_ratio_to_show_side(1.40, 0.7, 1.4) == "1.40"

    def test_very_close_to_boundary_still_resolves(self):
        """16.09 / 22.99 = 0.69987，三位还是 0.700，得再加一位。"""
        shown = fmt_ratio_to_show_side(16.09 / MY_PRICE, 0.7, 1.4)
        assert float(shown) < 0.7

    def test_above_upper_bound_also_resolves(self):
        shown = fmt_ratio_to_show_side(22.99 * 1.4001, 0.7, 1.4)
        assert float(shown) > 1.4


class TestRejectReasonNeverContradictsItself:
    def _judge_at(self, price: float):
        """不带流量词——真实数据里常见（采集可能失败，见 test_provisional_tier）。

        为什么必须去掉词：门槛是「流量池 **或**（节点 **且** 价格带）」二选一，
        词重合够高时价格带出界也能保送（见 `judge_tier` 第 175 行）。
        那样根本进不到拒收分支，也就测不到这句文案。
        """
        return judge_tier(
            my_price=MY_PRICE,
            my_core_keywords={"dish drying rack": 0.8},
            my_category_path=["Home & Kitchen", "Kitchen & Dining"],
            my_daily_units=15,
            comp=UnifiedSnapshot(
                asin="B0EDGE0001",
                price=price,
                keywords=[],
                category_path=["Home & Kitchen", "Kitchen & Dining"],
            ),
        )

    def test_edge_reject_reason_number_is_below_lower_bound(self):
        res = self._judge_at(EDGE_PRICE)
        assert res.price_ratio is not None and res.price_ratio < 0.7
        assert res.tier in ("C", "unknown")
        shown = _shown_ratio(res.reject_reason or "")
        assert shown < 0.7, (
            f"文案说它低于下限，但显示的数字是 {shown}，读者会认为门槛算错了："
            f"{res.reject_reason!r}"
        )
        assert "低于下限" in (res.reject_reason or "")

    def test_reject_reason_states_direction_for_expensive_listing(self):
        res = self._judge_at(MY_PRICE * 1.76)
        reason = res.reject_reason or ""
        assert "高于上限" in reason
        assert _shown_ratio(reason) > 1.4


class TestBasisSummaryNeverContradictsItself:
    """`basis_summary` 是面板列表里老板唯一会读的那句话。"""

    def test_edge_competitor_summary_shows_two_sided_truth(self):
        th = TieringThresholds()
        ratio = EDGE_PRICE / MY_PRICE
        summary = build_basis_summary(
            {"price_ratio": ratio, "price_band_ok": False, "keyword_overlap": None},
            tier="C",
            thresholds=th,
        )
        assert _shown_ratio(summary) < th.price_ratio_min, (
            f"摘要显示的价格比落在带内，却又说它带外：{summary!r}"
        )
        assert "低于下限" in summary

    def test_in_band_summary_still_uses_two_decimals(self):
        th = TieringThresholds()
        summary = build_basis_summary(
            {"price_ratio": 0.87, "price_band_ok": True, "keyword_overlap": None},
            tier="B",
            thresholds=th,
        )
        assert "0.87 在 0.7~1.4 带内" in summary
