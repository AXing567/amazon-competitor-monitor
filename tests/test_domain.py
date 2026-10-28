"""纯函数单元测试。

只测最值钱的三块：分层、决策、差异判定。
这三块是系统的大脑，出错会直接导致误报或错误建议，必须可回归。

跑法（不需要 pytest）：
    python tests/test_domain.py
"""
from __future__ import annotations

import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters.unified import UnifiedSnapshot                        # noqa: E402
from app.domain import baseline as bl                                  # noqa: E402
from app.domain.decision import (                                      # noqa: E402
    build_out_of_stock_decision,
    calc_break_even_units,
    judge_clearance,
    judge_price_follow,
    route_signal,
)
from app.domain.diff import DiffEngine, SignalDraft                    # noqa: E402
from app.domain.tiering import calc_keyword_overlap, calc_price_ratio, judge_tier  # noqa: E402


# ============================================================
# 分层
# ============================================================


def test_price_ratio_basic():
    assert calc_price_ratio(20.0, 25.0) == 1.25
    assert calc_price_ratio(20.0, 14.0) == 0.7
    # 缺数据必须返回 None，不能返回 0 或 1
    assert calc_price_ratio(None, 25.0) is None
    assert calc_price_ratio(20.0, None) is None
    assert calc_price_ratio(0, 25.0) is None


def test_keyword_overlap_is_weighted_not_jaccard():
    my_core = {"a": 50.0, "b": 30.0, "c": 20.0}
    comp = [{"keyword": "a"}, {"keyword": "b"}, {"keyword": "z"}, {"keyword": "y"}]
    # 命中 a+b = 80 / 100 = 0.8；若是 Jaccard 只有 2/5 = 0.4
    assert calc_keyword_overlap(my_core, comp) == 0.8

    # 只命中低权重词 c → 0.2，说明算法不会被边缘词糊弄
    comp2 = [{"keyword": "c"}, {"keyword": "x"}]
    assert calc_keyword_overlap(my_core, comp2) == 0.2


def test_tier_rejects_unrelated_competitor():
    comp = UnifiedSnapshot(
        asin="B0TEST0001",
        price=80.0,
        net_price=80.0,
        category_path=["Home", "Kitchen", "Blenders"],
        keywords=[{"keyword": "vacuum cleaner", "traffic_share": 60}],
        monthly_units=300,
    )
    res = judge_tier(
        my_price=20.0,
        my_core_keywords={"toy car": 70.0, "kids gift": 30.0},
        my_category_path=["Toys", "Vehicles", "Play Cars"],
        my_daily_units=30,
        comp=comp,
    )
    assert res.passed is False
    assert res.tier == "C"
    assert res.reject_reason


def test_tier_s_level_when_everything_matches():
    comp = UnifiedSnapshot(
        asin="B0TEST0002",
        price=20.5,
        net_price=20.5,
        category_path=["Toys", "Vehicles", "Play Cars"],
        keywords=[
            {"keyword": "toy car", "traffic_share": 70},
            {"keyword": "kids gift", "traffic_share": 20},
        ],
        monthly_units=900,
        daily_units=30,
    )
    res = judge_tier(
        my_price=20.0,
        my_core_keywords={"toy car": 70.0, "kids gift": 30.0},
        my_category_path=["Toys", "Vehicles", "Play Cars"],
        my_daily_units=30,
        comp=comp,
    )
    assert res.passed is True
    assert res.tier == "S", res.tier_reasons


def test_tier_new_black_horse_is_a():
    recent = (date.today() - timedelta(days=20)).strftime("%Y-%m-%d")
    comp = UnifiedSnapshot(
        asin="B0TEST0003",
        price=21.0,
        net_price=21.0,
        category_path=["Toys", "Vehicles", "Play Cars"],
        keywords=[{"keyword": "toy car", "traffic_share": 50}],
        monthly_units=2000,
        release_date=recent,
    )
    res = judge_tier(
        my_price=20.0,
        my_core_keywords={"toy car": 50.0, "kids gift": 50.0},
        my_category_path=["Toys", "Vehicles", "Play Cars"],
        my_daily_units=30,
        comp=comp,
    )
    assert res.passed is True
    assert res.tier == "A"


# ============================================================
# 决策
# ============================================================


def test_break_even_units():
    r = calc_break_even_units(
        current_price=20.0,
        current_daily_units=50,
        unit_profit_now=5.0,     # 日利润 250
        new_price=18.0,
        unit_cost=15.0,          # 新单均利润 3.0
    )
    assert r["base_daily_profit"] == 250.0
    assert r["new_unit_profit"] == 3.0
    assert r["break_even_units"] == 83.3
    assert r["required_growth_pct"] == 66.7


def test_break_even_rejects_loss():
    r = calc_break_even_units(
        current_price=20.0,
        current_daily_units=50,
        unit_profit_now=5.0,
        new_price=14.0,
        unit_cost=15.0,
    )
    assert r["viable"] is False
    assert r["break_even_units"] is None


def test_no_follow_when_below_break_even():
    res = judge_price_follow(
        competitor_new_price=12.0,
        my_current_price=20.0,
        my_break_even_price=16.0,
        my_unit_profit_now=5.0,
        my_daily_units=50,
        my_dos=40,
    )
    assert res.decision == "no_action"
    assert "跌破" in res.reasons[0] or "保本" in res.reasons[0]
    assert res.actions  # 必须给替代动作


def test_no_follow_when_stock_low():
    res = judge_price_follow(
        competitor_new_price=18.0,
        my_current_price=20.0,
        my_break_even_price=15.0,
        my_unit_profit_now=5.0,
        my_daily_units=50,
        my_dos=12,
    )
    assert res.decision == "no_action"
    assert "库存" in res.headline or "撑不住" in res.headline


def test_no_follow_when_clearance():
    res = judge_price_follow(
        competitor_new_price=16.0,
        my_current_price=20.0,
        my_break_even_price=15.0,
        my_unit_profit_now=5.0,
        my_daily_units=50,
        competitor_is_clearance=True,
        my_dos=40,
    )
    assert res.decision == "no_action"
    assert res.metrics.get("strategy") == "wait_for_rebound"


def test_can_follow_when_all_gates_pass():
    res = judge_price_follow(
        competitor_new_price=19.0,
        my_current_price=20.0,
        my_break_even_price=15.0,
        my_unit_profit_now=5.0,
        my_daily_units=20,
        market_players=2,
        my_dos=45,
    )
    assert res.decision == "attack"
    assert res.metrics["break_even"]["break_even_units"] is not None


def test_clearance_needs_stock_decline_not_just_price():
    # 只降价、库存没动 → 不够确认
    res = judge_clearance(price_drop_pct=25, stock_declining=False)
    assert res.metrics["clearance_score"] < 50

    # 降价 + 库存单边下降 + 不补货 → 定性清仓
    res2 = judge_clearance(price_drop_pct=25, stock_declining=True, no_restock=True)
    assert res2.metrics["clearance_score"] >= 50
    assert "清仓" in res2.headline


def test_clearance_seasonal_short_circuit():
    res = judge_clearance(historical_seasonal=True)
    assert res.confidence == 0.95
    assert "季节" in res.headline


def test_out_of_stock_with_low_dos_warns_against_price_cut():
    res = build_out_of_stock_decision(competitor_asin="B0X", my_dos=10)
    assert res.decision == "attack"
    assert any("不能降价" in a or "不要改标价" in a for a in res.actions)


def test_signal_routing():
    assert route_signal("price_drop").startswith("主线1")
    assert route_signal("out_of_stock").startswith("主线1")
    assert route_signal("rating_drop").startswith("主线4")
    assert route_signal("unknown_type") == "归档"


# ============================================================
# 优先级：断货 > 降价
# ============================================================


def test_oos_overrides_price_signal():
    """对手既降价又断货时，绝不能建议跟价。

    他没货可卖，降价成交不了；这时候跟价只是把自己的利润也降掉。
    这是最容易出、也最伤钱的一类错误建议。
    """
    from types import SimpleNamespace

    from app.services.briefer import _attach_action

    my = SimpleNamespace(
        current_price=29.99,
        break_even_price=22.49,
        daily_sales_avg=40.0,
        target_margin_rate=0.25,
        fba_available_units=800,
        lead_time_days=35,
        asin="B0MINE",
        sub_category_rank=None,
        rating=None,
        review_count=None,
        core_keywords=None,
        title=None,
        image_url=None,
        bullet_points=None,
    )
    comp = SimpleNamespace(id=7, asin="B0X", tier="S", title="demo")
    sig = SimpleNamespace(
        signal_type="price_drop",
        after_value="26.49",
        level="P0",
        description="净价 29.99 → 26.49，降 11.7%",
    )

    # 没有断货 → 走正常跟价判断
    from app.services.briefer import BriefItem

    item_alone = BriefItem(asin="B0X", tier="S", title="demo", signal_type="price_drop",
                           level="P0", description="降价")
    _attach_action(item_alone, sig, comp, my, oos_comps=set())
    assert item_alone.decision in ("attack", "no_action")
    assert "断货" not in item_alone.headline

    # 同批次有断货 → 价格信号让路
    item_oos = BriefItem(asin="B0X", tier="S", title="demo", signal_type="price_drop",
                         level="P0", description="降价")
    _attach_action(item_oos, sig, comp, my, oos_comps={7})
    assert item_oos.decision == "attack"
    assert "断货" in item_oos.headline
    assert any("不要跟价" in a for a in item_oos.actions)


# ============================================================
# 差异判定
# ============================================================


def _snap(**kw) -> UnifiedSnapshot:
    base = dict(asin="B0TEST", price=20.0, net_price=20.0, rank_sub=1000,
                rating=4.5, review_count=500, in_stock=True, stock_hint="IN_STOCK",
                title="Toy Car", image_url="http://img/1.jpg")
    base.update(kw)
    return UnifiedSnapshot(**base)


def test_price_drop_beyond_3pct_is_p0():
    sigs = DiffEngine().compare(_snap(), _snap(net_price=19.0, price=19.0))
    price = [s for s in sigs if s.signal_type == "price_drop"]
    assert price and price[0].level == "P0"
    assert price[0].change_pct == -5.0


def test_price_small_change_is_noise():
    # 0.3% 的波动必须被当噪声丢掉，否则每轮采集都会误报
    sigs = DiffEngine().compare(_snap(), _snap(net_price=19.94, price=19.94))
    assert not [s for s in sigs if s.category == "price"]


def test_coupon_increase_is_p0():
    prev = _snap(coupon_value=1.0)
    curr = _snap(coupon_value=3.0)
    sigs = DiffEngine().compare(prev, curr)
    assert any(s.signal_type == "coupon_increase" and s.level == "P0" for s in sigs)


def test_out_of_stock_always_p0():
    """断货一律 P0，不等二次确认。

    代价不对称：误报一次只是多花点广告费，漏报一次错过整个真空窗。
    oos_confirmed 只影响措辞，不影响等级。
    """
    prev = _snap()
    curr = _snap(in_stock=False, stock_hint="OOS")

    sigs = DiffEngine().compare(prev, curr, oos_confirmed=True)
    oos = [s for s in sigs if s.signal_type == "out_of_stock"]
    assert oos and oos[0].level == "P0"
    assert "确认" in oos[0].description

    sigs2 = DiffEngine().compare(prev, curr, oos_confirmed=False)
    oos2 = [s for s in sigs2 if s.signal_type == "out_of_stock"]
    assert oos2 and oos2[0].level == "P0"
    assert "首次发现" in oos2[0].description


def test_rank_small_move_ignored():
    # 1000 → 1010 只动了 10 名，低于 20 名门槛，不报
    sigs = DiffEngine().compare(_snap(rank_sub=1000), _snap(rank_sub=1010))
    assert not [s for s in sigs if s.category == "rank"]


def test_rank_crash_reported():
    sigs = DiffEngine().compare(_snap(rank_sub=1000), _snap(rank_sub=1600))
    rank = [s for s in sigs if s.category == "rank"]
    assert rank and rank[0].level == "P0"


def test_review_surge_p0():
    sigs = DiffEngine().compare(_snap(review_count=500), _snap(review_count=580))
    assert any(s.signal_type == "review_surge" and s.level == "P0" for s in sigs)


def test_rating_below_floor_p0():
    sigs = DiffEngine().compare(_snap(rating=4.2), _snap(rating=3.9))
    assert any(s.signal_type == "rating_below_floor" and s.level == "P0" for s in sigs)


def test_price_band_break_alert():
    # 自家 20，对手从 20 掉到 13 → 跌破 0.7 倍，战场位移
    sigs = DiffEngine().compare(
        _snap(net_price=20.0), _snap(net_price=13.0), my_price=20.0
    )
    assert any(s.signal_type == "price_band_break" for s in sigs)


def test_blast_phase_tightens_threshold():
    # 旺季阈值 3% * 0.8 = 2.4%，2.5% 的降价在旺季算 P0，淡季只算 P1
    prev = _snap(net_price=20.0)
    curr = _snap(net_price=19.5)   # -2.5%
    winter = [s for s in DiffEngine("blast").compare(prev, curr) if s.category == "price"]
    normal = [s for s in DiffEngine("warmup").compare(prev, curr) if s.category == "price"]
    assert winter and winter[0].level == "P0"
    assert normal and normal[0].level == "P1"


def test_push_gate():
    from app.domain.diff import should_push

    p2_only = [SignalDraft("content", "title_changed", "P2", "改标题")]
    assert should_push(p2_only, "daily") is False
    assert should_push(p2_only, "all") is True

    has_p0 = p2_only + [SignalDraft("price", "price_drop", "P0", "降价")]
    assert should_push(has_p0, "p0_only") is True


# ============================================================
# 锚点体检
# ============================================================


class _FakeMy:
    asin = "B0MINE"
    current_price = 20.0
    break_even_price = None
    target_margin_rate = 0.25
    fba_available_units = None
    daily_sales_avg = None
    lead_time_days = None
    sub_category_rank = None
    rating = None
    review_count = None
    core_keywords = None
    title = None
    image_url = None
    bullet_points = None


def test_anchor_detects_missing_critical_fields():
    a = bl.assess(_FakeMy())
    assert a.can_tier is False          # 缺核心词
    assert a.can_judge_price_follow is True   # 有毛利率可推保本价
    assert a.can_judge_attack_window is False  # 缺库存与日均销量
    assert a.days_of_supply is None
    assert any("库存" in w for w in a.warnings)


def test_estimate_break_even_price():
    assert bl.estimate_break_even_price(price=20.0, margin_rate=0.25) == 15.0
    assert bl.estimate_break_even_price(price=20.0, margin_rate=1.5) is None
    assert bl.estimate_break_even_price(price=0, margin_rate=0.25) is None


# ============================================================
# 运行器
# ============================================================


def main() -> int:
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    failed: list[tuple[str, str]] = []

    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  ✅ {name}")
        except Exception:  # noqa: BLE001
            failed.append((name, traceback.format_exc()))
            print(f"  ❌ {name}")

    print("")
    print(f"通过 {passed}/{len(tests)}")
    if failed:
        print("")
        for name, tb in failed:
            print(f"--- {name} ---")
            print(tb)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
