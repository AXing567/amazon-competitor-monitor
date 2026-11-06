"""复盘闭环：动作记下来之后，7 天后真的有人回看。

## 这组测试守的是什么

「动作 → 复盘」这条链有三处会**安静地断掉**，每一处都不报错：

1. `record()` 不抓 `baseline_metrics` → 到期时没有基准，前后对比无从谈起，
   只能靠人回忆「当时他多少钱、我当时多少单」。这一列在建表时就存在，
   但一直写的是 `None`；
2. `verify_due_at` 到期后没有任何查询去取 → 承诺了要回看，然后什么都不发生；
3. 复盘只存了「有效/无效」，没存「当时的数」→ 半年后无法验证那个结论对不对。

三处都不会抛异常。它们只会让「我们复盘过」这句话变成一句空话。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.db import get_session
from app.models import Action, Competitor, Decision, MyProduct, Signal, Snapshot
from app.services import action_log, briefer


# ============================================================
# 现场
# ============================================================


@pytest.fixture()
def scene(clean_db):
    """自家锚点 + 竞品 + 一条两天的快照历史。"""
    now = datetime.now()
    with get_session() as s:
        my = MyProduct(
            tenant_id="default",
            asin="B0MINE0001",
            platform="amazon",
            marketplace="US",
            current_price=24.99,
            break_even_price=13.20,
            daily_sales_avg=85.0,
            target_margin_rate=0.28,
            fba_available_units=2600,
        )
        s.add(my)
        s.flush()
        comp = Competitor(
            tenant_id="default",
            my_product_id=my.id,
            asin="B0CMP00001",
            platform="amazon",
            marketplace="US",
            title="测试竞品",
            tier="S",
            status="active",
        )
        s.add(comp)
        s.flush()
        # 两条快照：昨天 24.99，今天 24.99（动作就发生在这一片平静里）
        for days_ago in (1, 0):
            s.add(
                Snapshot(
                    competitor_id=comp.id,
                    captured_at=now - timedelta(days=days_ago),
                    price=24.99,
                    net_price=24.99,
                    rank_main=420,
                    rating=4.7,
                    review_count=4820,
                    in_stock=True,
                    data_quality="full",
                )
            )
        sig = Signal(
            tenant_id="default",
            competitor_id=comp.id,
            category="price",
            signal_type="price_drop",
            level="P0",
            after_value="24.99",
            description="降价：$26.99 → $24.99",
            detected_at=now - timedelta(hours=2),
        )
        s.add(sig)
        s.flush()
        return {"my_id": my.id, "comp_id": comp.id, "signal_id": sig.id}


def _set_due_in_past(action_id: int, days: int = 1) -> None:
    """把验证日期拨到过去，模拟「到期了」。"""
    with get_session() as s:
        row = s.get(Action, action_id)
        row.verify_due_at = datetime.now() - timedelta(days=days)


# ============================================================
# 记动作：抓基线
# ============================================================


def test_record_captures_baseline_metrics(scene):
    """记动作那一刻必须把指标抓下来——这是复盘的唯一基准。"""
    rec = action_log.record(
        "default",
        action_type="price_hold",
        competitor_id=scene["comp_id"],
        my_product_id=scene["my_id"],
        description="不跟价",
        verify_days=7,
    )
    assert rec["ok"], rec
    assert rec["baseline_available"] is True

    with get_session() as s:
        row = s.get(Action, rec["action_id"])
        base = row.baseline_metrics or {}

    assert base.get("at"), "没记抓取时间，就分不清这份基线是新是旧"
    assert base["competitor"]["price"] == 24.99
    assert base["competitor"]["rank_main"] == 420
    assert base["mine"]["daily_sales_avg"] == 85.0
    # 自家锚点的更新时间要单独留着：复盘时用它判断「我的数没变」是不是因为没人更新
    assert base.get("mine_updated_at")


def test_record_links_decision(scene):
    """带 signal_id 记动作 → 自动挂上当时的判断。

    挂不上就永远答不了「你当时凭什么这么做」——而那正是复盘要问的第二句话
    （第一句是「后来有效吗」）。
    """
    briefer.build_daily_brief("default", hours=24)
    rec = action_log.record(
        "default",
        action_type="price_hold",
        competitor_id=scene["comp_id"],
        my_product_id=scene["my_id"],
        signal_id=scene["signal_id"],
        verify_days=7,
    )
    assert rec["ok"], rec
    assert rec["decision_id"] is not None
    detail = action_log.list_actions("default", days=1)
    assert detail[0]["decision_id"] == rec["decision_id"]


def test_baseline_available_false_when_never_collected(clean_db):
    """竞品一次都没采过 → 要明确说「基线没抓到」，而不是给一个空字典。

    空字典在复盘表上显示成「无变化」，于是「没采集」被读成「动作无效」。
    """
    with get_session() as s:
        my = MyProduct(tenant_id="default", asin="B0MINE0001", platform="amazon",
                       marketplace="US", current_price=24.99)
        s.add(my)
        s.flush()
        comp = Competitor(tenant_id="default", my_product_id=my.id, asin="B0NOCOLL1",
                          platform="amazon", marketplace="US", tier="B", status="active")
        s.add(comp)
        s.flush()
        my_id, cid = my.id, comp.id

    rec = action_log.record(
        "default", action_type="price_hold", competitor_id=cid,
        my_product_id=my_id, verify_days=7,
    )
    assert rec["ok"], rec
    # `comparable` 而不是 `baseline_available`：后者只问「抓到东西没」，
    # 而锚点在手时它永远是 True——哪怕竞品一次都没采过、到期根本无从对比。
    assert rec["comparable"] is False


# ============================================================
# 到期队列
# ============================================================


def test_due_reviews_only_returns_overdue_unreviewed(scene):
    """队列只收「到期 + 还没判」的。三类别的东西都必须被排除。"""
    base = dict(
        competitor_id=scene["comp_id"], my_product_id=scene["my_id"],
        user_id=None, source="panel",
    )
    due_id = action_log.record("default", action_type="price_hold", verify_days=7, **base)["action_id"]
    future_id = action_log.record("default", action_type="ad_shift", verify_days=7, **base)["action_id"]
    nodate_id = action_log.record("default", action_type="stock_alert", **base)["action_id"]
    done_id = action_log.record("default", action_type="price_follow", verify_days=7, **base)["action_id"]

    _set_due_in_past(due_id, 3)
    _set_due_in_past(done_id, 5)
    action_log.review_action("default", action_id=done_id, verdict="effective")
    # future_id 保持未来的验证日期

    got = {it["id"] for it in action_log.due_reviews("default")}
    assert due_id in got, "到期未复盘的没进队列——这条链就是断的"
    assert future_id not in got, "没到期的进了队列，队列会变成一堆假待办"
    assert nodate_id not in got, "没承诺回看的动作不该来打扰人"
    assert done_id not in got, "已经判过的还在队列里"


def test_due_reviews_reports_overdue_days(scene):
    """超期天数要算出来。0 天（今天到期）和 14 天该有不同紧迫感。"""
    aid = action_log.record(
        "default", action_type="price_hold", competitor_id=scene["comp_id"],
        my_product_id=scene["my_id"], verify_days=7,
    )["action_id"]
    _set_due_in_past(aid, 3)
    item = action_log.due_reviews("default")[0]
    assert item["overdue_days"] == 3


def test_due_reviews_includes_comparison(scene):
    """到期队列必须带前后对比——只给「该复盘 #12 了」人是不会动的。"""
    aid = action_log.record(
        "default", action_type="price_hold", competitor_id=scene["comp_id"],
        my_product_id=scene["my_id"], verify_days=7,
    )["action_id"]
    _set_due_in_past(aid, 3)

    # 之后对手又降了一次（多一条快照），前后才有差
    with get_session() as s:
        s.add(
            Snapshot(
                competitor_id=scene["comp_id"],
                captured_at=datetime.now() + timedelta(hours=1),
                price=22.49,
                net_price=22.49,
                rank_main=380,
                rating=4.7,
                review_count=4820,
                in_stock=True,
                data_quality="full",
            )
        )

    item = action_log.due_reviews("default")[0]
    cmp = item["comparison"]
    assert cmp["state"] == action_log.STATE_CHANGED
    price_change = next(c for c in cmp["changes"] if c["key"] == "price")
    assert price_change["before"] == 24.99
    assert price_change["after"] == 22.49
    assert price_change["kind"] == "money"
    # 差值必须是**数字**，不是 "$-2.50"：格式化归展示层，
    # 后端拼了符号，面板再 money() 一次就是 "$$-2.50"。
    assert price_change["delta"] == pytest.approx(-2.5, abs=1e-6)


# ============================================================
# 三种状态不能混
# ============================================================


def test_comparison_states_are_distinct(scene):
    """`unchanged`（有数但没变）与 `no_data`（根本没数）必须分开。

    合并成一句「无变化」的后果很具体：一次漏采会被记成一次失败的动作，
    然后那条错误经验被写进下次的策略里。
    """
    before = {"at": "t0", "competitor": {"price": 24.99}, "mine": {"daily_sales_avg": 85.0}}
    same = {"at": "t1", "competitor": {"price": 24.99}, "mine": {"daily_sales_avg": 85.0}}
    moved = {"at": "t1", "competitor": {"price": 22.99}, "mine": {"daily_sales_avg": 85.0}}
    empty = {"at": "t1", "competitor": {}, "mine": {}}

    assert action_log.compare_metrics(before, same)["state"] == action_log.STATE_UNCHANGED
    assert action_log.compare_metrics(before, moved)["state"] == action_log.STATE_CHANGED
    assert action_log.compare_metrics(before, empty)["state"] == action_log.STATE_NO_DATA
    assert action_log.compare_metrics(None, moved)["state"] == action_log.STATE_NO_DATA


def test_comparison_flags_stale_anchor(scene):
    """锚点自动作以来没更新过 → 自家那半边的数不能当证据。"""
    aid = action_log.record(
        "default", action_type="price_hold", competitor_id=scene["comp_id"],
        my_product_id=scene["my_id"], verify_days=7,
    )["action_id"]
    _set_due_in_past(aid, 3)
    item = action_log.due_reviews("default")[0]
    # 造数里没人改过锚点，所以 mine 的 updated_at 前后一致
    assert item["comparison"]["mine_stale"] is True


# ============================================================
# 判定
# ============================================================


def test_review_rejects_unknown_verdict(scene):
    """非法结论要被拒绝，且 hint 说清三个值各是什么意思。

    允许任意字符串的话，库里会出现「有效 / 有用 / 还行 / 差不多」四种写法，
    半年后没法按结论筛。
    """
    aid = action_log.record(
        "default", action_type="price_hold", competitor_id=scene["comp_id"],
        my_product_id=scene["my_id"], verify_days=7,
    )["action_id"]
    res = action_log.review_action("default", action_id=aid, verdict="还行")
    assert res["ok"] is False
    # hint 要给出三个合法值各自的意思，而不只是"你传错了"。
    # 只说错了人只会再试一个同义词（"有用"），然后第三次失败。
    assert "有效" in res["hint"] and "判不了" in res["hint"]


def test_review_records_result_and_note(scene):
    """复盘要留下：结果指标、结论、备注、复盘时间。

    `result_metrics` 与 `baseline_metrics` **同结构**才能直接相减；
    结构不一致的话，将来想做「批量统计动作效果」要写一堆兼容代码。
    """
    aid = action_log.record(
        "default", action_type="price_hold", competitor_id=scene["comp_id"],
        my_product_id=scene["my_id"], verify_days=7,
    )["action_id"]
    _set_due_in_past(aid, 3)

    with get_session() as s:
        s.add(
            Snapshot(
                competitor_id=scene["comp_id"],
                captured_at=datetime.now() + timedelta(hours=1),
                price=22.49, net_price=22.49, rank_main=380,
                rating=4.7, review_count=4820, in_stock=True, data_quality="full",
            )
        )

    res = action_log.review_action(
        "default", action_id=aid, verdict="ineffective",
        note="跟了价但 BSR 没起色，对手同时在清仓",
    )
    assert res["ok"], res
    assert res["verdict"] == "ineffective"
    assert res["verdict_label"] == "无效"
    assert res["reviewed_at"]
    assert res["comparison"]["state"] == action_log.STATE_CHANGED

    with get_session() as s:
        row = s.get(Action, aid)
        assert row.verdict == "ineffective"
        assert row.review_note.startswith("跟了价")
        assert row.result_metrics["competitor"]["price"] == 22.49
        # 同结构：两侧都有 at / competitor / mine
        assert set(row.result_metrics) == set(row.baseline_metrics)


def test_review_removes_from_queue(scene):
    """判定之后立刻从队列消失。不消失的话，人会重复判同一件事。"""
    aid = action_log.record(
        "default", action_type="price_hold", competitor_id=scene["comp_id"],
        my_product_id=scene["my_id"], verify_days=7,
    )["action_id"]
    _set_due_in_past(aid, 3)
    assert len(action_log.due_reviews("default")) == 1

    action_log.review_action("default", action_id=aid, verdict="inconclusive")
    assert action_log.due_reviews("default") == []


def test_review_is_repeatable(scene):
    """改判要允许：当时觉得无效，一个月后回看可能是「对手在清仓，跟价其实守住了份额」。"""
    aid = action_log.record(
        "default", action_type="price_hold", competitor_id=scene["comp_id"],
        my_product_id=scene["my_id"], verify_days=7,
    )["action_id"]
    action_log.review_action("default", action_id=aid, verdict="ineffective", note="第一次")
    res = action_log.review_action("default", action_id=aid, verdict="effective", note="改判")
    assert res["ok"]
    assert res["verdict"] == "effective"
    assert res["review_note"] == "改判"


def test_review_missing_action(scene):
    res = action_log.review_action("default", action_id=999999, verdict="effective")
    assert res["ok"] is False
    assert "不存在" in res["error"]


# ============================================================
# 日报里带一段「到期该复盘」
# ============================================================


def test_brief_includes_review_section(scene):
    """日报末尾带待复盘——单开一个提醒渠道，一周后它就会被静音。"""
    aid = action_log.record(
        "default", action_type="price_hold", competitor_id=scene["comp_id"],
        my_product_id=scene["my_id"], verify_days=7,
    )["action_id"]
    _set_due_in_past(aid, 2)

    brief = briefer.build_daily_brief("default", hours=24)
    assert len(brief.reviews) == 1
    text = brief.to_text()
    assert "到期该复盘" in text
    # 三种状态必居其一，不能什么都不写：
    #   changed    → 摆出前后事实（"对手价"）
    #   unchanged  → 明说"没有任何变化"（这本身是结论）
    #   no_data    → 明说"没有可比"（这是缺数据，不是没变化）
    # 空着会被读成第一种，而那是最需要证据时最没有证据的一种读法。
    assert any(k in text for k in ("对手价", "没有任何变化", "没有可比")), text
