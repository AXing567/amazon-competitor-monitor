"""阈值规格 / L2 参数化 / L3 编排 的测试。

不依赖 pytest：直接 `python tests/test_thresholds.py`（沿用 tests/test_domain.py 的运行器风格）。

## 一条必须理解的约定：本文件跑在临时库上

`app.db` 在 import 时就按 `settings.database_url` 建 engine，所以
**必须在 import app.* 之前**改 `DATABASE_URL` 环境变量，否则会写到你真实的
`data/competitor_monitor.db` 里。这不是洁癖——T03 的用例会建用户、写阈值、
造采集日志，落在真库上就是脏数据。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import traceback
from dataclasses import fields
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---- 必须在 import app.* 之前（见模块文档）----
_TMP_DIR = Path(tempfile.mkdtemp(prefix="thr_test_"))
_DB_FILE = _TMP_DIR / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_FILE}"
os.environ["ACTION_TOKEN_SECRET"] = ""          # 回调 fail-closed 用例依赖它是空

from sqlalchemy import func, select                                                # noqa: E402

from app.adapters.unified import UnifiedSnapshot                                   # noqa: E402
from app.config import settings                                                    # noqa: E402
from app.db import get_session, init_db                                            # noqa: E402
from app.domain import tiering as tiering_mod                                       # noqa: E402
from app.domain.diff import RULES, DiffEngine                                       # noqa: E402
from app.domain.thresholds import (                                                 # noqa: E402
    GROUP_CLASSES,
    GROUPS,
    SPEC_BY_KEY,
    THRESHOLD_SPEC,
    DiffThresholds,
    NotifyThresholds,
    TieringThresholds,
    defaults,
    for_group,
    spec_groups,
    validate,
    warnings,
)
from app.domain.tiering import judge_tier                                           # noqa: E402
from app.models import (                                                            # noqa: E402
    Action,
    CollectRun,
    Competitor,
    MyProduct,
    Signal,
    Snapshot,
    ThresholdConfig,
    ThresholdVersion,
)
from app.services import (                                                          # noqa: E402
    action_log,
    binding,
    collector,
    health,
    identity,
    pool,
    scheduler,
    settings_store,
    simulator,
)


# ============================================================
# T01 · 默认值等价：全篇最关键的一条断言
# ============================================================


def test_diff_defaults_equal_rules():
    """`DiffThresholds()` 的每个字段必须等于 `RULES` 的对应值。

    31 个领域测试的期望值全建立在 RULES 的数值上。这个等价断言一旦失守，
    说明「重构把默认值改了」，回归基线也就废了——所以它必须是一条测试，
    而不是一句「我核对过了」。
    """
    d = DiffThresholds()
    for key, expected in RULES.items():
        if key == "phase_scale":
            continue
        assert getattr(d, key) == expected, f"{key}: {getattr(d, key)} != {expected}"


def test_diff_phase_scale_roundtrip():
    """phase_scale 在 SPEC 里拆成三个数字，但拼回来必须与 RULES 的 dict 完全一致。"""
    d = DiffThresholds()
    assert d.phase_scale == RULES["phase_scale"]
    # 嵌套写法（RULES 原样）也要能被吃下去
    assert DiffThresholds.from_dict(RULES) == d
    assert DiffThresholds.from_dict({"phase_scale": {"blast": 0.5}}).phase_scale_blast == 0.5


def test_tiering_defaults_equal_module_constants():
    """tiering 模块常量已降级为「默认值来源」，必须与 dataclass 逐键相等。"""
    t = TieringThresholds()
    expected = {
        "price_ratio_min": tiering_mod.PRICE_RATIO_MIN,
        "price_ratio_max": tiering_mod.PRICE_RATIO_MAX,
        "keyword_overlap_gate": tiering_mod.KEYWORD_OVERLAP_GATE,
        "overlap_s": tiering_mod.OVERLAP_S,
        "overlap_a": tiering_mod.OVERLAP_A,
        "overlap_b": tiering_mod.OVERLAP_B,
        "core_coverage": tiering_mod.CORE_COVERAGE,
    }
    for key, value in expected.items():
        assert getattr(t, key) == value, f"{key}: {getattr(t, key)} != {value}"


def test_notify_defaults_equal_collector_cooldown():
    """冷却窗口的默认值 = 重构前 collector.COOLDOWN_HOURS，锁死。"""
    from app.services.collector import COOLDOWN_HOURS

    n = NotifyThresholds()
    for level, hours in COOLDOWN_HOURS.items():
        assert n.cooldown_hours(level) == hours, f"{level}: {n.cooldown_hours(level)} != {hours}"


# ============================================================
# T01 · SPEC 双向覆盖（不靠肉眼，靠断言）
# ============================================================


def test_spec_covers_every_dataclass_field():
    """dataclass 字段 ↔ SPEC 键 一一对应，双向都不能缺。

    单向检查（只查 dataclass → SPEC）会漏掉「SPEC 里有一个没人认领的键」，
    那类脏数据会让面板渲染出一个改了也不生效的输入框。
    """
    for group in GROUPS:
        cls = GROUP_CLASSES[group]
        field_names = {f.name for f in fields(cls)}
        spec_keys = {item.key for item in THRESHOLD_SPEC[group]}
        assert field_names == spec_keys, (
            f"{group}: dataclass 独有 {field_names - spec_keys}；SPEC 独有 {spec_keys - field_names}"
        )


def test_spec_includes_declared_but_unconsumed_keys():
    """声明了但没人读的死键也必须进 SPEC。

    `oos_confirm_minutes` 与 `core_coverage` 当前都没有消费方。跳过它们的后果：
    下一个写采集确认逻辑 / 词池逻辑的人会重新硬编码一个 30 或 0.8，
    而配置页上永远看不到这个数字——死键就是下一批散落魔法数字的胚胎。
    """
    assert "oos_confirm_minutes" in {i.key for i in THRESHOLD_SPEC["diff"]}
    assert "core_coverage" in {i.key for i in THRESHOLD_SPEC["tiering"]}
    assert "phase_scale_warmup" in {i.key for i in THRESHOLD_SPEC["diff"]}


def test_spec_defaults_come_from_dataclass():
    """SPEC 的 default 必须取自 dataclass 实例，不能另写一份数字。"""
    for group in GROUPS:
        instance = GROUP_CLASSES[group]()
        for item in THRESHOLD_SPEC[group]:
            assert item.default == getattr(instance, item.key), item.key


def test_spec_schema_shape_for_panel():
    """面板靠 schema 动态渲染表单，所以结构必须可枚举且分组齐全。"""
    groups = spec_groups()
    assert [g["group"] for g in groups] == list(GROUPS)
    for g in groups:
        assert g["title"]
        for item in g["items"]:
            assert item["label"] and item["type"] in ("int", "float")
            # 面板要显示范围，范围必须给全（否则前端没法做输入校验）
            assert item["minimum"] is not None and item["maximum"] is not None


# ============================================================
# T01 · 校验与合并
# ============================================================


def test_validate_rejects_out_of_range():
    cleaned, errors = validate("diff", {"price_p0_pct": 0.9})
    assert cleaned == {}
    assert errors and "超出允许范围" in errors[0]


def test_validate_rejects_unknown_key():
    cleaned, errors = validate("diff", {"price_p0_pct_typo": 0.05})
    assert cleaned == {}
    assert errors and "不属于分组" in errors[0]


def test_validate_coerces_and_accepts():
    cleaned, errors = validate("diff", {"price_p0_pct": "0.05", "rank_min_abs_move": "30"})
    assert errors == []
    assert cleaned == {"price_p0_pct": 0.05, "rank_min_abs_move": 30}
    assert isinstance(cleaned["rank_min_abs_move"], int)


def test_validate_warns_on_p1_above_p0():
    """P1 门槛高于 P0 是「怪但在某些场景合理」的配置：提示后果，不阻断。

    真正拦下来会让「把 price_p0_pct 调到 0.001 做清仓监控」这种合理用法无法保存。
    """
    cleaned, errors = validate("diff", {"price_p1_pct": 0.2})
    assert errors == [] and cleaned == {"price_p1_pct": 0.2}
    warns = warnings("diff", {"price_p1_pct": 0.2})
    assert warns and "P1" in warns[0]

    assert warnings("diff", {"price_p1_pct": 0.005}) == []


def test_merge_returns_new_object():
    """merge 必须返回新对象：共享实例被就地改掉会让试算顺手改生产阈值。"""
    base = DiffThresholds()
    merged = base.merge({"price_p0_pct": 0.10})
    assert base.price_p0_pct == 0.03
    assert merged.price_p0_pct == 0.10
    assert isinstance(merged, DiffThresholds)


def test_from_dict_fills_defaults_and_ignores_unknown():
    """稀疏覆盖读取：没给的键落回默认值，未知键不炸（写路径才该拒绝）。"""
    d = DiffThresholds.from_dict({"price_p0_pct": 0.05, "no_such_key": 1})
    assert d.price_p0_pct == 0.05
    assert d.price_p1_pct == defaults("diff")["price_p1_pct"]
    assert DiffThresholds.unknown_keys({"no_such_key": 1}) == {"no_such_key"}


def test_for_group_builds_typed_object():
    obj = for_group("tiering", {"overlap_s": 0.7})
    assert isinstance(obj, TieringThresholds)
    assert obj.overlap_s == 0.7


# ============================================================
# T02 · 阈值真的能改行为
# ============================================================


def _snap(**kw) -> UnifiedSnapshot:
    base = dict(
        asin="B0TEST",
        price=20.0,
        net_price=20.0,
        price_source="promotion",
        rank_sub=1000,
        rating=4.5,
        review_count=500,
        in_stock=True,
        stock_hint="IN_STOCK",
        title="Toy Car",
        image_url="http://img/1.jpg",
    )
    base.update(kw)
    return UnifiedSnapshot(**base)


def test_diff_engine_thresholds_take_effect():
    """-5% 降价：默认 3% 门槛 → P0；门槛抬到 10% → 只到 P1。"""
    prev = _snap()
    curr = _snap(net_price=19.0, price=19.0)

    default_levels = [s.level for s in DiffEngine().compare(prev, curr) if s.category == "price"]
    loose_levels = [
        s.level
        for s in DiffEngine(thresholds=DiffThresholds(price_p0_pct=0.10)).compare(prev, curr)
        if s.category == "price"
    ]
    assert default_levels == ["P0"]
    assert loose_levels == ["P1"]


def test_diff_engine_positional_phase_still_works():
    """位置参数 `DiffEngine("blast")` 必须仍取到 phase=blast 且阈值收紧。"""
    engine = DiffEngine("blast")
    assert engine.phase == "blast"
    assert engine.scale == DiffThresholds().phase_scale_blast
    assert engine._t("price_p0_pct") == DiffThresholds().price_p0_pct * 0.8


def test_diff_engine_unknown_phase_falls_back():
    assert DiffEngine("no-such-phase").phase == "warmup"


def test_noise_guard_is_configurable():
    """噪声闸门也要能被配置改到（否则它只是换了个位置的硬编码）。"""
    prev = _snap()
    curr = _snap(net_price=19.94, price=19.94)   # -0.3%
    assert not [s for s in DiffEngine().compare(prev, curr) if s.category == "price"]
    loose = DiffEngine(thresholds=DiffThresholds(price_floor_guard=0.0))
    assert [s for s in loose.compare(prev, curr) if s.category == "price"]


def test_judge_tier_threshold_takes_effect_and_reason_uses_it():
    """门槛调到 90% 后：overlap=0.8 的样本被拒，且拒绝理由里的数字是 90%。"""
    comp = UnifiedSnapshot(
        asin="B0T",
        price=20.0,
        net_price=20.0,
        category_path=["Home", "Kitchen", "Blenders"],     # 节点不同 → 只剩词重合这条路
        keywords=[{"keyword": "toy car", "traffic_share": 80}],
        monthly_units=900,
    )
    kw = {"toy car": 80.0, "kids gift": 20.0}

    strict = judge_tier(
        my_price=20.0,
        my_core_keywords=kw,
        my_category_path=["Toys"],
        my_daily_units=30,
        comp=comp,
        thresholds=TieringThresholds(keyword_overlap_gate=0.90),
    )
    assert strict.passed is False
    assert strict.reject_reason and "90%" in strict.reject_reason
    assert "40%" not in strict.reject_reason

    # 默认 40% 门槛下同一份数据能进池（证明差异是阈值造成的，不是数据造成的）
    loose = judge_tier(
        my_price=20.0,
        my_core_keywords=kw,
        my_category_path=["Toys"],
        my_daily_units=30,
        comp=comp,
    )
    assert loose.passed is True


# ============================================================
# 红线：L2 必须保持纯函数
# ============================================================


def test_domain_layer_has_no_io():
    """`app/domain/*.py` 不许出现数据库 / 网络 / 环境变量 / 当前时间。

    白名单：`tiering.py` 里 `date.today()` 是既有代码（判断新品上架天数），
    本轮不动它——把它挪到上层属于「重构」，不在本任务范围。
    """
    forbidden = (
        "sqlalchemy",
        "app.db",
        "app.models",
        "httpx",
        "requests",
        "os.environ",
        "datetime.now()",
    )
    offenders = []
    for path in sorted((ROOT / "app" / "domain").glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for pattern in forbidden:
                if pattern in line:
                    offenders.append(f"{path.name}:{lineno} 含 {pattern}")
    assert not offenders, offenders


def test_no_runtime_rules_read_in_diff():
    """`RULES` 只允许作为「默认值来源」存在，运行时不读。

    这条是 T02 的机制保证：只要方法体里还留着 `RULES[...]`，
    换个阈值对象就不生效，而单测很可能仍然全绿。
    """
    src = (ROOT / "app" / "domain" / "diff.py").read_text(encoding="utf-8")
    assert "RULES = {" in src, "RULES 应保留为默认值来源"
    assert "RULES[" not in src, "方法体里还有运行时读取 RULES 的地方"


# ============================================================
# T03 · L3 编排（配置仓库 / 试算 / 身份 / 动作 / 绑定 / 采集埋点）
#
# 这一组用例会写库，但写的是模块头部指定的临时库（见模块文档）。
# 所有断言都做成「前后差量」或「指定 tenant」，因此**测试顺序无关**。
# ============================================================

_SEEDED = False

CMP_A = "B0CMP0001"     # S 级，价格 20.00 → 19.60 → 18.60 → 17.60
CMP_B = "B0CMP0002"     # A 级，价格 30.00 → 28.00 → 27.00


def _seed() -> None:
    """建库 + 造演示数据。只造一次。"""
    global _SEEDED
    if _SEEDED:
        return

    init_db()
    identity.ensure_default_user()

    now = datetime.now()
    with get_session() as s:
        my = MyProduct(
            tenant_id="default",
            asin="B0MINE0001",
            marketplace="US",
            current_price=20.0,
            break_even_price=15.0,
            daily_sales_avg=30.0,
            fba_available_units=900,
            target_margin_rate=0.25,
            core_keywords={"toy car": 80.0, "kids gift": 20.0},
        )
        s.add(my)
        s.flush()

        reasons = {
            "price_ratio": 1.0,
            "price_band_ok": True,
            "keyword_overlap": 0.72,
            "keyword_pool_ok": True,
            "node_match": True,
            "functional_match": None,
        }
        for asin, tier, series in (
            (CMP_A, "S", [(3, 20.00), (2, 19.60), (1, 18.60), (0, 17.60)]),
            (CMP_B, "A", [(3, 30.00), (2, 28.00), (1, 27.00)]),
        ):
            comp = Competitor(
                tenant_id="default",
                my_product_id=my.id,
                asin=asin,
                tier=tier,
                tier_reasons=dict(reasons),
                tier_updated_at=now,
                price_ratio=1.0,
                keyword_overlap=0.72,
                node_match=True,
                status="active",
                title=f"demo {asin}",
            )
            s.add(comp)
            s.flush()
            for days_ago, price in series:
                s.add(
                    Snapshot(
                        competitor_id=comp.id,
                        captured_at=now - timedelta(days=days_ago),
                        price=price,
                        net_price=price,
                        coupon_value=0.0,
                        in_stock=True,
                        stock_hint="IN_STOCK",
                        data_quality="full",
                        source="sellersprite",
                    )
                )
    _SEEDED = True


def _competitor_id(asin: str) -> int:
    _seed()
    with get_session() as s:
        comp = s.execute(select(Competitor).where(Competitor.asin == asin)).scalar_one()
        return int(comp.id)


def _counts() -> dict[str, int]:
    """5 张关键表的行数快照。试算零写库就是靠它前后比对。"""
    _seed()
    with get_session() as s:
        return {
            m.__tablename__: s.execute(select(func.count()).select_from(m)).scalar_one()
            for m in (Signal, Snapshot, CollectRun, ThresholdConfig, ThresholdVersion)
        }


def _last_run(competitor_id: int) -> CollectRun:
    with get_session() as s:
        return s.execute(
            select(CollectRun)
            .where(CollectRun.competitor_id == competitor_id)
            .order_by(CollectRun.id.desc())
            .limit(1)
        ).scalar_one()


def _tiers() -> dict[str, str]:
    """当前各竞品分层。用例里做「改阈值 → 观察分层变化 → 复位」用。"""
    _seed()
    with get_session() as s:
        return {c.asin: c.tier for c in s.execute(select(Competitor)).scalars()}


def _restore_tiers(tiers: dict[str, str]) -> None:
    with get_session() as s:
        for asin, tier in tiers.items():
            comp = s.execute(
                select(Competitor).where(Competitor.asin == asin)
            ).scalar_one_or_none()
            if comp is not None:
                comp.tier = tier


# ------------------------------------------------------------
# 三层继承 / 版本 / 回滚
# ------------------------------------------------------------


def test_three_layer_inheritance():
    _seed()
    tenant = "t-three-layer"
    product_id = 101

    r = settings_store.save(
        scope="company", group_name="diff", values={"price_p0_pct": 0.05}, tenant_id=tenant
    )
    assert r["ok"] and r["version"] == 1

    values, sources = settings_store.resolve(tenant, my_product_id=product_id)
    assert values["diff"]["price_p0_pct"] == 0.05
    assert sources["diff"]["price_p0_pct"] == "company"

    settings_store.save(
        scope="user", scope_id="7", group_name="diff",
        values={"price_p0_pct": 0.08}, tenant_id=tenant,
    )
    values, sources = settings_store.resolve(tenant, user_id=7, my_product_id=product_id)
    assert values["diff"]["price_p0_pct"] == 0.08
    assert sources["diff"]["price_p0_pct"] == "user"

    settings_store.save(
        scope="product", scope_id=str(product_id), group_name="diff",
        values={"price_p0_pct": 0.10}, tenant_id=tenant,
    )
    values, sources = settings_store.resolve(tenant, user_id=7, my_product_id=product_id)
    assert values["diff"]["price_p0_pct"] == 0.10
    assert sources["diff"]["price_p0_pct"] == "product"

    # 全都不设 → 默认值，来源标 default
    values, sources = settings_store.resolve("t-three-layer-empty", my_product_id=999)
    assert values["diff"]["price_p0_pct"] == 0.03
    assert sources["diff"]["price_p0_pct"] == "default"


def test_collect_path_skips_user_layer():
    """采集是系统行为，不传 user_id → user 层必须被跳过（不是当空值）。"""
    _seed()
    tenant = "t-collect-path"
    settings_store.save(
        scope="company", group_name="diff", values={"price_p0_pct": 0.05}, tenant_id=tenant
    )
    settings_store.save(
        scope="user", scope_id="9", group_name="diff",
        values={"price_p0_pct": 0.09}, tenant_id=tenant,
    )

    at_company = settings_store.resolve_diff_thresholds(tenant, 555)
    assert at_company.price_p0_pct == 0.05

    with_user = settings_store.resolve_diff_thresholds(tenant, 555, user_id=9)
    assert with_user.price_p0_pct == 0.09


def test_versions_append_and_rollback_appends():
    """连续保存 3 次 → v1/v2/v3；回滚到 v1 → v4，且历史 4 行都在。"""
    _seed()
    tenant = "t-versions"
    base = dict(scope="company", group_name="diff", tenant_id=tenant)

    r1 = settings_store.save(values={"price_p0_pct": 0.05}, operator="老板", **base)
    r2 = settings_store.save(values={"price_p0_pct": 0.06}, operator="小李", **base)
    r3 = settings_store.save(values={"price_p1_pct": 0.02}, operator="小李", **base)
    assert [r1["version"], r2["version"], r3["version"]] == [1, 2, 3]
    assert r2["changed_keys"] == ["price_p0_pct"]

    versions = settings_store.list_versions(
        scope="company", group_name="diff", tenant_id=tenant
    )
    assert [v["version"] for v in versions] == [3, 2, 1]
    assert [v["action"] for v in versions] == ["update", "update", "create"]

    rb = settings_store.rollback(
        scope="company", group_name="diff", version=1, tenant_id=tenant, operator="老板"
    )
    assert rb["ok"] and rb["new_version"] == 4 and rb["from_version"] == 1

    values, _ = settings_store.resolve(tenant)
    assert values["diff"]["price_p0_pct"] == 0.05
    # v1 里没有 price_p1_pct 覆盖 → 回滚后必须回到默认值（replace 语义，不是合并）
    assert values["diff"]["price_p1_pct"] == 0.01

    versions = settings_store.list_versions(
        scope="company", group_name="diff", tenant_id=tenant
    )
    assert len(versions) == 4, "回滚不许删历史"
    assert [v["action"] for v in versions] == ["rollback", "update", "update", "create"]


# ------------------------------------------------------------
# 试算：零写库 / 自一致 / 方向
# ------------------------------------------------------------


def test_simulate_writes_nothing():
    _seed()
    before = _counts()
    result = simulator.simulate_diff_thresholds(
        "default", {"diff": {"price_p0_pct": 0.10}}, days=7
    )
    assert result["ok"] is True
    after = _counts()
    assert before == after, f"试算写了库：{before} → {after}"


def test_simulate_self_consistent():
    """把「当前生效值」原样当候选值试算，delta 必须全 0。

    这条能一次抓出「试算逻辑与生产逻辑不一致」的 bug——一旦不一致，
    用户就会看到「试算说 3 次、实际报 12 次」，从此不信这个功能。
    """
    _seed()
    current_values, _ = settings_store.resolve("default")
    result = simulator.simulate_diff_thresholds(
        "default", {"diff": current_values["diff"]}, days=7
    )
    assert result["ok"] is True
    assert result["delta"] == {"p0": 0, "p1": 0, "p2": 0, "total": 0}, result["delta"]
    assert result["current"] == result["candidate"]
    assert result["samples"] == []
    # 窗口里确实有命中，否则「全 0」可能是因为根本没数据（假绿）
    assert result["current"]["total"] > 0


def test_simulate_direction_is_sane():
    """门槛调大 → P0 变少；调到极小 → P0 变多。"""
    _seed()
    loose = simulator.simulate_diff_thresholds(
        "default", {"diff": {"price_p0_pct": 0.10}}, days=7
    )
    assert loose["ok"]
    assert loose["candidate"]["p0"] < loose["current"]["p0"]

    tight = simulator.simulate_diff_thresholds(
        "default", {"diff": {"price_p0_pct": 0.001}}, days=7
    )
    assert tight["ok"]
    assert tight["candidate"]["p0"] >= tight["current"]["p0"]


def test_simulate_reports_two_numbers():
    """两个数字都要给：原始命中数 + 模拟冷却去重后的命中数。"""
    _seed()
    result = simulator.simulate_diff_thresholds("default", {}, days=7)
    assert result["ok"]
    hits = result["hit_counts"]
    assert set(hits) == {
        "current_raw",
        "current_after_cooldown",
        "candidate_raw",
        "candidate_after_cooldown",
    }
    # 冷却只会减少命中，不会增加
    assert hits["current_after_cooldown"] <= hits["current_raw"]
    assert result["after_cooldown"]["current"]["total"] == hits["current_after_cooldown"]


def test_simulate_rejects_bad_values():
    _seed()
    result = simulator.simulate_diff_thresholds("default", {"diff": {"price_p0_pct": 9}}, days=7)
    assert result["ok"] is False
    assert "超出允许范围" in result["error"]


def test_simulate_survives_empty_tenant():
    """没数据也要能打开试算页：返回 ok + 全 0，而不是报错。"""
    _seed()
    result = simulator.simulate_diff_thresholds("t-no-such-tenant", {"diff": {}}, days=7)
    assert result["ok"] is True
    assert result["window"]["competitors"] == 0
    assert result["current"]["total"] == 0


# ------------------------------------------------------------
# 采集埋点
# ------------------------------------------------------------


def test_collect_records_failed_run():
    """失败也要落一行：ok=0 + errors 非空 + duration_ms>0 + trigger 正确。"""
    _seed()
    cid = _competitor_id(CMP_B)
    old_url = settings.sellersprite_bridge_url
    settings.sellersprite_bridge_url = "http://127.0.0.1:1"   # 必然 connection refused
    try:
        res = asyncio.run(collector.acollect_competitor(cid))
    finally:
        settings.sellersprite_bridge_url = old_url

    assert res.ok is False
    run = _last_run(cid)
    assert run.ok is False
    assert run.errors, "失败轮次必须留下原因"
    assert run.duration_ms and run.duration_ms > 0
    assert run.trigger == "schedule"
    assert run.finished_at is not None


def test_collect_records_success_run_and_signal_count():
    """成功一轮：ok=1，且 signal_count == 本次真正落库的信号数。"""
    _seed()
    cid = _competitor_id(CMP_B)
    fake = UnifiedSnapshot(
        asin=CMP_B, price=24.0, net_price=24.0, price_source="promotion",
        in_stock=True, stock_hint="IN_STOCK", data_quality="full", source="test",
    )

    async def fake_fetch(self, asin, marketplace="US"):
        return fake

    before = _counts()["signals"]
    with mock.patch.object(collector.SellerSpriteAdapter, "fetch_snapshot", fake_fetch):
        res = asyncio.run(collector.acollect_competitor(cid, trigger="panel"))
    after = _counts()["signals"]

    assert res.ok is True
    assert after - before == len(res.signals)
    run = _last_run(cid)
    assert run.ok is True
    assert run.trigger == "panel"
    assert run.signal_count == after - before
    assert run.duration_ms and run.duration_ms > 0


def test_collect_uses_configured_thresholds():
    """公司层把 price_p0_pct 调到 0.20 后，-5% 只判到 P1；恢复 0.03 又是 P0。"""
    _seed()
    cid = _competitor_id(CMP_A)
    tenant = "default"

    # 把冷却关掉：本用例只想验证「阈值是否生效」，不想被去重规则干扰
    settings_store.save(
        scope="company", group_name="notify", tenant_id=tenant,
        values={"cooldown_p0_hours": 0, "cooldown_p1_hours": 0, "cooldown_p2_hours": 0},
    )

    def make_snap(price: float) -> UnifiedSnapshot:
        return UnifiedSnapshot(
            asin=CMP_A, price=price, net_price=price, price_source="promotion",
            in_stock=True, stock_hint="IN_STOCK", data_quality="full", source="test",
        )

    def run_with(price: float):
        async def fake_fetch(self, asin, marketplace="US"):
            return make_snap(price)

        with mock.patch.object(collector.SellerSpriteAdapter, "fetch_snapshot", fake_fetch):
            return asyncio.run(collector.acollect_competitor(cid))

    settings_store.save(
        scope="company", group_name="diff", tenant_id=tenant, values={"price_p0_pct": 0.20}
    )
    loose = run_with(16.70)      # 相对上一轮 17.60 → -5.1%
    drops = [d for d in loose.signals if d.signal_type == "price_drop"]
    assert drops and all(d.level == "P1" for d in drops), [d.level for d in drops]

    settings_store.save(
        scope="company", group_name="diff", tenant_id=tenant, values={"price_p0_pct": 0.03}
    )
    strict = run_with(15.85)     # 相对 16.70 → -5.1%
    drops2 = [d for d in strict.signals if d.signal_type == "price_drop"]
    assert drops2 and drops2[0].level == "P0", [d.level for d in drops2]


# ------------------------------------------------------------
# 身份
# ------------------------------------------------------------


def test_identity_ensure_default_user_is_idempotent():
    _seed()
    first = identity.resolve_current_user(None)
    second = identity.resolve_current_user(None)
    assert first["id"] == second["id"]
    assert first["role"] == "owner"

    users = identity.list_users(include_identities=False)
    assert len([u for u in users if u["role"] == "owner"]) == 1


def test_identity_unmapped_external_is_counted():
    _seed()
    identity.reset_unmapped()
    assert identity.resolve_by_external("feishu", "ou_not_mapped") is None

    seen = {i["external_id"]: i for i in identity.unmapped_identities()}
    assert "ou_not_mapped" in seen
    assert seen["ou_not_mapped"]["seen_count"] == 1


def test_identity_upsert_and_resolve():
    _seed()
    user = identity.resolve_current_user(None)
    res = identity.upsert_identity(user["id"], "feishu", "ou_mapped")
    assert res["ok"] is True

    found = identity.resolve_by_external("feishu", "ou_mapped")
    assert found is not None and found["id"] == user["id"]

    # 同一身份绑到别人身上必须被拒（不能静默改绑）
    other = identity.create_user("小李二号", role="member")
    dup = identity.upsert_identity(other["id"], "feishu", "ou_mapped")
    assert dup["ok"] is False


# ------------------------------------------------------------
# 动作记录
# ------------------------------------------------------------


def test_action_record_has_full_attribution():
    """谁 / 针对谁 / 依据什么 / 从哪来，四个字段都不许为空。"""
    _seed()
    cid = _competitor_id(CMP_A)
    before = _counts()
    res = action_log.record("default", action_type="price_follow", asin=CMP_A,
                            description="用 Coupon 让到 16.99")
    assert res["ok"] is True

    with get_session() as s:
        row = s.get(Action, res["action_id"])
        assert row.user_id
        assert row.competitor_id == cid
        assert row.reason, "依据不能为空（未填写时也要有占位说明）"
        assert row.source == "panel"
        assert row.my_product_id


def test_action_record_on_tenant_without_users():
    """新租户第一次录动作时，会自动建默认用户——这条路径不许抛 database is locked。

    它专门覆盖「读会话里嵌套开写会话」这个坑：空库首启时才会触发，
    线上极难复现，所以必须有一条用例钉住。
    """
    _seed()
    cid = _competitor_id(CMP_A)
    res = action_log.record("t-fresh-tenant", action_type="price_follow", competitor_id=cid)
    assert res["ok"] is True, res
    assert res["user"]


def test_action_list_filters_by_user_and_asin():
    _seed()
    user = identity.resolve_current_user(None)
    action_log.record("default", action_type="price_follow", asin=CMP_A, user_id=user["id"])
    rows = action_log.list_actions("default", days=30, user_id=user["id"])
    assert rows and all(r["user_id"] == user["id"] for r in rows)
    only_a = action_log.list_actions("default", days=30, asin=CMP_A)
    assert only_a and all(r["asin"] == CMP_A for r in only_a)


def test_action_record_requires_my_product():
    """拿不到自家产品时必须报错，不能写 NULL——那会在 7 天后验证时炸。

    正常数据里 `Competitor.my_product_id` 是 NOT NULL，走不到这个分支，
    所以用 patch 把它掐掉来验证「兜底分支真的存在且返回人话错误」。
    """
    _seed()
    with get_session() as s:
        before = s.execute(select(func.count()).select_from(Action)).scalar_one()

    with mock.patch("app.services.action_log.binding.resolve_my_product", return_value=None):
        res = action_log.record("default", action_type="price_follow", asin=CMP_A)

    assert res["ok"] is False
    assert "无法确定自家产品" in res["error"]

    with get_session() as s:
        after = s.execute(select(func.count()).select_from(Action)).scalar_one()
    assert after == before, "报错时不许留下半条记录"


# ------------------------------------------------------------
# 绑定
# ------------------------------------------------------------


def test_binding_basis_snapshot_and_summary():
    """basis 必须含 5 个可复现判断的键，summary 必须是说得通的中文。"""
    _seed()
    cid = _competitor_id(CMP_A)
    res = binding.rebuild_basis("default", competitor_id=cid)
    assert res["ok"] and res["rebuilt"] == 1

    rows = binding.list_for("default", competitor_id=cid)
    assert len(rows) == 1
    basis = rows[0]["basis"]
    for key in ("price_ratio", "keyword_overlap", "node_match", "thresholds_version", "judged_at"):
        assert key in basis, f"basis 缺 {key}"
    summary = rows[0]["basis_summary"]
    assert summary and len(summary) >= 20, summary
    assert "词重合" in summary


def test_binding_resolve_my_product():
    _seed()
    cid = _competitor_id(CMP_A)
    user = identity.resolve_current_user(None)
    binding.rebuild_basis("default", competitor_id=cid, user_id=user["id"])

    resolved = binding.resolve_my_product("default", user["id"], cid)
    assert resolved is not None
    # 没有竞品时的兜底：返回租户第一个自家产品
    assert binding.resolve_my_product("default", user["id"], None) is not None


# ------------------------------------------------------------
# 回调 token（fail-closed）
# ------------------------------------------------------------


def test_callback_fails_closed_without_secret():
    _seed()
    ok, msg = action_log.verify_action_token("42.price_follow.deadbeefdeadbeef")
    assert ok is False
    assert msg == action_log.TOKEN_EMPTY_SECRET

    res = action_log.handle_callback("42.price_follow.deadbeefdeadbeef", "agree")
    assert res["ok"] is False
    assert "fail-closed" in res["error"]


def test_callback_token_roundtrip_with_secret():
    _seed()
    token = action_log.sign_action_token(42, "price_follow", secret="s3cret")
    assert action_log.verify_action_token(token, secret="s3cret") == (True, "")
    assert action_log.verify_action_token(token, secret="wrong")[0] is False
    assert action_log.verify_action_token("42.price_follow.aaaaaaaaaaaaaaaa", secret="s3cret")[0] is False
    assert action_log.verify_action_token("not-a-token", secret="s3cret")[0] is False


def test_add_competitor_uses_configured_tiering_gate():
    """入池判定必须吃配置：门槛 0.90 时 overlap=0.8 的竞品被拒，且文案数字跟着变。"""
    _seed()
    tenant = "default"
    new_asin = "B0NEW0001"
    snap = UnifiedSnapshot(
        asin=new_asin, price=20.5, net_price=20.5, price_source="promotion",
        category_path=["Home", "Kitchen", "Blenders"],     # 节点不同 → 只剩词重合这条路
        keywords=[{"keyword": "toy car", "traffic_share": 70}],   # 命中 my_core 的 80 权重 → 0.8
        monthly_units=900, in_stock=True, stock_hint="IN_STOCK",
        data_quality="full", source="test",
    )

    async def fake_fetch(self, asin, marketplace="US"):
        return snap

    def add():
        with mock.patch.object(collector.SellerSpriteAdapter, "fetch_snapshot", fake_fetch):
            return pool.add_competitor(tenant, new_asin)

    try:
        settings_store.save(
            scope="company", group_name="tiering", tenant_id=tenant,
            values={"keyword_overlap_gate": 0.90},
        )
        strict = add()
        assert strict["ok"] is False, strict
        assert "90%" in strict["reject_reason"], strict["reject_reason"]
        assert "40%" not in strict["reject_reason"], strict["reject_reason"]

        # 阈值改回默认 → 同一份数据能进池（证明差异是阈值造成的，不是数据造成的）
        settings_store.save(
            scope="company", group_name="tiering", tenant_id=tenant,
            values={"keyword_overlap_gate": 0.40},
        )
        loose = add()
        assert loose["ok"] is True, loose
        assert loose["tier"] in ("S", "A"), loose["tier"]
    finally:
        settings_store.save(
            scope="company", group_name="tiering", tenant_id=tenant,
            values={"keyword_overlap_gate": 0.40},
        )
        # 归档掉测试竞品，别影响后续按 active 统计的用例
        with get_session() as s:
            row = s.execute(
                select(Competitor).where(Competitor.asin == new_asin)
            ).scalar_one_or_none()
            if row is not None:
                row.status = "archived"


def test_pool_maintenance_uses_configured_tiering_gate():
    """周维护也要吃配置：同一份数据，门槛 0.90 → C，门槛 0.40 → S。

    注意测试里把「快照带流量词」这件事补上了（patch row_to_unified）。
    原因：`snapshots` 表没有 keywords 列，`row_to_unified` 还原出来的快照
    永远没有词 → 重合度永远是 None → 不管什么阈值都会判 C。
    也就是说今天的周维护是个「池子清道夫」（这是既有缺陷，我已单独上报，
    不在本轮修，因为修它要动 snapshots 表结构）。
    """
    _seed()
    tenant = "default"
    real_judge = tiering_mod.judge_tier
    real_convert = collector.row_to_unified
    captured: list = []

    def spy_judge(**kwargs):
        captured.append(kwargs.get("thresholds"))
        return real_judge(**kwargs)

    def convert_with_keywords(row, marketplace="US"):
        return real_convert(row, marketplace).model_copy(
            update={"keywords": [{"keyword": "toy car", "traffic_share": 70}]}
        )

    before = _tiers()
    try:
        settings_store.save(
            scope="company", group_name="tiering", tenant_id=tenant,
            values={"keyword_overlap_gate": 0.90},
        )
        with mock.patch.object(tiering_mod, "judge_tier", spy_judge), \
                mock.patch.object(collector, "row_to_unified", convert_with_keywords):
            asyncio.run(scheduler.job_pool_maintenance())

        assert captured and all(t is not None for t in captured), "周维护没把阈值传给 judge_tier"
        assert all(abs(t.keyword_overlap_gate - 0.90) < 1e-9 for t in captured)
        after_strict = _tiers()
        assert after_strict[CMP_B] == "C", f"新门槛下 B 级竞品应被踢到 C：{after_strict}"

        # 改回默认 —— 必须读到新值（防「读一次就缓存住」）
        captured.clear()
        settings_store.save(
            scope="company", group_name="tiering", tenant_id=tenant,
            values={"keyword_overlap_gate": 0.40},
        )
        with mock.patch.object(tiering_mod, "judge_tier", spy_judge), \
                mock.patch.object(collector, "row_to_unified", convert_with_keywords):
            asyncio.run(scheduler.job_pool_maintenance())

        assert all(abs(t.keyword_overlap_gate - 0.40) < 1e-9 for t in captured)
        assert _tiers()[CMP_B] == "S", "阈值恢复后应重新评上 S"
    finally:
        settings_store.save(
            scope="company", group_name="tiering", tenant_id=tenant,
            values={"keyword_overlap_gate": 0.40},
        )
        _restore_tiers(before)


def test_add_competitor_marks_unknown_when_evidence_missing():
    """缺关键词时给 unknown，不给 C：拿不到证据 ≠ 证据表明不是竞品。"""
    _seed()
    tenant = "default"
    new_asin = "B0UNK00001"
    snap = UnifiedSnapshot(
        asin=new_asin, price=20.5, net_price=20.5, price_source="promotion",
        keywords=[],                                   # 关键词慢通道没取到 → 流量池维度不可判
        monthly_units=900, in_stock=True, stock_hint="IN_STOCK",
        data_quality="partial", source="test",
    )

    async def fake_fetch(self, asin, marketplace="US"):
        return snap

    try:
        with mock.patch.object(collector.SellerSpriteAdapter, "fetch_snapshot", fake_fetch):
            res = pool.add_competitor(tenant, new_asin)

        assert res["ok"] is True, res
        assert res["tier"] == "unknown", res["tier"]
        assert res["status"] == "observing"
        assert "reject_reason" not in res, "证据不足不该给拒绝理由"

        with get_session() as s:
            row = s.execute(select(Competitor).where(Competitor.asin == new_asin)).scalar_one()
            assert row.tier == "unknown", "库里存的也必须是 unknown，不能落成 C"
    finally:
        with get_session() as s:
            row = s.execute(
                select(Competitor).where(Competitor.asin == new_asin)
            ).scalar_one_or_none()
            if row is not None:
                row.status = "archived"


def test_pool_maintenance_skips_when_evidence_missing():
    """现状回归：真实数据形态（keywords=[]、category_path=None）下，池子一个都不许改判。"""
    _seed()
    from app.services import scheduler as scheduler_mod

    before = _tiers()
    logs: list[str] = []

    def fake_info(msg, *args, **kwargs):
        logs.append(msg % args if args else msg)

    with mock.patch.object(scheduler_mod.logger, "info", fake_info):
        asyncio.run(scheduler_mod.job_pool_maintenance())

    assert _tiers() == before, f"证据不足时不许改判：{before} → {_tiers()}"
    assert any("跳过" in line for line in logs), logs
    assert any("类目路径缺失" in line for line in logs), logs


def test_pool_public_contract_unchanged():
    """pool.py 只许「加」，不许「减少返回字段」或改现有函数签名。

    它是 493 行、混了两个业务域的已知问题文件。本轮只加 set_competitor_status
    和一个 binding 钩子；这个用例是防止顺手重构的护栏。
    """
    _seed()
    items = pool.list_competitors("default")
    assert items
    expected = {
        "id", "asin", "tier", "status", "title", "price", "net_price", "rank_sub",
        "rating", "review_count", "in_stock", "price_ratio", "keyword_overlap",
        "last_checked_at", "data_quality",
    }
    assert expected <= set(items[0]), expected - set(items[0])

    try:
        paused = pool.set_competitor_status("default", CMP_B, "paused")
        assert paused["ok"] and paused["status"] == "paused"
        assert "已停止采集" in paused["effect"]
        assert pool.set_competitor_status("default", CMP_B, "nonsense")["ok"] is False
        assert pool.set_competitor_status("default", "B0NOPE", "paused")["ok"] is False
    finally:
        # 复位，别影响后面的用例（模拟/健康度都按 active 口径统计）
        pool.set_competitor_status("default", CMP_B, "active")


# ------------------------------------------------------------
# 数据健康（任何数据状态下都不许抛）
# ------------------------------------------------------------


def test_health_returns_structure_even_without_data():
    _seed()
    result = health.collect_health("t-no-such-tenant", days=7)
    assert result["ok"] is True
    assert result["rounds"]["total"] == 0
    assert result["rounds"]["success_rate"] is None
    assert result["failures"] == []
    assert result["price_trust"] == []
    assert isinstance(result["unmapped_identities"], list)

    real = health.collect_health("default", days=7)
    assert real["rounds"]["total"] > 0, "前面几个采集用例应该已经写了 collect_runs"
    assert isinstance(real["price_trust"], list) and real["price_trust"]
    # 价格可信度：net_price 为空的那些天不许补 0，必须是「未知」
    day = real["price_trust"][0]
    assert day["rounds"] == day["price_known"] + day["price_unknown"]


# ============================================================
# 运行器
# ============================================================


def main() -> int:
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    failed: list[tuple[str, str]] = []

    try:
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
    finally:
        shutil.rmtree(_TMP_DIR, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
