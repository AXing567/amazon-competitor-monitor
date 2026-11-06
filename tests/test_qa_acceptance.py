"""QA 独立验收（第二轮）：用**不同方法**证明 T01–T03，而不是重跑工程师的测试。

运行：
    .venv/Scripts/python.exe tests/test_qa_acceptance.py

## 与 `tests/test_thresholds.py` 的分工

工程师的测试证明「他实现的东西按他设想的方式工作」。本文件只做三件事：
**换方法、攻边界、找静默失败**。

| 攻击面 | 工程师怎么验的 | 本文件怎么验（不同方法） |
|---|---|---|
| 迁移幂等 | 只跑 `init_db` 看打印（`tests/` 里 `migrate` **零覆盖**） | 手工造旧版 `actions` 表 + 老数据，跑真实 `init_db`，逐列逐行核对 |
| 三层继承 | 直接断言 `resolve()` 返回值 | 额外走**试算公共 API** 读 `thresholds.current`，证明调用链真的接上了 |
| 试算零写库 | `count(*)` 前后一致 | **全表内容 tuple + MAX(rowid) + sqlite_sequence** 逐字段比对 |
| 试算自一致 | 传当前值 `delta == 0` | 造 30 天序列，**跑生产判定路径落 signals**，再与试算逐类型比对 |
| 证据不足不改判 | 只验「全部跳过」 | 补 **`decidable_dims == 2` 边界必须判** + 证据充足时必须降级 |
| L2 纯函数 | 子串扫描 `sqlalchemy`/`datetime.now()` | **AST 扫描**（能抓 `open()` / `date.today()` / 间接调用）+ 导入白名单 |

## 约定

- 只读 + 只写测试：不改 `app/` 下任何业务代码。
- 所有写操作落在临时库（`DATABASE_URL` 在导入 `app.*` 之前改掉）。
- 真实库 `data/competitor_monitor.db` 全程不碰。
- 用例名带 `test_defect_` 前缀 = **预期失败**（复现缺陷）。运行器单独归类，不算意外失败。
"""
from __future__ import annotations

import ast
import collections
import os
import shutil
import sqlite3
import sys
import tempfile
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---- 必须在 import app.* 之前（app.db 在 import 时按此建 engine）----
_TMP_DIR = Path(tempfile.mkdtemp(prefix="qa_accept_"))
_DB_FILE = _TMP_DIR / "qa_accept.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_FILE}"
os.environ["ACTION_TOKEN_SECRET"] = ""

from sqlalchemy import create_engine, func, select                          # noqa: E402
from sqlalchemy.orm import sessionmaker                                    # noqa: E402

import app.db as db_mod                                                    # noqa: E402
from app.db import get_session, init_db, engine as app_engine              # noqa: E402
from app.domain import tiering as tiering_mod                              # noqa: E402
from app.domain.diff import DiffEngine                                     # noqa: E402
from app.domain.thresholds import (                                        # noqa: E402
    DiffThresholds,
    NotifyThresholds,
    TieringThresholds,
)
from app.domain.tiering import judge_tier                                  # noqa: E402
from app.migrations import migrate, migrate_report                         # noqa: E402
import app.migrations as mod_migrations                                    # noqa: E402
from app.adapters.unified import UnifiedSnapshot                           # noqa: E402
from app.models import (                                                   # noqa: E402
    Action,
    Competitor,
    MyProduct,
    Signal,
    Snapshot,
    ThresholdConfig,
    ThresholdVersion,
)
from app.services import collector, identity, scheduler, settings_store, simulator  # noqa: E402


# ============================================================
# 公共夹具
# ============================================================

#: 迁移给 `actions` 补的**身份五列**。这是本组用例的业务意图：
#: 「谁、针对谁、依据什么、从哪来」少一个，动作记录就不完整。
#: 抽成常量，免得几个用例各抄一遍抄错。
_FIVE_NEW_COLUMNS = frozenset({"user_id", "competitor_id", "signal_id", "reason", "source"})


def _actions_columns_from_migration() -> set[str]:
    """`ADDITIVE_COLUMNS` 里所有针对 `actions` 的列。

    ## 为什么断言数量要派生，不能写死

    第一版这里写的是 `assert len(applied) == 5`。它在本轮加
    `reviewed_at` / `review_note` 时红了——**代码是对的，红的是测试**。
    一个只在「别人合法地加了一列」时才失败的测试，会训练人见到红就改数字，
    然后真正该报警的那次也顺手改了。

    派生之后：加列不用改测试；但「加了列却没进迁移清单」仍然会被抓住，
    因为老库上 `migrate()` 出来的列数会少于模型声明的列数。
    """
    from app.migrations import ADDITIVE_COLUMNS

    return {col for table, col, _ in ADDITIVE_COLUMNS if table == "actions"}

_DB_READY = False


def db() -> None:
    """建库一次（幂等）。所有需要用 app engine 的用例先调它。"""
    global _DB_READY
    if not _DB_READY:
        init_db()
        _DB_READY = True


def _raw_engine(filename: str):
    """独立于 app.db 的裸 engine，给迁移用例造「老库」用。"""
    return create_engine(f"sqlite:///{_TMP_DIR / filename}")


def _table_info(engine, table: str) -> list[tuple]:
    with engine.connect() as conn:
        return conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()


def _sqlite_master(engine, kind: str, table: str) -> list[str]:
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(
            f"SELECT name FROM sqlite_master WHERE type='{kind}' AND tbl_name='{table}'"
        ).fetchall()
    return sorted(r[0] for r in rows)


_OLD_ACTIONS_DDL = """
CREATE TABLE actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id VARCHAR(64) NOT NULL,
  my_product_id INTEGER NOT NULL,
  decision_id INTEGER,
  action_type VARCHAR(32) NOT NULL,
  description TEXT DEFAULT '',
  executed_at DATETIME,
  baseline_metrics JSON,
  verify_due_at DATETIME,
  result_metrics JSON,
  verdict VARCHAR(16)
);
"""


def _make_old_db(filename: str, rows: int = 2) -> tuple:
    """造一个「只有旧表结构 + 有历史数据」的库。返回 (engine, 原始行)。"""
    path = _TMP_DIR / filename
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_ACTIONS_DDL)
    original = []
    for i in range(rows):
        values = ("default", 1, f"price_cut_{i}", f"老数据-{i}", f"2026-01-{i + 1:02d} 10:00:00")
        conn.execute(
            "INSERT INTO actions (tenant_id, my_product_id, action_type, description, executed_at)"
            " VALUES (?,?,?,?,?)",
            values,
        )
        original.append(values)
    conn.commit()
    conn.close()
    return create_engine(f"sqlite:///{path}"), original


# ============================================================
# A. 迁移的幂等性
# ============================================================


def test_01_migrate_adds_all_five_columns_and_keeps_data():
    """老库（无新列、有 2 行数据）→ migrate：列全加上、数据一字不动、索引建好。

    ## 断言为什么从「恰好 5 个」改成「从迁移清单派生」

    第一版写的是 `new_cols - before_cols == {五个列名}`。这断言在**每次给
    `actions` 加合法新列时都会红**——而那时代码是对的，红的是测试。
    红了的测试只有两种结局：改断言、或者被人加 `-k "not test_01"` 跳过。
    两条路都会让这个测试在真正该报警的时候已经不可信了。

    所以改成：核心五列必须在内（这是本测试的业务意图），
    总数从 `ADDITIVE_COLUMNS` 派生。加列时不用改这里，
    但「加了列却没进迁移清单」仍然会被抓住（`new_cols - before_cols` 会超出 expected）。
    """
    from app.migrations import ADDITIVE_COLUMNS

    engine, original = _make_old_db("old_add.db", rows=2)

    before_cols = {r[1] for r in _table_info(engine, "actions")}
    assert before_cols == {
        "id", "tenant_id", "my_product_id", "decision_id", "action_type",
        "description", "executed_at", "baseline_metrics", "verify_due_at",
        "result_metrics", "verdict",
    }

    expected = {col for table, col, _ in ADDITIVE_COLUMNS if table == "actions"}
    # 业务意图：这五列是「谁、针对谁、依据什么、从哪来」，少一个动作记录就不完整
    assert {"user_id", "competitor_id", "signal_id", "reason", "source"} <= expected

    applied = migrate(engine)
    new_cols = {r[1] for r in _table_info(engine, "actions")}
    assert new_cols - before_cols == expected
    assert len(applied) == len(expected), applied
    assert all("[added]" in a for a in applied), applied

    # 类型也要对（不是「有这列就行」）
    types = {r[1]: (r[2] or "").upper() for r in _table_info(engine, "actions")}
    assert types["user_id"] == "INTEGER"
    assert types["competitor_id"] == "INTEGER"
    assert types["signal_id"] == "INTEGER"
    assert types["reason"] == "TEXT"
    assert types["source"] == "VARCHAR(16)"
    assert types["reviewed_at"] == "DATETIME"
    assert types["review_note"] == "TEXT"

    # 数据零丢失：老行的每个字段都要原样在
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT tenant_id, my_product_id, action_type, description, executed_at"
            " FROM actions ORDER BY id"
        ).fetchall()
    assert [tuple(r) for r in rows] == original, (rows, original)

    # 索引真的建出来了（工程师只保证列，索引是另一条路径）
    assert "ix_action_user_time" in _sqlite_master(engine, "index", "actions")


def test_02_migrate_is_idempotent_and_does_not_touch_rows():
    """连跑 3 次：第 2/3 次必须返回空列表，且字段值不变。"""
    engine, original = _make_old_db("old_idem.db", rows=3)
    assert len(migrate(engine)) == len(_actions_columns_from_migration())
    assert migrate(engine) == []
    assert migrate(engine) == []

    with engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT tenant_id, my_product_id, action_type, description, executed_at"
            " FROM actions ORDER BY id"
        ).fetchall()
    assert [tuple(r) for r in rows] == original

    # 新列必须是 NULL（不能凭空给个默认值，那是静默造假数据）
    with engine.connect() as conn:
        nulls = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM actions WHERE user_id IS NULL AND source IS NULL"
        ).scalar()
    assert nulls == 3


def test_03_init_db_upgrades_old_db_without_data_loss():
    """真实入口 `init_db()` 在老库上的表现：先 create_all（6 张新表）再 migrate（5 列）。

    这是最贴近线上的一次演练——所有老库升级都走这条路径。
    """
    engine, original = _make_old_db("old_initdb.db", rows=2)
    real_engine, real_session = db_mod.engine, db_mod.SessionLocal
    db_mod.engine = engine
    db_mod.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        first = init_db()
        second = init_db()
        third = init_db()
    finally:
        db_mod.engine, db_mod.SessionLocal = real_engine, real_session

    assert len(first) == len(_actions_columns_from_migration()), first
    assert second == [] and third == [], (second, third)

    cols = {r[1] for r in _table_info(engine, "actions")}
    assert {"user_id", "competitor_id", "signal_id", "reason", "source"} <= cols

    with engine.connect() as conn:
        tables = {
            r[0]
            for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
        }
        rows = conn.exec_driver_sql(
            "SELECT tenant_id, my_product_id, action_type, description, executed_at"
            " FROM actions ORDER BY id"
        ).fetchall()
    assert {
        "users", "user_identities", "threshold_configs", "threshold_versions",
        "competitor_bindings", "collect_runs",
    } <= tables, tables
    assert [tuple(r) for r in rows] == original


def test_04_init_db_on_fresh_db_reports_nothing_applied():
    """全新库：create_all 直接建全列 → 迁移无事可做，三次都返回 []。"""
    db()
    assert init_db() == []
    assert init_db() == []
    cols = {r[1] for r in _table_info(app_engine, "actions")}
    assert {"user_id", "competitor_id", "signal_id", "reason", "source"} <= cols


def test_05_migrate_silently_skips_column_with_different_type():
    """【把「静默跳过」钉成断言】列已存在但类型不同 → 迁移不报错、也不修正。

    这不是「通过即安全」。SQLite 动态类型让它能跑，但 `user_id` 里可能存着 'abc'，
    而模型声明的是 Integer。本用例的作用是：一旦哪天 `applied` 里开始出现
    `user_id [added]`（说明有人去掉了「列存在即跳过」的保护），这里立刻红。
    """
    path = _TMP_DIR / "old_type.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE actions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id VARCHAR(64) NOT NULL,
          my_product_id INTEGER NOT NULL,
          action_type VARCHAR(32) NOT NULL,
          executed_at DATETIME,
          user_id TEXT
        );
        INSERT INTO actions (tenant_id, my_product_id, action_type, executed_at, user_id)
        VALUES ('default', 1, 'x', '2026-01-01', 'abc');
        """
    )
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{path}")
    applied = migrate(engine)

    assert not any("user_id" in a for a in applied), f"类型不一致被静默跳过：{applied}"
    types = {r[1]: (r[2] or "").upper() for r in _table_info(engine, "actions")}
    assert types["user_id"] == "TEXT", "迁移不会也不能改列类型"
    with engine.connect() as conn:
        stored = conn.exec_driver_sql("SELECT user_id, typeof(user_id) FROM actions").fetchall()
    assert stored == [("abc", "text")], stored


def test_06_migrated_old_db_accepts_orm_insert_with_new_columns():
    """迁移之后必须**真的能用**：通过 ORM 往老库写一条带 user_id / source 的 action。

    只看 `PRAGMA table_info` 有这 5 列是不够的——列在但 ORM 写不进去（类型/默认值/顺序
    对不上）同样会让 T03 的 action_log 在老库上直接 500。这条走完整写入-读回。
    """
    engine, _rows = _make_old_db("old_orm.db", rows=1)
    real_engine, real_session = db_mod.engine, db_mod.SessionLocal
    db_mod.engine = engine
    db_mod.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        init_db()
        session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
        try:
            session.add(
                Action(
                    tenant_id="default",
                    my_product_id=1,
                    action_type="price_cut",
                    description="迁移后写入",
                    user_id=7,
                    competitor_id=2,
                    signal_id=3,
                    reason="对手降价 8%",
                    source="panel",
                )
            )
            session.commit()
            row = session.execute(
                select(Action).where(Action.description == "迁移后写入")
            ).scalar_one()
            assert (row.user_id, row.competitor_id, row.signal_id) == (7, 2, 3)
            assert row.reason == "对手降价 8%" and row.source == "panel"
        finally:
            session.close()
    finally:
        db_mod.engine, db_mod.SessionLocal = real_engine, real_session


def test_fixed_03_migration_rolls_back_entirely_when_it_fails():
    """【D2 已修】迁移中途失败 = 一个列都不留（要么全成、要么全不成）。

    修法：pysqlite 只在 DML 前隐式 BEGIN、DDL 走 autocommit，所以
    `engine.begin()` 包不住 `ALTER TABLE`。迁移改为自己管事务：
    `isolation_level=None` → `BEGIN IMMEDIATE` → 全部 DDL → `COMMIT`，
    任何异常 `ROLLBACK` 并原样抛出；`applied` 只在 COMMIT 成功后才返回。

    验证方式：往 `IDEMPOTENT_INDEXES` 尾部临时塞一条必然失败的 DDL（列不存在），
    让它排在 5 个 ADD COLUMN **之后**执行。
    修之前：5 个列留下、库半新半旧、下次重跑继续崩在同一处。
    修之后：整体回滚，表结构与迁移前逐列一致，且下一次重跑能正常补齐。
    """
    path = _TMP_DIR / "atomic_fixed.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE actions (id INTEGER PRIMARY KEY, tenant_id TEXT, executed_at TEXT);"
    )
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{path}")
    before = {r[1] for r in _table_info(engine, "actions")}

    original = list(mod_migrations.IDEMPOTENT_INDEXES)
    mod_migrations.IDEMPOTENT_INDEXES = original + [
        ("qa_bad_index", "actions", (), "CREATE INDEX qa_bad_index ON actions(no_such_col)")
    ]
    raised = None
    try:
        try:
            migrate_report(engine)
        except Exception as exc:            # noqa: BLE001 —— 就是要它抛
            raised = exc
    finally:
        mod_migrations.IDEMPOTENT_INDEXES = original

    assert raised is not None, "失败 DDL 应当把异常原样抛给调用方"
    after = {r[1] for r in _table_info(engine, "actions")}
    assert after == before, f"失败后留下了半迁移状态：{sorted(after - before)}"
    assert not (_FIVE_NEW_COLUMNS & after), "失败后不该有任何新列落库"

    # 失败可重入：坏 DDL 拿掉后必须能一次补齐
    report = migrate_report(engine)
    assert len(report["applied"]) == len(_actions_columns_from_migration()), report
    assert _FIVE_NEW_COLUMNS <= {r[1] for r in _table_info(engine, "actions")}
    engine.dispose()


def test_fixed_01_migrate_is_a_noop_on_empty_db():
    """【D1 已修】空库调 `migrate()` 什么都不做，且把「为什么跳过」写进 `skipped`。

    旧行为：索引 DDL 无条件执行 → `no such table: main.actions`。
    新行为：建索引前先用 `PRAGMA table_info` 查表与列，缺就跳过并记录原因。
    """
    engine = _raw_engine("empty_fixed.db")
    assert _table_info(engine, "actions") == []

    report = migrate_report(engine)        # 修之前这里抛 OperationalError
    assert report["applied"] == []
    assert any("actions" in s for s in report["skipped"]), report["skipped"]
    assert any("ix_action_user_time" in s for s in report["skipped"]), report["skipped"]
    # 空库上连跑两次也必须稳
    assert migrate(engine) == []
    engine.dispose()


def test_fixed_02_migrate_skips_index_when_column_missing():
    """【D1 已修】actions 在但缺 `executed_at` → 列照加、索引跳过、不崩。

    旧行为：`no such column: executed_at`。
    新行为：5 个列正常补上，索引进 `skipped` 并点名缺哪个列；
    再跑一次 `applied == []`（幂等）。
    """
    path = _TMP_DIR / "thin_actions_fixed.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE actions (id INTEGER PRIMARY KEY, tenant_id TEXT);")
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{path}")
    report = migrate_report(engine)        # 修之前这里抛 no such column
    assert len(report["applied"]) == len(_actions_columns_from_migration()), report["applied"]
    assert _FIVE_NEW_COLUMNS <= {r[1] for r in _table_info(engine, "actions")}
    assert any(
        "ix_action_user_time" in s and "executed_at" in s for s in report["skipped"]
    ), report["skipped"]

    assert migrate(engine) == [], "第二次不该再应用任何动作"
    engine.dispose()


# ============================================================
# B. 阈值三层继承的真实生效顺序
# ============================================================

_LAYER_TENANT = "qa-layer"
_LAYER = {}


def _layer_fixture() -> dict:
    """company=0.05 / user=0.03 / product=0.08 三层都设上。"""
    if _LAYER:
        return _LAYER
    db()
    with get_session() as s:
        my = MyProduct(tenant_id=_LAYER_TENANT, asin="QALAYER01", marketplace="US",
                       current_price=20.0, daily_sales_avg=30.0)
        s.add(my)
        s.flush()
        my_id = int(my.id)
    user = identity.ensure_default_user(_LAYER_TENANT)
    uid = int(user["id"])

    settings_store.save(scope="company", group_name="diff", tenant_id=_LAYER_TENANT,
                        values={"price_p0_pct": 0.05})
    settings_store.save(scope="user", scope_id=str(uid), group_name="diff",
                        tenant_id=_LAYER_TENANT, values={"price_p0_pct": 0.03})
    settings_store.save(scope="product", scope_id=str(my_id), group_name="diff",
                        tenant_id=_LAYER_TENANT, values={"price_p0_pct": 0.08})
    _LAYER.update(my_id=my_id, uid=uid)
    return _LAYER


def _sim_current_p0(tenant: str, my_id: int, uid: int | None) -> float:
    """走**试算公共 API** 读当前生效阈值——证明调用链真的接上了，而不只是 resolve() 对。"""
    res = simulator.simulate_diff_thresholds(
        tenant, None, days=1, my_product_id=my_id, user_id=uid
    )
    assert res["ok"] is True, res
    return res["thresholds"]["current"]["price_p0_pct"]


def test_10_three_layer_product_wins_for_both_collect_and_panel():
    """product 优先级最高：采集（不传 user）与面板试算（传 user）都必须拿到 0.08。"""
    fx = _layer_fixture()
    tenant, my_id, uid = _LAYER_TENANT, fx["my_id"], fx["uid"]

    # 采集路径：user_id=None
    assert settings_store.resolve_diff_thresholds(tenant, my_id).price_p0_pct == 0.08
    # 面板路径：传 user
    assert settings_store.resolve_diff_thresholds(tenant, my_id, user_id=uid).price_p0_pct == 0.08
    # 同一结论要能从试算 API 读到（否则说明试算没接 resolve）
    assert _sim_current_p0(tenant, my_id, uid) == 0.08
    assert _sim_current_p0(tenant, my_id, None) == 0.08

    _values, sources = settings_store.resolve(tenant, user_id=uid, my_product_id=my_id)
    assert sources["diff"]["price_p0_pct"] == "product"


def test_11_after_removing_product_layer_user_wins_on_panel_company_on_collect():
    """删掉 product 层：面板（传 user）= 0.03，采集（无 user）= 0.05。"""
    fx = _layer_fixture()
    tenant, my_id, uid = _LAYER_TENANT, fx["my_id"], fx["uid"]

    # 用公开 save(replace=True, values={}) 清空该层，而不是直接 delete 行
    cleaned = settings_store.save(
        scope="product", scope_id=str(my_id), group_name="diff",
        tenant_id=tenant, values={}, replace=True,
    )
    assert cleaned["ok"] is True, cleaned
    assert cleaned["removed_keys"] == ["price_p0_pct"], cleaned["removed_keys"]

    assert settings_store.resolve_diff_thresholds(tenant, my_id).price_p0_pct == 0.05
    assert settings_store.resolve_diff_thresholds(tenant, my_id, user_id=uid).price_p0_pct == 0.03
    assert _sim_current_p0(tenant, my_id, uid) == 0.03
    assert _sim_current_p0(tenant, my_id, None) == 0.05

    _values, sources = settings_store.resolve(tenant, user_id=uid, my_product_id=my_id)
    assert sources["diff"]["price_p0_pct"] == "user"


def test_12_after_removing_user_layer_company_wins_and_default_as_last_resort():
    """再删 user 层 → 只剩 company 0.05；最后删 company → 回到默认 0.03。"""
    fx = _layer_fixture()
    tenant, my_id, uid = _LAYER_TENANT, fx["my_id"], fx["uid"]

    settings_store.save(scope="product", scope_id=str(my_id), group_name="diff",
                        tenant_id=tenant, values={}, replace=True)
    settings_store.save(scope="user", scope_id=str(uid), group_name="diff",
                        tenant_id=tenant, values={}, replace=True)

    assert settings_store.resolve_diff_thresholds(tenant, my_id, user_id=uid).price_p0_pct == 0.05
    assert _sim_current_p0(tenant, my_id, uid) == 0.05

    settings_store.save(scope="company", group_name="diff", tenant_id=tenant,
                        values={}, replace=True)
    assert settings_store.resolve_diff_thresholds(tenant, my_id, user_id=uid).price_p0_pct == 0.03
    assert _sim_current_p0(tenant, my_id, uid) == 0.03
    _values, sources = settings_store.resolve(tenant, user_id=uid, my_product_id=my_id)
    assert sources["diff"]["price_p0_pct"] == "default"


def test_13_cross_group_key_in_db_is_filtered_on_read():
    """脏数据防线：diff 的键被写进 tiering 行（手工改库/旧代码）时，读路径必须过滤掉。

    不过滤的后果是「改了个跟分组无关的值，另一个分组静默变了」——最难查的一类。
    """
    db()
    tenant = "qa-dirty"
    with get_session() as s:
        s.add(
            ThresholdConfig(
                tenant_id=tenant, scope="company", scope_id=tenant,
                group_name="tiering",
                values={"price_p0_pct": 0.99, "overlap_a": 0.55},   # 前一个键不属于 tiering
                version=1,
            )
        )

    values, _ = settings_store.resolve(tenant)
    assert values["tiering"]["overlap_a"] == 0.55, "本组的键不能被误过滤"
    assert values["tiering"].get("price_p0_pct") is None, "跨组键必须被丢弃"
    assert values["diff"]["price_p0_pct"] == 0.03, "diff 分组不许被污染"


def test_14_numeric_string_in_db_is_normalized_to_float_on_read():
    """历史数据里可能有 "0.05" 字符串（手工改库）。读路径必须归一成 float。

    归一失败的后果：`getattr(th, key) * scale` 抛 TypeError，采集整轮失败。
    """
    db()
    tenant = "qa-strnum"
    with get_session() as s:
        s.add(
            ThresholdConfig(
                tenant_id=tenant, scope="company", scope_id=tenant,
                group_name="diff", values={"price_p0_pct": "0.05"}, version=1,
            )
        )

    values, _ = settings_store.resolve(tenant)
    value = values["diff"]["price_p0_pct"]
    assert isinstance(value, float), f"未归一成 float：{value!r} ({type(value)})"
    assert abs(value - 0.05) < 1e-12

    th = settings_store.resolve_diff_thresholds(tenant)
    assert isinstance(th.price_p0_pct, float)


# ============================================================
# C. 试算零写库：全表内容 + rowid 水位
# ============================================================

_QA_TENANT = "qa-sim"
_SIM: dict = {}
_DAYS = 30

#: 30 天脚本化序列。刻意让「3%~5% 的价格变动」出现（day 23 = -4%），
#: 这样「试算若偷用了默认 0.03 而不是配置 0.05」一定会被计数差暴露出来。
_EVENTS = {
    2: {"title": "toy car A v2"},                                   # P2 标题
    3: {"rank_sub": 1400},                                          # P0 名次崩 (+40%)
    5: {"price": 19.0, "net_price": 19.0},                          # 20→19 = -5%
    6: {"price": 19.2, "net_price": 19.2},                          # +1.05%
    7: {"review_count": 160},                                       # +60 → P0 评论暴涨
    9: {"image_url": "img/b.jpg"},                                  # P1 主图
    10: {"rating": 4.3},                                            # -0.3 → P1 星级
    11: {"coupon_value": 1.2},                                      # 券 +1.2/19.2 = 6.25% → P0
    12: {"price": 21.0, "net_price": 21.0},                         # +9.4% → P1 涨价
    14: {"in_stock": False, "stock_hint": "OOS"},                   # P0 断货
    15: {"rank_sub": 700},                                          # -50% → P1 冲榜
    17: {"in_stock": True, "stock_hint": "IN_STOCK"},               # P1 恢复上架
    18: {"review_count": 155},                                      # -5 → P2 评论减少
    20: {"price": 19.5, "net_price": 19.5},                         # -7.1% → P0
    22: {"rank_sub": 900},
    23: {"price": 18.72, "net_price": 18.72},                       # -4%：0.03→P0 / 0.05→P1 分水岭
    25: {"rating": 3.9},                                            # 跌破 4.0 → P0
    27: {"price": 20.5, "net_price": 20.5},
}


def _series(price_offset: float = 0.0) -> list[dict]:
    """30 天脚本化序列。`price_offset` 整体平移价格，百分比变动保持不变。"""
    state = {
        "price": 20.0, "net_price": 20.0, "coupon_value": 0.0, "deal_type": None,
        "rank_sub": 1000, "rating": 4.6, "review_count": 100,
        "in_stock": True, "stock_hint": "IN_STOCK",
        "title": "toy car A", "image_url": "img/a.jpg",
    }
    out = []
    for i in range(_DAYS):
        if i in _EVENTS:
            state = {**state, **_EVENTS[i]}
        row = dict(state)
        if price_offset:
            row["price"] = round(row["price"] + price_offset, 2)
            row["net_price"] = round(row["net_price"] + price_offset, 2)
        out.append({"i": i, **row})
    return out


def _ensure_sim_data() -> dict:
    """30 天序列 + 两个竞品（S 级走「S/A 的 P1 升 P0」分支，C 级不走）。

    放两个竞品是为了让「生产回放 vs 试算」的比对覆盖**多条分支**；
    如果只放 S 级，阈值 0.03/0.05 的差异会被 S/A 的升档规则掩盖（见 test_31）。
    """
    if _SIM:
        return _SIM
    db()
    with get_session() as s:
        my = MyProduct(tenant_id=_QA_TENANT, asin="QAMINE001", marketplace="US",
                       current_price=20.0, daily_sales_avg=30.0,
                       core_keywords={"toy car": 80.0, "kids gift": 20.0})
        s.add(my)
        s.flush()
        my_id = int(my.id)

        anchor = datetime.now()
        for asin, tier, offset in (("QACMP0001", "S", 0.0), ("QACMP0002", "C", 5.0)):
            comp = Competitor(tenant_id=_QA_TENANT, my_product_id=my_id, asin=asin,
                              marketplace="US", tier=tier, status="active",
                              title=f"qa competitor {asin}")
            s.add(comp)
            s.flush()
            for row in _series(offset):
                i = row.pop("i")
                s.add(
                    Snapshot(
                        competitor_id=int(comp.id),
                        captured_at=anchor - timedelta(days=(_DAYS - 1 - i)),
                        price=row["price"], net_price=row["net_price"],
                        coupon_value=row["coupon_value"], deal_type=row["deal_type"],
                        rank_sub=row["rank_sub"], rating=row["rating"],
                        review_count=row["review_count"], in_stock=row["in_stock"],
                        stock_hint=row["stock_hint"], title=row["title"],
                        image_url=row["image_url"],
                        data_quality="full", source="qa",
                    )
                )

    # 配置**偏离默认值**：只有真的吃配置，生产与试算才会一致。
    settings_store.save(scope="company", group_name="diff", tenant_id=_QA_TENANT,
                        values={"price_p0_pct": 0.05})
    # 冷却归零：让「判定路径」与「冷却去重」解耦（生产冷却用 now()，历史回放不可比）。
    settings_store.save(scope="company", group_name="notify", tenant_id=_QA_TENANT,
                        values={"cooldown_p0_hours": 0, "cooldown_p1_hours": 0,
                                "cooldown_p2_hours": 0})

    _SIM.update(tenant=_QA_TENANT, my_id=my_id, anchor=anchor)
    return _SIM


_SIM_TABLES = ("snapshots", "signals", "threshold_configs", "threshold_versions")


def _content_snapshot() -> dict:
    """全表内容快照：排序后的 tuple 列表 + MAX(rowid) + sqlite_sequence。

    为什么 count(*) 不够（工程师的验法）：
      · DELETE 一行再 INSERT 一行，count 不变，内容全变；
      · 试算期间插入又删除，count 不变，但 rowid 已经前进——下次 ID 会跳号；
      · 阈值版本被改写（update 而不是 insert），count 同样不变。
    """
    out: dict = {}
    with app_engine.connect() as conn:
        for table in _SIM_TABLES:
            cols = [r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({table})")]
            rows = conn.exec_driver_sql(f"SELECT * FROM {table} ORDER BY id").fetchall()
            out[table] = [tuple(r) for r in rows]
            out[f"{table}#max_rowid"] = conn.exec_driver_sql(
                f"SELECT COALESCE(MAX(rowid), 0) FROM {table}"
            ).scalar()
            out[f"{table}#count"] = conn.exec_driver_sql(
                f"SELECT COUNT(*) FROM {table}"
            ).scalar()
        # AUTOINCREMENT 表才会有 sqlite_sequence；本项目不用 AUTOINCREMENT，
        # 所以它是「有则比、无则跳过」，不是必需项。
        try:
            out["sqlite_sequence"] = [
                tuple(r)
                for r in conn.exec_driver_sql(
                    "SELECT name, seq FROM sqlite_sequence ORDER BY name"
                )
            ]
        except Exception:  # noqa: BLE001
            out["sqlite_sequence"] = None
    return out


def test_20_simulate_writes_nothing_content_level():
    """试算前后：4 张表**逐行内容**、MAX(rowid)、sqlite_sequence 必须完全一致。"""
    fx = _ensure_sim_data()
    before = _content_snapshot()

    # 三种不同入参，覆盖「有无覆盖值 / 不同窗口 / 指定 ASIN」三条分支
    r1 = simulator.simulate_diff_thresholds(
        fx["tenant"], None, days=30, my_product_id=fx["my_id"]
    )
    r2 = simulator.simulate_diff_thresholds(
        fx["tenant"], {"diff": {"price_p0_pct": 0.02}}, days=30, my_product_id=fx["my_id"]
    )
    r3 = simulator.simulate_diff_thresholds(
        fx["tenant"], {"price_p0_pct": 0.10}, days=7, asin="QACMP0001",
        my_product_id=fx["my_id"],
    )
    assert r1["ok"] and r2["ok"] and r3["ok"]

    after = _content_snapshot()
    diff = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
    assert not diff, f"试算改了库：{diff}"


# ============================================================
# D. 试算的自一致性：必须与生产走同一条判定路径
# ============================================================


def _replay_production(fx: dict) -> None:
    """用**生产代码**（DiffEngine + is_oos_confirmed + _save_signals）回放该租户
    全部 active 竞品的历史，把信号落进 signals 表。

    冷却配置为 0，所以与「原始命中数」可比。
    """
    tenant, my_id = fx["tenant"], fx["my_id"]
    thresholds = settings_store.resolve_diff_thresholds(tenant, my_id)
    notify_th = settings_store.resolve_notify_thresholds(tenant, my_id)

    with get_session() as s:
        comps = list(
            s.execute(
                select(Competitor).where(
                    Competitor.tenant_id == tenant,
                    Competitor.status != "archived",
                )
            ).scalars()
        )
        assert len(comps) >= 2, "本用例要求多竞品，才能覆盖不同 tier 分支"

        for comp in comps:
            cid = int(comp.id)
            rows = list(
                s.execute(
                    select(Snapshot)
                    .where(Snapshot.competitor_id == cid)
                    .order_by(Snapshot.captured_at)
                ).scalars()
            )
            assert len(rows) >= 2
            unified = [collector.row_to_unified(r, "US") for r in rows]
            for prev, curr in zip(unified, unified[1:]):
                engine = DiffEngine(phase="warmup", thresholds=thresholds)
                drafts = engine.compare(
                    prev, curr, tier=comp.tier or "C", my_price=20.0,
                    oos_confirmed=collector.is_oos_confirmed(prev),
                )
                drafts = [
                    d for d in drafts
                    if not collector._is_cooling_down(s, cid, d, cooldown=notify_th)
                ]
                collector._save_signals(s, tenant, cid, drafts)


def _stored_signal_pairs(fx: dict) -> collections.Counter:
    with get_session() as s:
        rows = s.execute(
            select(Signal.signal_type, Signal.level).where(Signal.tenant_id == fx["tenant"])
        ).all()
    return collections.Counter((r[0], r[1]) for r in rows)


def _sim_timeline_pairs(fx: dict) -> collections.Counter:
    """白盒：直接从试算的 timeline 取 (类型, 等级) 多重集做逐条比对。"""
    thresholds = settings_store.resolve_diff_thresholds(fx["tenant"], fx["my_id"])
    now = datetime.now()
    with get_session() as s:
        series = simulator._load_series(
            s, fx["tenant"], asin=None,
            window_from=now - timedelta(days=_DAYS + 1),
            window_to=now + timedelta(minutes=1),
        )
    evaluated = simulator._evaluate(series, thresholds)
    return collections.Counter((d.signal_type, d.level) for _a, _t, d in evaluated["timeline"])


def test_30_simulate_matches_production_signal_for_signal():
    """根基层：用当前生效阈值跑试算，结果必须与生产写进 signals 的**逐条对得上**。

    这条是整个功能可信度的地基。试算与生产哪怕只差一条阈值路径，
    用户就会看到「试算说 3 次、实际报 12 次」，从此不信这个功能。
    """
    fx = _ensure_sim_data()
    _replay_production(fx)

    stored = _stored_signal_pairs(fx)
    assert sum(stored.values()) > 10, f"序列太安静，测不出问题：{stored}"

    sim_pairs = _sim_timeline_pairs(fx)
    assert sim_pairs == stored, (
        "试算与生产判定不一致\n"
        f"  只在生产: {stored - sim_pairs}\n"
        f"  只在试算: {sim_pairs - stored}"
    )

    res = simulator.simulate_diff_thresholds(
        fx["tenant"], None, days=_DAYS + 1, my_product_id=fx["my_id"]
    )
    assert res["ok"] is True
    assert res["window"]["pairs"] == (_DAYS - 1) * 2, res["window"]
    assert res["window"]["competitors"] == 2, res["window"]

    by_level = collections.Counter()
    for (_t, level), n in stored.items():
        by_level[level.lower()] += n
    for level in ("p0", "p1", "p2"):
        assert res["current"][level] == by_level[level], (
            f"{level} 计数不一致：试算 {res['current'][level]} vs 生产 {by_level[level]}"
        )
    assert res["current"]["total"] == sum(stored.values())

    # 冷却为 0，所以「去重后」必须等于原始命中数（两侧都不许偷偷去重）
    assert res["after_cooldown"]["current"]["total"] == res["current"]["total"]
    assert res["delta"]["total"] == 0, "传当前生效值，delta 必须为 0"


def test_31_simulate_is_sensitive_to_the_configured_value():
    """反证：如果试算偷用默认值（0.03）而不是配置值（0.05），计数必须不同。

    没有这条，上一条可能因为「序列里恰好没有 3%~5% 的变动」而假通过。
    """
    fx = _ensure_sim_data()
    base = simulator.simulate_diff_thresholds(
        fx["tenant"], None, days=_DAYS + 1, my_product_id=fx["my_id"]
    )
    assert abs(base["thresholds"]["current"]["price_p0_pct"] - 0.05) < 1e-12

    # 默认值 0.03 应比配置值 0.05 更敏感 → P0 更多（day 23 的 -4% 会从 P1 升成 P0）
    as_default = simulator.simulate_diff_thresholds(
        fx["tenant"], {"diff": {"price_p0_pct": 0.03}}, days=_DAYS + 1,
        my_product_id=fx["my_id"],
    )
    assert as_default["candidate"]["p0"] > base["current"]["p0"], (
        f"试算对阈值不敏感：0.03 → {as_default['candidate']} / 0.05 → {base['current']}"
    )
    assert as_default["delta"]["p0"] >= 1

    # 收紧到 0.5 → 价格类 P0 必须被降档。
    # 注意不能用 total 判断：价格只要过了噪声门槛就一定有信号，收紧只会改变**等级**，
    # 不会让它消失。这正是「只看总数会看漏」的一个活例子。
    loose = simulator.simulate_diff_thresholds(
        fx["tenant"], {"diff": {"price_p0_pct": 0.5}}, days=_DAYS + 1,
        my_product_id=fx["my_id"],
    )
    assert loose["candidate"]["p0"] < base["current"]["p0"], (
        f"收紧 P0 门槛后 P0 未减少：{loose['candidate']} vs {base['current']}"
    )
    assert loose["candidate"]["p1"] > base["current"]["p1"], "被降档的信号应落到 P1"


def test_32_simulate_delta_zero_even_when_values_arrive_as_strings():
    """「传当前生效值 delta 全 0」的加压版：值以字符串形式传进来也必须 delta 0。

    面板表单提交全是字符串。如果 `_coerce` 漏一个，这里会变成「没改也报会变」。
    """
    fx = _ensure_sim_data()
    current = simulator.simulate_diff_thresholds(
        fx["tenant"], None, days=_DAYS + 1, my_product_id=fx["my_id"]
    )["thresholds"]["current"]

    as_strings = {k: str(v) for k, v in current.items() if v is not None}
    res = simulator.simulate_diff_thresholds(
        fx["tenant"], {"diff": as_strings}, days=_DAYS + 1, my_product_id=fx["my_id"]
    )
    assert res["ok"] is True, res
    assert res["delta"]["total"] == 0, f"字符串入参导致 delta 漂移：{res['delta']}"
    assert res["thresholds"]["changed_keys"], "覆盖键应被识别（否则上面是空转）"


def test_33_cooldown_window_is_honored_in_both_directions():
    """冷却窗口两侧都要对：刚进窗口内 → 抑制；出窗口 → 放行。

    刻意避开「恰好等于窗口」的边界：**R3 已修**，两侧现在统一到「抑制」侧
    （生产 `detected_at >= now-window`，试算 `(at-last) <= window`），
    边界语义不会再相反；但这个用例本来就只想验两侧的粗粒度行为，
    继续避开精确边界可以让它对时钟抖动免疫。
    """
    db()
    with get_session() as s:
        my = MyProduct(tenant_id="qa-cool", asin="QACOOL001", marketplace="US",
                       current_price=20.0)
        s.add(my)
        s.flush()
        comp = Competitor(tenant_id="qa-cool", my_product_id=int(my.id), asin="QACOOLC01",
                          marketplace="US", tier="S", status="active")
        s.add(comp)
        s.flush()
        cid = int(comp.id)

    notify = NotifyThresholds(cooldown_p0_hours=4, cooldown_p1_hours=12, cooldown_p2_hours=48)

    def make_draft():
        return DiffEngine().compare(
            UnifiedSnapshot(asin="x", price=20.0, net_price=20.0, price_source="promotion",
                            in_stock=True, stock_hint="IN_STOCK"),
            UnifiedSnapshot(asin="x", price=18.0, net_price=18.0, price_source="promotion",
                            in_stock=True, stock_hint="IN_STOCK"),
        )[0]

    draft = make_draft()
    assert draft.signal_type == "price_drop" and draft.level == "P0"

    # 3h59m 前报过 → 应抑制
    with get_session() as s:
        s.add(Signal(tenant_id="qa-cool", competitor_id=cid, category=draft.category,
                     signal_type=draft.signal_type, level="P0",
                     detected_at=datetime.now() - timedelta(hours=3, minutes=59)))
    with get_session() as s:
        assert collector._is_cooling_down(s, cid, draft, cooldown=notify) is True

    # 挪到 4h01m 前 → 应放行
    with get_session() as s:
        for row in s.execute(select(Signal).where(Signal.competitor_id == cid)).scalars():
            row.detected_at = datetime.now() - timedelta(hours=4, minutes=1)
    with get_session() as s:
        assert collector._is_cooling_down(s, cid, draft, cooldown=notify) is False


# ============================================================
# E. 证据不足不改判 + 边界
# ============================================================

_TIER_TENANT = "default"
_TIER_ASIN = "QATIER0001"
_TIER = {}


def _tier_fixture() -> dict:
    """在 default 租户造一个 S 级竞品，快照**不带**流量词（真实数据形态）。"""
    if _TIER:
        return _TIER
    db()
    with get_session() as s:
        my = s.execute(select(MyProduct).where(MyProduct.tenant_id == _TIER_TENANT)).scalar_one_or_none()
        if my is None:
            my = MyProduct(tenant_id=_TIER_TENANT, asin="QAMINE002", marketplace="US",
                           current_price=20.0, daily_sales_avg=30.0,
                           core_keywords={"toy car": 80.0, "kids gift": 20.0})
            s.add(my)
            s.flush()
        my_id = int(my.id)

        comp = Competitor(tenant_id=_TIER_TENANT, my_product_id=my_id, asin=_TIER_ASIN,
                          marketplace="US", tier="S", status="active",
                          tier_reasons={"price_ratio": 1.025, "keyword_overlap": 0.72,
                                        "node_match": True, "qa_marker": "原始理由"},
                          price_ratio=1.025, keyword_overlap=0.72, node_match=True)
        s.add(comp)
        s.flush()
        cid = int(comp.id)
        for days_ago in (1, 0):
            s.add(Snapshot(competitor_id=cid,
                           captured_at=datetime.now() - timedelta(days=days_ago),
                           price=20.5, net_price=20.5, in_stock=True,
                           stock_hint="IN_STOCK", data_quality="full", source="qa"))
    _TIER.update(my_id=my_id, cid=cid)
    return _TIER


def _tier_state() -> tuple:
    with get_session() as s:
        comp = s.execute(select(Competitor).where(Competitor.asin == _TIER_ASIN)).scalar_one()
        return (comp.tier, dict(comp.tier_reasons or {}), comp.price_ratio)


def _run_maintenance(tenant: str = _TIER_TENANT) -> list[str]:
    logs: list[str] = []

    def fake_info(msg, *args, **kwargs):
        logs.append(msg % args if args else str(msg))

    fake_settings = SimpleNamespace(tenant_id=tenant)
    with mock.patch.object(scheduler, "settings", fake_settings), \
            mock.patch.object(scheduler.logger, "info", fake_info):
        import asyncio
        asyncio.run(scheduler.job_pool_maintenance())
    return logs


def _restore_tiering_gate() -> None:
    settings_store.save(scope="company", group_name="tiering", tenant_id=_TIER_TENANT,
                        values={"keyword_overlap_gate": 0.40})


def test_40_pool_maintenance_skips_real_data_shape_and_keeps_tier_reasons():
    """真实数据形态（无 keywords、无类目路径）下：一个都不改，理由也不许被半份覆盖。"""
    _tier_fixture()
    gate_before = settings_store.resolve_tiering_thresholds(_TIER_TENANT, _TIER["my_id"])
    assert abs(gate_before.keyword_overlap_gate - 0.40) < 1e-9

    before = _tier_state()
    logs = _run_maintenance()
    after = _tier_state()

    assert after[0] == before[0] == "S", f"证据不足时改判了：{before} → {after}"
    assert after[1] == before[1], "证据不足时 tier_reasons 被覆盖了（半份理由后患无穷）"
    assert after[1].get("qa_marker") == "原始理由"
    assert any("跳过" in line for line in logs), logs


def test_41_pool_maintenance_does_downgrade_when_evidence_is_enough():
    """不能一律跳过：证据充足（价格 + 词重合 = 2 维）时，该降级必须降级。"""
    fx = _tier_fixture()
    real_convert = collector.row_to_unified

    def convert_with_keywords(row, marketplace="US"):
        return real_convert(row, marketplace).model_copy(
            update={"keywords": [{"keyword": "toy car", "traffic_share": 70}]}
        )

    try:
        # 门槛 0.90 > 实际重合 0.8 → 必须降成 C
        settings_store.save(scope="company", group_name="tiering", tenant_id=_TIER_TENANT,
                            values={"keyword_overlap_gate": 0.90})
        with mock.patch.object(collector, "row_to_unified", convert_with_keywords):
            _run_maintenance()
        assert _tier_state()[0] == "C", f"证据充足却不降级：{_tier_state()}"

        # 门槛回到 0.40 → 必须重新评上 S（证明是阈值在起作用，不是数据变了）
        _restore_tiering_gate()
        with mock.patch.object(collector, "row_to_unified", convert_with_keywords):
            _run_maintenance()
        assert _tier_state()[0] == "S", f"阈值恢复后未重新评级：{_tier_state()}"
    finally:
        _restore_tiering_gate()
        with get_session() as s:
            comp = s.execute(select(Competitor).where(Competitor.asin == _TIER_ASIN)).scalar_one()
            comp.tier = "S"


def test_42_decidable_dims_boundary_two_must_be_judged():
    """边界：`decidable_dims == 2` 必须判（insufficient=False），1 必须跳过。

    工程师只测了「全部跳过」。差一错误（`< 2` 写成 `<= 2`）会把
    「刚好两维可判」的竞品永远冻结，而这种静默冻结不会报任何错。
    """
    comp = UnifiedSnapshot(
        asin="B0B", price=20.0, net_price=20.0, in_stock=True,
        keywords=[{"keyword": "toy car", "traffic_share": 80}],
    )
    kw = {"toy car": 80.0, "kids gift": 20.0}

    # 价格 + 词重合 = 2 维（无类目路径 → 节点不可判）
    two = judge_tier(my_price=20.0, my_core_keywords=kw, my_category_path=None,
                     my_daily_units=30, comp=comp)
    assert two.price_ratio is not None and two.keyword_overlap is not None
    assert two.node_match is None
    assert two.decidable_dims == 2, two.decidable_dims
    assert two.insufficient is False, "2 维必须判定"
    assert two.effective_tier != "unknown"

    # 价格 + 节点 = 2 维（无词）
    comp_nokw = UnifiedSnapshot(asin="B0C", price=20.0, net_price=20.0,
                                category_path=["Toys", "Cars"])
    two_node = judge_tier(my_price=20.0, my_core_keywords=kw,
                          my_category_path=["Toys", "Cars"],
                          my_daily_units=30, comp=comp_nokw)
    assert two_node.keyword_overlap is None and two_node.node_match is True
    assert two_node.decidable_dims == 2
    assert two_node.insufficient is False

    # 只有价格 = 1 维 → 证据不足
    comp_bare = UnifiedSnapshot(asin="B0D", price=20.0, net_price=20.0)
    one = judge_tier(my_price=20.0, my_core_keywords=kw, my_category_path=None,
                     my_daily_units=30, comp=comp_bare)
    assert one.decidable_dims == 1
    assert one.insufficient is True
    assert one.effective_tier == "unknown"

    # 三维全可判 = 3
    three = judge_tier(my_price=20.0, my_core_keywords=kw,
                       my_category_path=["Toys", "Cars"], my_daily_units=30,
                       comp=UnifiedSnapshot(asin="B0E", price=20.0, net_price=20.0,
                                            keywords=[{"keyword": "toy car", "traffic_share": 80}],
                                            category_path=["Toys", "Cars"]))
    assert three.decidable_dims == 3
    assert three.insufficient is False


# ============================================================
# F. L2 纯函数红线（AST 扫描，比子串扫描更难绕过）
# ============================================================

_DOMAIN_DIR = ROOT / "app" / "domain"

_FORBIDDEN_IMPORT_ROOTS = {
    "sqlalchemy", "requests", "httpx", "aiohttp", "urllib", "socket", "sqlite3",
    "os", "pathlib", "shutil", "subprocess", "asyncio", "logging",
}
_FORBIDDEN_IMPORT_MODULES = {"app.db", "app.models", "app.services", "app.config", "app.api"}

#: 已知例外：tiering.py 判「新品上架天数」用 date.today()，是既有代码，本轮不动。
_KNOWN_ALLOWED = {("tiering.py", "date.today")}


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _scan_domain() -> list[str]:
    offenders: list[str] = []
    for path in sorted(_DOMAIN_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    full = alias.name
                    if root in _FORBIDDEN_IMPORT_ROOTS or full in _FORBIDDEN_IMPORT_MODULES:
                        offenders.append(f"{path.name}:{node.lineno} import {full}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                root = module.split(".")[0]
                if root in _FORBIDDEN_IMPORT_ROOTS or module in _FORBIDDEN_IMPORT_MODULES:
                    offenders.append(f"{path.name}:{node.lineno} from {module} import ...")
            elif isinstance(node, ast.Call):
                dotted = _dotted(node.func)
                checks = (
                    ("datetime.datetime.now", "datetime.now"),
                    ("datetime.datetime.today", "datetime.today"),
                    ("datetime.datetime.utcnow", "datetime.utcnow"),
                    ("datetime.date.today", "date.today"),
                    ("datetime.now", "datetime.now"),
                    ("datetime.today", "datetime.today"),
                    ("datetime.utcnow", "datetime.utcnow"),
                    ("date.today", "date.today"),
                    ("time.time", "time.time"),
                    ("time.monotonic", "time.monotonic"),
                    ("os.getenv", "os.getenv"),
                    ("open", "open"),
                )
                for dotted_form, label in checks:
                    if dotted == dotted_form:
                        if (path.name, label) not in _KNOWN_ALLOWED:
                            offenders.append(f"{path.name}:{node.lineno} 调用 {dotted}")
                if node.func.__class__ is ast.Attribute and node.func.attr in {
                    "read_text", "read_bytes", "write_text", "write_bytes",
                    "connexion", "connect", "commit",
                }:
                    offenders.append(f"{path.name}:{node.lineno} 调用 .{node.func.attr}()")
    return offenders


def test_50_domain_layer_is_pure_by_ast():
    """`app/domain/*.py` 不许有：DB 会话 / 网络 / 环境变量 / 时钟 / 文件读写。"""
    offenders = _scan_domain()
    assert not offenders, "L2 纯度被破坏：\n  " + "\n  ".join(offenders)


def test_51_thresholds_module_imports_stdlib_only():
    """`thresholds.py` 被三边共用，最容易为「方便」塞进一个默认值查询函数。

    它必须只依赖 stdlib + 自身，且不含任何 I/O / 时钟。
    """
    path = _DOMAIN_DIR / "thresholds.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert roots <= {"__future__", "dataclasses", "typing"}, f"意外依赖：{roots - {'__future__','dataclasses','typing'}}"

    # 模块级不允许出现「取默认值要查库/读配置」的入口
    forbidden_calls = {"get_session", "resolve", "settings", "os", "open", "datetime"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            dotted = _dotted(node.func)
            assert dotted not in forbidden_calls, dotted


def test_52_domain_does_not_import_l3_or_l4():
    """domain 只能依赖 domain + adapters(unified 纯数据模型)，不许反向依赖 db/models/services。"""
    offenders = []
    for path in sorted(_DOMAIN_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
                module = ",".join(names)
            if not module:
                continue
            for bad in ("app.db", "app.models", "app.services", "app.config", "app.api"):
                if bad in module:
                    offenders.append(f"{path.name}:{node.lineno} → {module}")
    assert not offenders, offenders


# ============================================================
# 运行器
# ============================================================


def main() -> int:
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed: list[str] = []
    defects: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []

    for name, fn in tests:
        try:
            fn()
            passed.append(name)
            print(f"  ✅ {name}")
        except Exception:  # noqa: BLE001
            if name.startswith("test_defect_"):
                defects.append((name, traceback.format_exc()))
                print(f"  ⚠️  {name}  （缺陷复现）")
            else:
                failed.append((name, traceback.format_exc()))
                print(f"  ❌ {name}")

    total = len(tests)
    print("")
    print(f"通过 {len(passed)}/{total}　缺陷复现 {len(defects)}　意外失败 {len(failed)}")

    if defects:
        print("")
        print("=" * 70)
        print("缺陷复现（预期失败，详见报告）")
        print("=" * 70)
        for name, tb in defects:
            print(f"\n--- {name} ---")
            print(tb.strip().splitlines()[-1] if tb.strip() else "")

    if failed:
        print("")
        print("=" * 70)
        print("意外失败（可能是代码缺陷，也可能是本测试写错）")
        print("=" * 70)
        for name, tb in failed:
            print(f"\n--- {name} ---")
            print(tb)

    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TMP_DIR, ignore_errors=True)
