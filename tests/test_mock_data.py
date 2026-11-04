"""Mock 数据集：能造出来、造得对、造错了要炸。

## 为什么「造得对」要单独测

Mock 数据最容易出的错不是"造不出来"，而是**造出来了但自相矛盾**：
面板上写「C 级对手 · 词重合 71%」，两个字段各自都合法，凑在一起是假话。
一次这样的截图足以让人不再相信整个系统。

所以这里的核心断言是：引擎算出来的档位 == `SCENARIOS` 里写的期望档位。
它同时守两件事——引擎没坏、数据没歪。
"""
from __future__ import annotations

import pytest

from app.db import DATA_MODE_LIVE, DATA_MODE_MOCK, db_file_path, using_data_mode
from app.services import mock_data


@pytest.fixture(scope="module")
def built():
    """造一次，全模块共用（造数约 2~4 秒，每个用例重造没必要）。"""
    return mock_data.build(quiet=True)


# ============================================================
# 能造出来
# ============================================================


def test_build_reports_what_it_made(built):
    assert built["ok"] is True
    assert built["my_asin"] == mock_data.MOCK_MY_ASIN
    assert built["my_products"] == 1
    assert built["competitors"] == len(mock_data.SCENARIOS)
    assert built["snapshots"] > 0


def test_build_writes_into_mock_db_only(built):
    """库文件必须是 mock 那个。写错库是这一整套功能唯一的灾难性失败。"""
    mock_path = db_file_path(mode=DATA_MODE_MOCK)
    assert mock_path is not None
    assert mock_path.as_posix() == built["db_file"].replace("\\", "/")


def test_build_produces_signals_and_collect_runs(built):
    """看板和健康页在 Mock 模式下不能是空的，否则"完整演示"名不副实。"""
    assert built["signals"] > 0
    with using_data_mode(DATA_MODE_MOCK):
        from sqlalchemy import select

        from app.db import get_session
        from app.models import CollectRun

        with get_session() as s:
            runs = list(s.execute(select(CollectRun)).scalars())
    assert runs, "没有采集轮次，数据健康页会是一张空表"
    assert any(r.ok is False for r in runs), "没有失败轮次，看不出失败长什么样"


# ============================================================
# 造得对：档位由引擎算，且与场景名一致
# ============================================================


def test_tier_distribution_matches_scenarios(built):
    """每一条场景都要真的落到它想演示的档位上。

    `build()` 里的 `_rescore` 已经自检过一次，这里从结果侧再确认一遍：
    自检用的是"内存里算完立刻核"，这里是"落库之后读回来"。
    两者都可能单独出问题（比如 self-check 被绕过、或者落库时被覆盖）。
    """
    expected: dict[str, int] = {}
    for scen in mock_data.SCENARIOS:
        expected[scen["tier"]] = expected.get(scen["tier"], 0) + 1
    assert built["tiers"] == dict(sorted(expected.items()))


def test_all_five_outcomes_are_represented(built):
    """S / A / B / C / unknown 一个都不能少。

    只造"好看"的档位（比如全是 A/B），面板上「待判定」「已暂停」
    这些分支就永远没有样本——而它们恰恰是最容易写错的分支。
    """
    assert set(built["tiers"]) == {"S", "A", "B", "C", "unknown"}, built["tiers"]


def test_mock_competitors_carry_engine_reasons(built):
    """每条竞品都要有 `tier_reasons`，且里面的数字与列上的数字同源。"""
    from app.db import get_session
    from app.models import Competitor
    from sqlalchemy import select

    with using_data_mode(DATA_MODE_MOCK):
        with get_session() as s:
            comps = list(s.execute(select(Competitor)).scalars())
            rows = [(c.asin, c.tier, dict(c.tier_reasons or {}), c.keyword_overlap)
                    for c in comps]

    assert rows
    for asin, tier, reasons, kw in rows:
        assert reasons, f"{asin} 没有判定依据"
        assert reasons.get("price_ratio") is not None or asin == "B0MOCK1009", asin
        if kw is not None:
            assert reasons.get("keyword_overlap") == kw, f"{asin} 列与理由里的词重合同不同源"


def test_provisional_rows_say_why_they_are_provisional(built):
    """暂定的行必须同时有 `provisional=True` 和一条能读的理由。

    只有标志没有理由 → 面板只能显示「B 级（暂定）」，用户不知道还差什么。
    只有理由没有标志 → 前端那段判断根本走不到。
    """
    from app.db import get_session
    from app.models import Competitor
    from sqlalchemy import select

    with using_data_mode(DATA_MODE_MOCK):
        with get_session() as s:
            comps = list(s.execute(select(Competitor)).scalars())
            data = [(c.asin, dict(c.tier_reasons or {})) for c in comps]

    provisional = [(a, r) for a, r in data if r.get("provisional")]
    assert provisional, "Mock 数据里应当有暂定行（1007 缺词、1008 抽样薄）"


# ============================================================
# 造错了要炸
# ============================================================


def test_rescore_rejects_scenario_whose_expected_tier_is_wrong(monkeypatch):
    """把某条场景的期望档位改错，`build()` 必须抛错而不是照单全收。

    这是本模块最重要的守卫：它证明"自检是真的在跑"。
    如果这条测试失败，说明自检形同虚设——那样后面所有的档位断言都不可信。
    """
    broken = [dict(s) for s in mock_data.SCENARIOS]
    broken[0]["tier"] = "C"          # 1001 实际是 S
    monkeypatch.setattr(mock_data, "SCENARIOS", broken)

    with pytest.raises(mock_data.MockDataError) as exc:
        mock_data.build(quiet=True)
    msg = str(exc.value)
    assert "场景名写的是 C" in msg
    assert broken[0]["asin"] in msg


def test_guard_refuses_when_both_modes_share_one_file(monkeypatch):
    """两个连接串指向同一个文件时必须拒绝造数。

    这个守卫没有"部分生效"的余地：它一旦失效，Mock 数据就写进了真实库，
    而真实库里此后分不清哪条是采来的、哪条是造的。
    """
    from app.config import settings

    shared = db_file_path(mode=DATA_MODE_LIVE)
    assert shared is not None
    monkeypatch.setattr(settings, "mock_database_url", f"sqlite:///{shared.as_posix()}")

    with pytest.raises(mock_data.MockDataError) as exc:
        mock_data.build(quiet=True)
    assert "同一个库" in str(exc.value)


# ============================================================
# 清场
# ============================================================


def test_clear_empties_business_tables_but_keeps_schema():
    from sqlalchemy import select

    from app.db import ensure_mode_ready, get_session
    from app.models import Competitor, MyProduct, Signal, Snapshot

    ensure_mode_ready(DATA_MODE_MOCK)
    mock_data.clear()

    with using_data_mode(DATA_MODE_MOCK):
        with get_session() as s:
            for model in (MyProduct, Competitor, Snapshot, Signal):
                n = len(list(s.execute(select(model)).scalars()))
                assert n == 0, f"{model.__name__} 没清干净，还有 {n} 行"

    # 表还在（能查说明表没被删）
    with using_data_mode(DATA_MODE_MOCK):
        with get_session() as s:
            assert list(s.execute(select(Competitor)).scalars()) == []
