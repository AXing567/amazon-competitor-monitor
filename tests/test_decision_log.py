"""决策落库：把「建议」变成能回查的审计记录。

## 这组测试守的是什么

`decisions` 表建了两轮、一行没写。补上写入之后，真正的风险不是「写不进去」
（写不进去会报错），而是**写进去了但答不出问题**：

- 有结论没理由 → 「当时为什么建议不跟价」还是答不上；
- 有理由没数 → 「日销要涨到多少才不亏」算不出来，而那是当时决策的核心依据；
- 重复写 → 同一件事在库里出现五次，得在一堆一样的行里挑。

三条都不是异常，都是「看起来有数据」的坏数据。所以这里逐条钉住。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.capabilities.queries import get_change_history
from app.capabilities.types import CapContext
from app.db import get_session
from app.models import Action, Competitor, Decision, MyProduct, Signal
from app.services import briefer, decision_log


# ============================================================
# 造一套最小可判的现场
# ============================================================


@pytest.fixture()
def scene(clean_db):
    """一个自家锚点 + 一个竞品 + 一条降价信号。够 `judge_price_follow` 跑完整流程。

    `clean_db` 不是装饰：模块内的库是共用的，不清场的话上一个用例的
    `MyProduct` 会被 `build_daily_brief` 的 `limit(1)` 拿到，用例之间开始互相影响。
    """
    now = datetime.now()
    with get_session() as s:
        my = MyProduct(
            tenant_id="default",
            asin="B0MINE0001",
            marketplace="US",
            platform="amazon",
            current_price=24.99,
            break_even_price=13.20,
            daily_sales_avg=85.0,
            target_margin_rate=0.28,
            fba_available_units=2600,
            lead_time_days=32,
        )
        s.add(my)
        s.flush()
        comp = Competitor(
            tenant_id="default",
            my_product_id=my.id,
            asin="B0CMP00001",
            marketplace="US",
            platform="amazon",
            title="测试竞品",
            tier="S",
            status="active",
        )
        s.add(comp)
        s.flush()
        sig = Signal(
            tenant_id="default",
            competitor_id=comp.id,
            category="price",
            signal_type="price_drop",
            level="P0",
            before_value="26.99",
            after_value="22.99",
            change_pct=-0.148,
            description="降价 14.8%：$26.99 → $22.99",
            detected_at=now - timedelta(hours=2),
        )
        s.add(sig)
        # flush 才能拿到 sig.id。不 flush 的话 `scene["signal_id"]` 是 None，
        # 而 None 会在 `latest_for_signal` 里被 `int()` 拒掉——
        # 报错指向 decision_log，真正的原因在这个 fixture 少了一行。
        s.flush()
        return {"my_id": my.id, "comp_id": comp.id, "signal_id": sig.id}


def _decisions() -> list[Decision]:
    with get_session() as s:
        return list(s.execute(__import__("sqlalchemy").select(Decision)).scalars())


# ============================================================
# 写入
# ============================================================


def test_brief_persists_one_decision_per_item(scene):
    """日报里的每条 P0/P1 都要落一行，且 item 上回填了 decision_id。"""
    brief = briefer.build_daily_brief("default", hours=24)
    items = brief.p0 + brief.p1
    assert items, "造数没生效：日报里一条信号都没有"

    rows = _decisions()
    assert len(rows) == len(items)

    # 回填的意义：动作记录要靠它挂上「当时的判断」。缺了就是两张互不相认的表。
    for it in items:
        assert it.decision_id is not None, f"{it.asin} 的判断没回填 decision_id"
    ids = {it.decision_id for it in items}
    assert ids == {r.id for r in rows}


def test_decision_row_carries_reasons_and_metrics(scene):
    """审计行必须能独立回答「结论 + 为什么 + 当时的数」。"""
    briefer.build_daily_brief("default", hours=24)
    rows = _decisions()
    assert len(rows) == 1
    row = rows[0]

    assert row.headline, "没有一句话结论，回看时只剩 attack/no_action"
    assert row.reasons, "没有理由，等于只有判决书没有判决理由"
    assert row.suggested_actions, "没有动作建议，日报那条就等于没给下一步"
    assert row.metrics, "没有中间量，「日销要涨到多少才不亏」永久算不出来"
    assert row.confidence is not None, "没有确定性，所有结论看起来一样可信"
    assert row.signal_type == "price_drop"
    assert row.origin == decision_log.ORIGIN_BRIEF
    assert row.tenant_id == "default"

    # 跟价判断的核心证据：降价后的盈亏平衡单量
    be = row.metrics.get("break_even") or {}
    assert be.get("break_even_units"), f"盈亏平衡单量没落库：{row.metrics}"


def test_verify_due_at_derived_from_verify_days(scene):
    """`verify_days` 要有对应的日期，否则「建议几天后回看」只是个数字。"""
    briefer.build_daily_brief("default", hours=24)
    row = _decisions()[0]
    if not row.verify_days:
        pytest.skip("这条判断本身不设回看日（如缺数据），换场景再测")
    assert row.verify_due_at is not None
    delta = (row.verify_due_at - row.created_at).days
    assert delta == row.verify_days


# ============================================================
# 幂等
# ============================================================


def test_rerun_does_not_duplicate(scene):
    """日报重跑（手动触发/失败重试/面板刷新）不能把同一条判断写两次。

    这是这个模块最容易出的 bug，而且**不报错**：一周后 decisions 里
    全是重复行，「当时为什么」要在一堆一样的行里找。
    """
    briefer.build_daily_brief("default", hours=24)
    first = len(_decisions())
    assert first >= 1

    briefer.build_daily_brief("default", hours=24)
    briefer.build_daily_brief("default", hours=24)
    assert len(_decisions()) == first, "重跑产生了重复行"


def test_changed_metrics_produce_new_row(scene):
    """锚点变了导致判断依据变了 → 应该留新行，不是被去重掉。

    区分「重复」和「重判」：结论文案可能一模一样（都是「可以跟进降价」），
    但支撑它的日均销量从 85 变成 200，那是**两次不同的判断**。
    只按 (signal, decision) 去重会把这个差别抹平。
    """
    briefer.build_daily_brief("default", hours=24)
    before = len(_decisions())
    assert before == 1

    with get_session() as s:
        my = s.get(MyProduct, scene["my_id"])
        my.daily_sales_avg = 200.0      # 盈亏平衡单量的分母变了

    briefer.build_daily_brief("default", hours=24)
    rows = _decisions()
    assert len(rows) == before + 1, "依据变了却没留新行，历史被覆盖"
    # 两行的 key 必须不同，否则唯一索引会挡掉第二行
    keys = {r.dedupe_key for r in rows}
    assert len(keys) == len(rows)


def test_same_content_same_key(scene):
    """内容完全相同 → 同一把钥匙。这是唯一索引能起作用的前提。"""
    args = dict(
        tenant_id="default",
        signal_id=1,
        decision="attack",
        headline="可以跟进降价",
        actions=["a", "b"],
        metrics={"break_even": {"break_even_units": 128}},
    )
    assert decision_log.dedupe_key(**args) == decision_log.dedupe_key(**args)
    other = dict(args, headline="建议不跟价")
    assert decision_log.dedupe_key(**args) != decision_log.dedupe_key(**other)


# ============================================================
# 跳过与降级
# ============================================================


def test_record_without_signal_is_skipped():
    """没有 signal_id 的判断不落库（它没法按信号回查，也就没有审计价值）。

    刻意不猜一个 competitor_id 出来：猜错会写进另一个竞品的账上，
    而且看起来完全正常。
    """
    res = decision_log.persist(
        [{"competitor_id": 1, "decision": "attack", "headline": "无信号"}],
        tenant_id="default",
    )
    assert res["ok"] is True
    assert res["skipped"] == 1
    assert res["written"] == 0


def test_persist_failure_does_not_raise(scene, monkeypatch):
    """审计写失败时**不能抛**——它的调用点在「马上要往外发消息」的链路上。

    老板可以不看审计记录，但不能不收到「对手断货了」。
    """
    import app.services.decision_log as dl

    def boom(*a, **kw):
        from sqlalchemy.exc import OperationalError

        raise OperationalError("INSERT", {}, Exception("disk full"))

    monkeypatch.setattr(dl, "get_session", boom)
    res = dl.persist(
        [{"signal_id": 1, "competitor_id": 1, "decision": "attack", "headline": "x"}],
        tenant_id="default",
    )
    assert res["ok"] is False
    assert res["written"] == 0


# ============================================================
# 另一条出口：实时预警
# ============================================================


def test_alert_path_also_persists(scene):
    """实时预警出口也要落库。

    只在日报里落的话，P0 推送出去的那些判断永远查不到——
    而 P0 恰恰是最需要交代「凭什么」的那批。
    """
    items = briefer.build_alerts_since("default", minutes=180)
    assert items, "造数没生效：没有未推送的 P0 信号"

    rows = _decisions()
    assert len(rows) == len(items)
    assert {r.origin for r in rows} == {decision_log.ORIGIN_ALERT}
    for it in items:
        assert it.decision_id is not None


def test_two_paths_dedupe_together(scene):
    """预警发过、日报再跑一遍 → 同一条信号同一种判断，库里有且只有一行。

    两条出口共用 `_attach_action`，如果去重只做了各自的一半，
    同一件事会在库里出现两次、来源不同，回看时难以判断哪条是主记录。
    """
    briefer.build_alerts_since("default", minutes=180)
    n_alert = len(_decisions())
    briefer.build_daily_brief("default", hours=24)
    assert len(_decisions()) == n_alert, "预警与日报对同一条信号写了两行"


# ============================================================
# 读取
# ============================================================


def test_get_returns_audit_shape(scene):
    briefer.build_daily_brief("default", hours=24)
    row = _decisions()[0]
    got = decision_log.get(row.id, tenant_id="default")
    assert got is not None
    assert got["headline"]
    assert got["reasons"]
    assert got["actions"]
    assert got["metrics"]
    assert got["origin_label"] == "日报"
    assert got["decision_label"] in ("进攻", "防守", "学习", "不动")
    assert got["asin"] == "B0CMP00001"


def test_get_respects_tenant(scene):
    """别的租户取不到——这条现在只有一个租户，但过滤条件写错时不会有人发现。"""
    briefer.build_daily_brief("default", hours=24)
    row = _decisions()[0]
    assert decision_log.get(row.id, tenant_id="other") is None


def test_latest_for_signal_returns_newest(scene):
    """一条信号被重判过 → 返回最近那次。

    动作记录挂的是「人做动作那一刻的依据」，也就是最近的判断，
    不是第一次的判断。
    """
    briefer.build_daily_brief("default", hours=24)
    with get_session() as s:
        my = s.get(MyProduct, scene["my_id"])
        my.daily_sales_avg = 200.0
    briefer.build_daily_brief("default", hours=24)

    latest = decision_log.latest_for_signal("default", scene["signal_id"])
    assert latest is not None
    rows = _decisions()
    assert latest["id"] == max(r.id for r in rows)


# ============================================================
# 出口：判断必须能被查回来
# ============================================================


def test_change_history_has_a_decision_line(scene):
    """看板「变更历史」要有第四条时间线：判断。

    只有「快照 / 信号 / 动作」三条线时，判断只能从动作记录点进去看
    （「当时的判断」按钮）。而**大多数判断压根没变成动作**——看完决定不动
    也是一种决定——这批恰恰最该复盘：事后发现判断错了，也没有任何入口能翻回来。
    """
    briefer.build_daily_brief("default", hours=24)

    res = get_change_history(CapContext(tenant_id="default"), "B0CMP00001", days=30)
    assert res["ok"] is True
    decs = res["decisions"]
    assert len(decs) == 1, "落库了但变更历史里看不到，等于只写不读"

    d = decs[0]
    assert d["decision"] in ("attack", "defend", "learn", "no_action")
    # 代码值给面板上色用，中文给业务看。只给代码值，页面上就会出现 no_action
    assert d["decision_label"] and d["decision_label"] != d["decision"]
    assert d["headline"]
    assert d["reasons"], "这一栏光有结论没理由，等于让人自己回忆"
    assert d["origin_label"] == "日报"
    assert d["signal_id"] == scene["signal_id"], "判断要能追回是哪条信号引出的"


# ============================================================
# 出口：判断必须能被查回来
# ============================================================


def test_change_history_has_a_decision_line(scene):
    """看板「变更历史」要有第四条时间线：判断。

    只有「快照 / 信号 / 动作」三条线时，判断只能从动作记录点进去看
    （「当时的判断」按钮）。而**大多数判断压根没变成动作**——看完决定不动
    也是一种决定——这批恰恰最该复盘：事后发现判断错了，也没有任何入口能翻回来。
    """
    briefer.build_daily_brief("default", hours=24)

    res = get_change_history(CapContext(tenant_id="default"), "B0CMP00001", days=30)
    assert res["ok"] is True
    decs = res["decisions"]
    assert len(decs) == 1, "落库了但变更历史里看不到，等于只写不读"

    d = decs[0]
    assert d["decision"] in ("attack", "defend", "learn", "no_action")
    # 代码值给面板上色用，中文给业务看。只给代码值，页面上就会出现 no_action
    assert d["decision_label"] and d["decision_label"] != d["decision"]
    assert d["headline"]
    assert d["reasons"], "这一栏光有结论没理由，等于让人自己回忆"
    assert d["origin_label"] == "日报"
    assert d["signal_id"] == scene["signal_id"], "判断要能追回是哪条信号引出的"
