"""回归测试 · 演示脚本「不碰真库 / 不删真数据」的两条硬保证（D3 / D4）。

运行：
    .venv/Scripts/python.exe -m pytest tests/test_demo_isolation.py -q

## 覆盖什么

| 用例 | 守的是哪条 |
|---|---|
| `test_clean_does_not_touch_real_actions` | `_clean()` 不能删没有 `[演示]` 前缀的真实动作（D4） |
| `test_clean_removes_demo_actions` | `_clean()` 必须删掉带 `[演示]` 前缀的演示动作 |
| `test_clean_delete_and_residual_use_same_predicate` | 删和核对必须是同一口径，"删得越干净"不能等于"数出来 0" |
| `test_guard_refuses_real_anchor` | 有真实锚点时 `_guard_target_db()` 必须 `SystemExit`（D3） |
| `test_guard_refuses_real_competitors` | 有真实竞品时同样拒绝 |
| `test_guard_refuses_main_db_path` | 目标就是主库路径时，**看路径**就拒绝（内容判据对主库无效） |
| `test_guard_allows_demo_anchor` | 纯演示库必须放行（守卫不能把正常路径也堵了） |
| `test_default_db_is_not_main_db` | 默认库路径不能是主库（D3 的根治点） |
| `test_db_flag_points_at_given_path` | `--db` 优先于环境变量，且写进 `DATABASE_URL` |
| `test_demo_tier_numbers_are_engine_derived` | 分层 / 词重合 / 价格比必须彼此自洽且来自引擎（不是常量兜底） |

## 为什么自己造 engine，不直接复用 `app.db` 的

`app/db.py` 的 `engine` 是**导入时**绑到 `settings.database_url` 的常量，
同一进程跑多个测试文件时"谁先导入谁赢"。本文件若与
`tests/test_qa_t05_acceptance.py` 一起跑（`pytest tests/`），直接复用那份 engine
就会拿到别人的库，测试之间互相污染。

所以这里把演示脚本的两个取数入口 `monkeypatch` 成本测试自己的：
`demo.get_session` → 指向 `tmp_path` 上的新库；`demo.settings` → 只带
`tenant_id` / `database_url` 的最小替身。一条用例一个全新库，且与导入顺序无关。

`DATABASE_URL` 仍然在导入 `scripts.demo_offline` **之前**设成临时目录下的文件：
演示脚本导入时会预置 `DATABASE_URL`（默认 `data/demo_offline.db`），
这里先占位，免得跑一次测试就在仓库里建出一个演示库。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---- 必须在 import app.* / scripts.demo_offline 之前占位 ----
_IMPORT_TMP = Path(tempfile.mkdtemp(prefix="demo_isolation_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_IMPORT_TMP / 'import_time.db').as_posix()}"
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from app.db import Base                                              # noqa: E402
from app.models import Action, Competitor, MyProduct                 # noqa: E402

import scripts.demo_offline as demo                                  # noqa: E402


# ============================================================
# fixture：一个干净的临时库 + 把演示脚本指过去
# ============================================================


@pytest.fixture()
def demo_db(tmp_path, monkeypatch) -> SimpleNamespace:
    """建一个空库，并把 `scripts.demo_offline` 的 `get_session` / `settings` 指过去。

    Returns:
        `SimpleNamespace(path=..., Session=...)`：`Session` 是 `sessionmaker`，
        用例直接 `with demo_db.Session() as s:` 造数据 / 读数据。
    """
    path = tmp_path / "demo_offline.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    @contextmanager
    def _session():
        """与 `app.db.get_session()` 同签名同语义的上下文管理器。"""
        s = Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(demo, "get_session", _session)
    monkeypatch.setattr(
        demo,
        "settings",
        SimpleNamespace(
            tenant_id="default",
            database_url=f"sqlite:///{path.as_posix()}",
        ),
    )
    return SimpleNamespace(path=path, Session=Session)


def _add_anchor(Session, asin: str, keywords: dict | None = None) -> int:
    """插一条自家锚点，返回 id。"""
    with Session() as s:
        my = MyProduct(
            tenant_id="default",
            asin=asin,
            marketplace="US",
            platform="amazon",
            core_keywords=keywords,
        )
        s.add(my)
        s.flush()
        new_id = int(my.id)
        s.commit()
    return new_id


def _add_action(
    Session,
    my_product_id: int,
    description: str,
    reason: str | None,
) -> None:
    """插一条动作记录。`reason` 决定它"是演示的还是真实的"。"""
    with Session() as s:
        s.add(
            Action(
                tenant_id="default",
                my_product_id=my_product_id,
                action_type="price_hold",
                description=description,
                reason=reason,
                user_id=1,
                source="panel",
            )
        )
        s.commit()


def _action_descriptions(Session) -> list[str]:
    with Session() as s:
        return list(s.execute(select(Action.description)).scalars())


# ============================================================
# D4：--clean 不能碰真实动作，但必须删掉演示动作
# ============================================================


def test_clean_does_not_touch_real_actions(demo_db) -> None:
    """真实动作（`reason` 不带 `[演示]`）挂在自家锚点上，`_clean()` 后必须还在。

    这就是 D4 的现场：单产品租户里"第一条 my_products"就是用户自己的锚点，
    旧实现按 `my_product_id` 删，等于跑一次演示就清空用户的动作记录。
    """
    my_id = _add_anchor(demo_db.Session, "B0REAL0001", {"toy car": 0.31})
    _add_action(demo_db.Session, my_id, "真实动作：不跟价", "他断货了，跟价没意义")
    _add_action(demo_db.Session, my_id, "真实动作：跟到 28.99", "只降 8%，还在保本价之上")

    demo._clean()          # 连自查（_verify_clean）一起跑，走默认路径

    left = _action_descriptions(demo_db.Session)
    assert sorted(left) == ["真实动作：不跟价", "真实动作：跟到 28.99"], left


def test_clean_removes_demo_actions(demo_db) -> None:
    """带 `[演示]` 前缀的动作必须被删；同一锚点下的真实动作必须留下。

    两条动作挂在**同一个** `my_product_id` 上——旧实现要么全删、要么全留，
    新实现按前缀分得开。
    """
    my_id = _add_anchor(demo_db.Session, demo.DEMO_MY_ASIN)
    _add_action(demo_db.Session, my_id, "演示动作", f"{demo.DEMO_REASON_PREFIX}他只降 8%")
    _add_action(demo_db.Session, my_id, "真实动作", "我自己写的理由")

    demo._clean()

    left = _action_descriptions(demo_db.Session)
    assert left == ["真实动作"], left


def test_clean_delete_and_residual_use_same_predicate(demo_db) -> None:
    """删完再数，残留必须是 0；且"数出来的 0"不能靠把真实数据也删掉换来。

    旧实现两个口径都是 `my_product_id == 演示锚点`，于是"删光真实动作"之后
    恰好显示 `actions 0`——删得越多看起来越干净。
    """
    my_id = _add_anchor(demo_db.Session, demo.DEMO_MY_ASIN)
    _add_action(demo_db.Session, my_id, "演示动作", f"{demo.DEMO_REASON_PREFIX}演示理由")
    _add_action(demo_db.Session, my_id, "真实动作", "真实理由")

    demo._clean()

    residuals = demo._demo_residuals()
    assert residuals["actions"] == 0, residuals
    assert _action_descriptions(demo_db.Session) == ["真实动作"]


# ============================================================
# D3：目标库守卫
# ============================================================


def test_guard_refuses_real_anchor(demo_db) -> None:
    """库里有用户自己的锚点（`asin != B0MINE0001`）→ 拒绝运行，且消息里点出那个 ASIN。"""
    _add_anchor(demo_db.Session, "B0REAL0001", {"toy car": 0.31, "diecast car": 0.22})

    with pytest.raises(SystemExit) as exc:
        demo._guard_target_db()

    message = str(exc.value.code)
    assert "B0REAL0001" in message, message
    assert "--db" in message, message


def test_guard_refuses_real_competitors(demo_db) -> None:
    """锚点是演示锚点，但竞品里有真实 ASIN → 同样拒绝（这是真库的另一个特征）。"""
    my_id = _add_anchor(demo_db.Session, demo.DEMO_MY_ASIN)
    with demo_db.Session() as s:
        s.add(
            Competitor(
                tenant_id="default",
                my_product_id=my_id,
                asin="B0REALCMP1",
                marketplace="US",
                platform="amazon",
                tier="A",
                status="active",
            )
        )
        s.commit()

    with pytest.raises(SystemExit) as exc:
        demo._guard_target_db()

    message = str(exc.value.code)
    assert "B0REALCMP1" in message, message
    assert "1" in message, message


def test_guard_refuses_main_db_path(demo_db, monkeypatch) -> None:
    """目标是主库路径时**看路径就拒绝**，不看内容。

    为什么必须有这条：实测主库里那条自家锚点的 ASIN 恰好是 `B0MINE0001`、
    唯一竞品恰好叫 `B0DEMO1234`（历史演示残留），光看内容它和"演示库"无法区分，
    内容判据对它完全无效。主库是"永远不许写"，不能靠推断。
    """
    monkeypatch.setattr(
        demo,
        "settings",
        SimpleNamespace(
            tenant_id="default",
            database_url=f"sqlite:///{demo.MAIN_DB_PATH.as_posix()}",
        ),
    )

    with pytest.raises(SystemExit) as exc:
        demo._guard_target_db()

    message = str(exc.value.code)
    assert demo.MAIN_DB_PATH.name in message, message


def test_guard_allows_demo_anchor(demo_db) -> None:
    """纯演示库（只有 `B0MINE0001` 锚点 + `B0DEMO*` 竞品）必须放行，否则正常路径也被堵死。"""
    my_id = _add_anchor(demo_db.Session, demo.DEMO_MY_ASIN)
    with demo_db.Session() as s:
        s.add(
            Competitor(
                tenant_id="default",
                my_product_id=my_id,
                asin="B0DEMO0001",
                marketplace="US",
                platform="amazon",
                tier="S",
                status="active",
            )
        )
        s.commit()

    demo._guard_target_db()      # 不抛即通过


# ============================================================
# 落点：默认库不能是主库
# ============================================================


def test_default_db_is_not_main_db(monkeypatch) -> None:
    """默认库路径必须不是 `data/competitor_monitor.db`（D3 的根治点）。"""
    assert demo.MAIN_DB_PATH.name == "competitor_monitor.db", demo.MAIN_DB_PATH
    assert demo.DEFAULT_DEMO_DB_PATH.resolve() != demo.MAIN_DB_PATH.resolve(), (
        f"默认库与主库重合：{demo.DEFAULT_DEMO_DB_PATH}"
    )

    # 未给 --db、环境里也没有 DATABASE_URL 时，应当落到独立演示库。
    monkeypatch.delenv("DATABASE_URL", raising=False)
    resolved = demo._preset_database_url(["demo_offline.py"])

    assert resolved == demo.DEFAULT_DEMO_DB_PATH, resolved
    assert os.environ["DATABASE_URL"] == f"sqlite:///{demo.DEFAULT_DEMO_DB_PATH.as_posix()}"


def test_db_flag_points_at_given_path(tmp_path, monkeypatch) -> None:
    """`--db PATH` 优先级最高：连指向主库的环境变量都压得住（这是逃生门唯一的形状）。"""
    target = tmp_path / "custom.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{demo.MAIN_DB_PATH.as_posix()}")

    resolved = demo._preset_database_url(["demo_offline.py", "--db", str(target)])

    assert resolved == target.resolve(), resolved
    assert os.environ["DATABASE_URL"] == f"sqlite:///{target.resolve().as_posix()}"
    assert resolved != demo.MAIN_DB_PATH.resolve()


# ============================================================
# 分层数字必须由引擎算出来（不是常量兜底）
# ============================================================


def _run_demo_on_new_db(db_path: Path) -> subprocess.CompletedProcess:
    """用子进程在指定库上跑一次造数（子进程的好处：settings / engine 都是全新的）。"""
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    env["SCHEDULER_ENABLED"] = "false"
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "demo_offline.py"),
            "--db",
            str(db_path),
            "--quiet",
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_demo_tier_numbers_are_engine_derived(tmp_path) -> None:
    """演示数据里的「分层 / 词重合 / 价格比」必须彼此自洽，且来自引擎。

    缺陷背景：旧实现把 `tier` 写成场景名、把 `price_ratio` / `keyword_overlap`
    写成常量 0.96 / 0.71，两者不同源 → 面板上 6 个竞品数字完全相同，出现
    「C 级对手 · 词重合 71%」（真有 71% 重合，过 40% 门槛就该是 A 级以上）。
    这类 bug **层级是对的、数字是错的**，只看 `tier` 永远发现不了，
    所以这里直接锁数字之间的关系。

    断言（刻意不写死任何阈值数值，改阈值不该让这条测试变红）：
      1. 8 个竞品的词重合不是同一个值（常量兜底的指纹）；
      2. 层级与词重合单调：S > A > B > C，C 接近 0；
      3. `price_ratio` == 最新快照价 / 自家售价（说明来自快照而不是常量）；
      4. `unknown` 那条是 `None`（判不了），不是 `0`（判过且不重合）。
    """
    db = tmp_path / "demo_offline.db"
    proc = _run_demo_on_new_db(db)
    assert proc.returncode == 0, (proc.stdout[-1500:], proc.stderr[-1500:])

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT asin, tier, keyword_overlap, price_ratio FROM competitors ORDER BY asin"
        ).fetchall()
        my_price = conn.execute(
            "SELECT current_price FROM my_products ORDER BY id LIMIT 1"
        ).fetchone()[0]
        latest_price = dict(
            conn.execute(
                "SELECT c.asin, (SELECT s.price FROM snapshots s "
                "  WHERE s.competitor_id = c.id "
                "  ORDER BY s.captured_at DESC, s.id DESC LIMIT 1) "
                "FROM competitors c"
            ).fetchall()
        )
    finally:
        conn.close()

    assert len(rows) == 8, rows

    by_tier: dict[str, list[float]] = {}
    for asin, tier, kw, ratio in rows:
        if kw is not None:
            by_tier.setdefault(tier, []).append(float(kw))
        # 3. 价格比必须能由快照价复算出来
        expected = round(latest_price[asin] / my_price, 4)
        assert abs(float(ratio) - expected) < 1e-4, (asin, ratio, expected)

    # 1. 不能是常量兜底
    all_kw = [kw for _a, _t, kw, _r in rows if kw is not None]
    assert len(set(all_kw)) > 1, f"所有竞品词重合度相同（{all_kw}）——像是常量兜底"

    # 2. 层级与词重合单调
    for tier in ("S", "A", "B", "C"):
        assert by_tier.get(tier), f"{tier} 级没有任何竞品：{by_tier}"
    assert min(by_tier["S"]) > max(by_tier["A"]), by_tier
    assert min(by_tier["A"]) > max(by_tier["B"]), by_tier
    assert min(by_tier["B"]) > max(by_tier["C"]), by_tier
    assert max(by_tier["C"]) < 0.2, f"C 级不该有明显词重合：{by_tier['C']}"

    # 4. unknown = 判不了（None），不是判过且不重合（0）
    assert [kw for _a, t, kw, _r in rows if t == "unknown"] == [None], rows

    # 5. 对标绑定的 basis 必须与竞品行同源
    #    （绑定是「当初凭什么判成这样」的存档，另写一份字面量就等于留了两份
    #     互相矛盾的证据，而面板会同时展示它们）
    conn = sqlite3.connect(db)
    try:
        bindings = conn.execute(
            "SELECT competitor_id, basis FROM competitor_bindings"
        ).fetchall()
        stored_kw = dict(conn.execute("SELECT id, keyword_overlap FROM competitors").fetchall())
    finally:
        conn.close()
    assert bindings, "演示数据里应该有对标绑定"
    for cid, raw in bindings:
        reasons = (json.loads(raw) if raw else {}).get("tier_reasons") or {}
        assert reasons.get("keyword_overlap") == stored_kw[cid], (cid, reasons, stored_kw[cid])
