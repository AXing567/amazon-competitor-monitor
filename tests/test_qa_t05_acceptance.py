"""QA 独立验收 · T04/T05（REST 出口 + 五视图面板 + 离线演示数据）。

运行：
    .venv/Scripts/python.exe tests/test_qa_t05_acceptance.py

## 用什么方法

不用浏览器，也不只看「端点 200」。三层证据：

1. **数据层**：直接数库（signals 11 条 / 分层分布 / 池子 8 条）——与工程师的说法对账；
2. **API 层**：用 TestClient 打真实路由（**不跑 lifespan，因此不会启动调度器**，
   否则后台任务会往库里写）；
3. **破坏性演练**：把表 DROP 掉看端点是否仍然 200 + `ok:false`（服务端故障隔离），
   以及**在含真实数据的库上跑演示脚本/清理**——这是文档声称安全的场景。

## 约定

- 只读 + 只写测试：不改 `app/` 下任何业务代码。
- 演示数据写在临时库（`DATABASE_URL` 在导入 `app.*` 之前改掉），真实库全程不碰。
- `test_defect_*` = 预期失败（复现缺陷）。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---- 必须在 import app.* 之前 ----
_TMP_DIR = Path(tempfile.mkdtemp(prefix="qa_t05_"))
_DB_FILE = _TMP_DIR / "panel.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_FILE}"
os.environ["ACTION_TOKEN_SECRET"] = ""
os.environ["SCHEDULER_ENABLED"] = "false"

from fastapi import FastAPI                                                # noqa: E402
from fastapi.testclient import TestClient                                  # noqa: E402
from sqlalchemy import func, select                                        # noqa: E402

from app.api.routes import api_router                                      # noqa: E402
from app.db import engine as app_engine, get_session                       # noqa: E402
from app.models import Action, Competitor, Signal, Snapshot, User          # noqa: E402

# --- 演示数据：在临时库上造（不碰真实库） ---
import scripts.demo_offline as demo                                        # noqa: E402

_SEEDED = False


def seed() -> None:
    """跑一次演示造数（`main()` 内部会先 _clean 再 seed，所以可重复）。"""
    global _SEEDED
    if _SEEDED:
        return
    argv = sys.argv
    sys.argv = ["demo_offline.py", "--quiet"]
    try:
        demo.main()
    finally:
        sys.argv = argv
    _SEEDED = True


def client() -> TestClient:
    """只挂 API 路由的测试 app —— **不带 lifespan**，因此不会启动 APScheduler。"""
    app = FastAPI()
    app.include_router(api_router)
    return TestClient(app)


def counts() -> dict[str, int]:
    with get_session() as s:
        return {
            "competitors": s.execute(select(func.count()).select_from(Competitor)).scalar_one(),
            "signals": s.execute(select(func.count()).select_from(Signal)).scalar_one(),
            "snapshots": s.execute(select(func.count()).select_from(Snapshot)).scalar_one(),
            "actions": s.execute(select(func.count()).select_from(Action)).scalar_one(),
        }


def signal_levels() -> dict[str, int]:
    with get_session() as s:
        rows = s.execute(select(Signal.level, func.count()).group_by(Signal.level)).all()
    return {lv: n for lv, n in rows}


def _user_id(role: str) -> int:
    with get_session() as s:
        row = s.execute(
            select(User).where(User.role == role, User.tenant_id == "default").limit(1)
        ).scalar_one_or_none()
    assert row is not None, f"没有 role={role} 的用户"
    return int(row.id)


# ============================================================
# ① 数据层：与工程师的说法对账
# ============================================================


def test_01_pool_has_eight_with_expected_status_split():
    """池子 8 条：active 6 / paused 1 / archived 1。"""
    seed()
    res = client().get("/api/panel/pool").json()
    assert res["ok"] is True, res
    assert res["count"] == 8, res["count"]
    assert res["counts_by_status"] == {"active": 6, "paused": 1, "archived": 1}, \
        res["counts_by_status"]
    assert len(res["items"]) == 8


def test_02_board_rows_exclude_archived_only():
    """看板 7 行 = 8 减掉 archived。**paused 是进看板的**（灰显），只是不采集。

    工程师的说法是「paused/archived 不进看板」，但紧接着又说 7 行；
    8 条里只有 1 条 archived，所以 7 行必然包含 paused。
    这条用例把这个口径钉死，避免后面有人照错误说法写断言。
    """
    seed()
    body = client().get("/api/panel/board").json()
    assert body["ok"] is True, body
    assert body["summary"]["total"] == 7, body["summary"]
    asins = {row["asin"] for row in body["items"]}
    assert "B0DEMO0008" not in asins, "归档的必须不在看板"
    assert "B0DEMO0007" in asins, "暂停的要进看板（灰显），不进的是采集队列不是看板"
    # 待判定要能被数出来，而不是被算成 C
    assert body["summary"]["by_tier"].get("unknown") == 1, body["summary"]["by_tier"]
    assert body["summary"]["undecided"] == 1


def test_03_signals_are_11_with_expected_level_split():
    """signals 11 条（P0 6 / P1 3 / P2 2）——这是试算验收的基准，先钉死。"""
    seed()
    assert counts()["signals"] == 11, counts()
    levels = signal_levels()
    assert levels.get("P0") == 6, levels
    assert levels.get("P1") == 3, levels
    assert levels.get("P2") == 2, levels


# ============================================================
# ② 试算：唯一可靠判据是「分等级计数」，不是 total
# ============================================================


def _simulate(values: dict, days: int = 7) -> dict:
    res = client().post(
        "/api/config/thresholds/simulate",
        json={"values": values, "days": days},
    ).json()
    assert res["ok"] is True, res
    return res


def test_04_simulate_moves_p0_in_both_directions_and_writes_nothing():
    """price_p0_pct=0.10 → P0 6→5；=0.001 → P0 6→7；两次 signals 行数必须都是 11。

    刻意不用 `total` 做判据：价格只要过了噪声门槛就一定有信号，收紧只会改**等级**，
    total 恒为 11。面板若拿 total 表达"改了会怎样"，会得出"改了没用"的错误结论。
    """
    seed()
    before = counts()

    loose_wide = _simulate({"diff": {"price_p0_pct": 0.10}})
    assert loose_wide["current"]["p0"] == 6, loose_wide["current"]
    assert loose_wide["candidate"]["p0"] == 5, loose_wide["candidate"]
    assert loose_wide["candidate"]["total"] == loose_wide["current"]["total"], \
        "total 不该变——这正是不能用 total 当判据的原因"

    loose_tight = _simulate({"diff": {"price_p0_pct": 0.001}})
    assert loose_tight["current"]["p0"] == 6, loose_tight["current"]
    assert loose_tight["candidate"]["p0"] == 7, loose_tight["candidate"]

    after = counts()
    assert before == after, f"试算写库了：{before} → {after}"


# ============================================================
# ③ 动作记录的可见范围（X-User-Id 必须真的改变结果）
# ============================================================


def test_05_actions_scope_semantics():
    """默认只看本人；`?user_id=all` 看全团队；换 X-User-Id 必须换结果。"""
    seed()
    owner, member = _user_id("owner"), _user_id("member")
    c = client()

    owner_default = c.get("/api/panel/actions").json()
    assert owner_default["ok"] is True, owner_default
    owner_n = len(owner_default["items"])
    assert owner_n == 3, f"演示数据应有 3 条动作，实际 {owner_n}"

    team = c.get("/api/panel/actions", params={"user_id": "all"}).json()
    assert len(team["items"]) == owner_n, "此刻全团队 == 本人（演示动作都挂在 owner 上）"

    # 以 member 身份补记一条
    rec = c.post(
        "/api/panel/actions",
        headers={"X-User-Id": str(member)},
        json={"action_type": "price_cut", "asin": "B0DEMO0001",
              "description": "QA 以 member 身份补记"},
    ).json()
    assert rec["ok"] is True, rec
    assert rec.get("action_id"), rec

    member_view = c.get("/api/panel/actions", headers={"X-User-Id": str(member)}).json()
    assert len(member_view["items"]) == 1, len(member_view["items"])
    assert member_view["items"][0].get("user_id") == member, member_view["items"][0]

    owner_view = c.get("/api/panel/actions", headers={"X-User-Id": str(owner)}).json()
    assert len(owner_view["items"]) == 3, "换回 owner 不该看到 member 的那条"

    team_view = c.get("/api/panel/actions", params={"user_id": "all"},
                      headers={"X-User-Id": str(owner)}).json()
    assert len(team_view["items"]) == 4, len(team_view["items"])

    # 非法 user_id 要给可用的错，而不是 500
    bad = c.get("/api/panel/actions", params={"user_id": "abc"})
    assert bad.status_code == 200
    assert bad.json()["ok"] is False and "all" in bad.json()["hint"]


# ============================================================
# ④ 五个视图的端点 + 页面本身
# ============================================================


_PANEL_ENDPOINTS = (
    "/api/panel/context",
    "/api/panel/board",
    "/api/panel/pool",
    "/api/panel/actions",
    "/api/panel/health",
    "/api/panel/competitors/B0DEMO0001/changes",
    "/api/config/thresholds/schema",
    "/api/config/thresholds",
    "/api/config/thresholds/versions?scope=company&group_name=diff",
    "/api/config/thresholds/explain?group_name=diff",
    "/api/users",
)


def test_06_all_panel_endpoints_return_ok():
    """11 个端点：HTTP 200 且 `ok` 为 true（巡检脚本用的就是这一组）。"""
    seed()
    c = client()
    bad = []
    for path in _PANEL_ENDPOINTS:
        r = c.get(path)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or body.get("ok") is not True:
            bad.append((path, r.status_code, str(body)[:200]))
    assert not bad, bad


def test_07_panel_page_renders_five_views():
    """`GET /panel` 必须真的渲染出五个视图的容器（不是 200 但空壳）。"""
    seed()
    from app.api.server import app as real_app
    from app.services import scheduler as scheduler_mod

    c = TestClient(real_app)          # 不用 with：不触发 lifespan，不启动调度器
    r = c.get("/panel")
    assert r.status_code == 200, r.status_code
    html = r.text
    for view in ("board", "pool", "actions", "config", "health"):
        assert f'data-view="{view}"' in html or f"view-{view}" in html, f"缺视图容器 {view}"
    assert "只看我" in html and "全团队" in html, "动作视图缺可见范围切换"
    assert scheduler_mod.scheduler_status()["running"] is False, "测试不该启动调度器"


# ============================================================
# ⑤ 服务端故障隔离：一张表坏掉不能让整屏消失
# ============================================================


def test_90_health_degrades_alone_when_one_table_is_broken():
    """DROP 掉 collect_runs：`/api/panel/health` 应 200 + ok:false（人话错误），
    而 `/api/panel/board` 仍然 ok:true。

    这是「单 tab 故障隔离」的**服务端一半**：只要端点不 500、错误是结构化 JSON，
    前端那个 try/catch + errCard 才有东西可渲染。
    """
    seed()
    with app_engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE collect_runs")
    try:
        c = client()
        health = c.get("/api/panel/health")
        assert health.status_code == 200, health.status_code
        hb = health.json()
        assert hb["ok"] is False, hb
        assert hb.get("error") and "OperationalError" not in hb["error"], \
            f"错误没被人话化：{hb.get('error')}"
        assert hb.get("hint"), "失败时要有排查建议"

        board = c.get("/api/panel/board").json()
        assert board["ok"] is True, "别的视图不该被拖死"
        assert board["summary"]["total"] == 7
    finally:
        from app.db import init_db
        init_db()                      # create_all 会把缺的表补回来


# ============================================================
# ⑥ --clean 的自检（纯演示库）
# ============================================================


def test_91_clean_leaves_no_residue_on_pure_demo_db():
    """纯演示库上 `_clean()` 之后残留必须全 0。"""
    global _SEEDED
    seed()
    demo._clean()
    left = demo._demo_residuals()
    assert all(v == 0 for v in left.values()), left
    assert counts()["competitors"] == 0, counts()
    _SEEDED = False          # 后面的用例需要重新造数


# ============================================================
# ⑦ 缺陷复现：演示脚本在「真实库」上的破坏性
# ============================================================

_REAL_DB: dict = {}


def _real_db_with_user_data() -> dict:
    """单独一个库：模拟「用户已经用了三个月」的真库。

    必须独立进程/独立 engine：本文件的 app engine 已绑在演示库上。
    这里用裸 sqlite3 建表 + 直接 SQL 插入，避免动 app engine。
    """
    if _REAL_DB:
        return _REAL_DB
    path = _TMP_DIR / "real.db"
    real_engine = __import__("sqlalchemy").create_engine(f"sqlite:///{path}")
    # 复用项目模型在新库上建表
    from sqlalchemy.orm import sessionmaker

    from app.db import Base
    from app.models import Action, Competitor, MyProduct
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=real_engine)

    # 用 ORM 而不是裸 SQL：这些表的 NOT NULL 约束靠 Python 侧 default 兜底，
    # 裸 SQL 很容易漏字段（我第一次就漏了 my_products.platform）。
    Session = sessionmaker(bind=real_engine, autoflush=False, expire_on_commit=False)
    s = Session()
    try:
        my = MyProduct(
            tenant_id="default", asin="B0REALMINE", marketplace="US", platform="amazon",
            current_price=29.99, daily_sales_avg=40.0, break_even_price=21.5,
            target_margin_rate=0.28, core_keywords={"real kw": 100.0},
        )
        s.add(my)
        s.flush()
        comp = Competitor(
            tenant_id="default", my_product_id=my.id, asin="B0REALCMP1",
            marketplace="US", platform="amazon", tier="A", status="active",
        )
        s.add(comp)
        s.flush()
        for i in range(2):
            s.add(Action(tenant_id="default", my_product_id=my.id, action_type="price_cut",
                         description=f"真实动作-{i}", user_id=1, source="panel"))
        s.commit()
    finally:
        s.close()
    _REAL_DB.update(engine=real_engine, path=path)
    return _REAL_DB


def _run_demo_on(path: Path, args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{path}"
    env["SCHEDULER_ENABLED"] = "false"
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "demo_offline.py"), *args],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180,
    )


def test_08_demo_refuses_db_with_real_anchor_and_leaves_it_untouched():
    """D3 回归：库里有「非演示锚点」时，演示脚本必须**拒绝运行**，且一个字段都不改。

    这是 D3 的正面验收（旧用例断言的是旧行为 returncode==0，已随修复一起翻正）。

    刻意用**与主库完全不同的文件名**（`real.db`）来验：只靠文件名的守卫在这里失效，
    能拦住就说明「内容判据」真的在工作。这两道闸要分开验，否则
    「守卫生效」可能只是「文件名恰好叫 competitor_monitor.db」。
    """
    db = _real_db_with_user_data()
    before = _peek_anchor(db["path"])
    proc = _run_demo_on(db["path"], ["--quiet"])

    assert proc.returncode != 0, "含真实锚点的库上，演示必须拒绝运行"
    out = proc.stdout + proc.stderr
    assert "自家锚点" in out or "拒绝" in out, out[-600:]
    assert _peek_anchor(db["path"]) == before, "拒绝运行却仍然改了锚点"


def test_09_demo_clean_refuses_db_with_user_actions():
    """D4 回归：`--clean` 也必须被守卫拦住——清场同样是删数据。

    D4 的实质是「按 `my_product_id` 删动作 = 清空用户动作记录」，
    修法改成按 `[演示]` 前缀认自己的行；这条用例同时钉住「守卫在 _clean 之前」。
    """
    db = _real_db_with_user_data()
    before_actions = _peek_actions(db["path"])
    assert any(d.startswith("真实动作") for d in before_actions), before_actions

    proc = _run_demo_on(db["path"], ["--clean"])

    assert proc.returncode != 0, "含真实数据的库上，--clean 必须拒绝运行"
    after = _peek_actions(db["path"])
    assert after == before_actions, f"真实动作被动了：{before_actions} → {after}"


def test_10_demo_refuses_when_target_is_the_main_db():
    """路径判据：`--db data/competitor_monitor.db` 必须被拒，且主库内容零变化。

    这道闸的价值在**顺序**：它必须跑在 `init_db()` 之前。否则
    `init_db()` 的 create_all + migrate 已经算「动了主库」了。
    """
    main_db = ROOT / "data" / "competitor_monitor.db"
    if not main_db.exists():
        return                      # 没有主库时这条不适用
    before = (_peek_anchor(main_db), _peek_actions(main_db))

    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    env["SCHEDULER_ENABLED"] = "false"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "demo_offline.py"),
         "--db", str(main_db), "--quiet"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180,
    )

    assert proc.returncode != 0, "指向主库时演示必须拒绝"
    out = proc.stdout + proc.stderr
    assert "主库" in out, out[-600:]
    assert (_peek_anchor(main_db), _peek_actions(main_db)) == before, "主库被改了"


def test_11_default_demo_db_is_a_separate_file():
    """防回归：`DEFAULT_DEMO_DB_PATH` 不许等于主库。

    有人为了让「不传参也能看演示」而把默认值改回主库，是最省事也最危险的改法。
    """
    assert demo.DEFAULT_DEMO_DB_PATH.name != demo.MAIN_DB_PATH.name, \
        f"演示默认库与主库同名：{demo.DEFAULT_DEMO_DB_PATH}"
    assert demo.DEFAULT_DEMO_DB_PATH != demo.MAIN_DB_PATH


def test_12_api_simulate_scope_parity_with_collector():
    """D5 回归（走 HTTP 端点，补能力层那三条之外的路径）：
    面板试算与生产采集必须踩同一批层——只带 `X-User-Id` 头不算「显式要用个人层」。
    """
    seed()
    from app.services import settings_store

    owner = _user_id("owner")
    ctx = client().get("/api/panel/context").json()
    my_id = ctx["my_product_id"]
    assert my_id, ctx

    saved = client().post(
        "/api/config/thresholds",
        headers={"X-User-Id": str(owner)},
        json={"scope": "user", "scope_id": str(owner), "group_name": "diff",
              "values": {"price_p0_pct": 0.001}},
    ).json()
    assert saved["ok"] is True, saved

    # 面板请求当然会带 X-User-Id（浏览器就有），但不该因此隐式启用个人层
    panel = client().post(
        "/api/config/thresholds/simulate",
        headers={"X-User-Id": str(owner)},
        json={"values": {}, "days": 7, "my_product_id": my_id},
    ).json()
    assert panel["ok"] is True, panel

    collect_th = settings_store.resolve_diff_thresholds("default", my_id)
    assert abs(collect_th.price_p0_pct - panel["thresholds"]["current"]["price_p0_pct"]) < 1e-12, (
        "试算与采集口径不一致："
        f"试算 {panel['thresholds']['current']['price_p0_pct']} / 采集 {collect_th.price_p0_pct}"
    )
    assert "user" not in (panel.get("scope") or {}).get("layers", {}), panel.get("scope")


# ============================================================
# ④ D5 验收：diff 阈值的作用域必须与生产采集同口径
# ============================================================
#
# 「信号是否存在」是全局唯一事实：一轮采集 100 个竞品约 25 分钟、受数据源额度约束，
# 不可能按人各跑一遍。所以 diff 组（决定信号产不产生）的生效层永远是
# company + product；个人差异的正确落点是 notify（决定这条信号推给谁）。
# 下面三条用例把这个裁定钉死在代码里。


def _d5_seed(
    tag: str,
    *,
    company_price: float = 0.05,
    user_price: float = None,
    product_price: float = None,
    product_id: int = 777,
    user_id: int = 9,
):
    """造一个**独立租户**的三层 diff 覆盖，返回 `(tenant, product_id, user_id)`。

    用独立租户而不是 "default"：不污染演示数据，也不会被别的用例的写入影响，
    于是这三条用例可以随便跑、随便重跑。

    刻意**不调 `seed()`**：这几条只跟阈值表打交道，不需要任何演示竞品/快照。
    依赖演示造数会引入一个与本事无关的失败面（演示脚本在目标库已有自家锚点时
    会拒绝覆盖），一条验收 D5 的用例不该因为演示数据的原因红掉。
    """
    from app.db import init_db
    init_db()                                  # 幂等：只保证表存在，不写业务数据

    from app.services import settings_store

    tenant = f"qa-d5-{tag}"
    saved = settings_store.save(
        scope="company", group_name="diff",
        values={"price_p0_pct": company_price}, tenant_id=tenant,
    )
    assert saved["ok"] is True, saved
    if user_price is not None:
        saved = settings_store.save(
            scope="user", scope_id=str(user_id), group_name="diff",
            values={"price_p0_pct": user_price}, tenant_id=tenant,
        )
        assert saved["ok"] is True, saved
    if product_price is not None:
        saved = settings_store.save(
            scope="product", scope_id=str(product_id), group_name="diff",
            values={"price_p0_pct": product_price}, tenant_id=tenant,
        )
        assert saved["ok"] is True, saved
    return tenant, product_id, user_id


def test_panel_simulate_ignores_ctx_user_id():
    """面板请求头带 `X-User-Id` → `ctx.user_id` 有值，但试算**不许**隐式继承它。

    继承的后果不是"多算一条"这么轻：只要库里存在一条 user 层 diff 覆盖，
    面板算出的触发次数就系统性高于生产，而用户正是拿这个数字决定"要不要收紧阈值"。
    试算变成一个骗人的工具之后，"能预估触发频率"这个核心承诺就没了。
    """
    from app.capabilities.judgments import simulate_thresholds
    from app.capabilities.types import SOURCE_PANEL, CapContext

    tenant, _product_id, uid = _d5_seed("ctx-uid", company_price=0.05, user_price=0.09)
    ctx = CapContext(tenant_id=tenant, user_id=uid, source=SOURCE_PANEL)

    res = simulate_thresholds(ctx, days=7)          # 关键：不传 user_id
    assert res["ok"] is True, res
    assert res["scope"]["user_id"] is None, res["scope"]
    assert "user" not in res["scope"]["layers"], res["scope"]
    # 数值层面再钉一次：必须取公司层的 0.05，不是个人层的 0.09
    assert res["thresholds"]["current"]["price_p0_pct"] == 0.05, res["thresholds"]["current"]


def test_simulate_matches_collector_resolution():
    """D5 的正面验收：试算的生效层 == 采集的生效层，含 `user` 与否必须相同。"""
    from app.capabilities.judgments import simulate_thresholds
    from app.capabilities.types import SOURCE_PANEL, CapContext
    from app.services import settings_store

    tenant, product_id, uid = _d5_seed(
        "parity", company_price=0.05, user_price=0.09, product_price=0.06
    )
    ctx = CapContext(tenant_id=tenant, user_id=uid, source=SOURCE_PANEL)

    res = simulate_thresholds(ctx, days=7, my_product_id=product_id)
    assert res["ok"] is True, res

    # 生产采集的真实口径（见 app/services/collector.py）：不传 user_id
    collect_th = settings_store.resolve_diff_thresholds(tenant, product_id)
    _, collect_sources = settings_store.resolve(tenant, my_product_id=product_id)
    collect_layers = set((collect_sources.get("diff") or {}).values())
    sim_layers = set(res["scope"]["layers"])

    assert ("user" in sim_layers) == ("user" in collect_layers), (
        f"口径不一致：试算踩了 {sorted(sim_layers)}，采集踩了 {sorted(collect_layers)}"
    )
    assert sim_layers == collect_layers, (sorted(sim_layers), sorted(collect_layers))
    assert res["thresholds"]["current"]["price_p0_pct"] == collect_th.price_p0_pct == 0.06


def test_explicit_user_id_still_works():
    """显式传 `user_id` 仍然启用个人层——只是不再**隐式**继承 `ctx.user_id`。

    防的是"修 D5"被理解成"把个人层删掉"：API 调用方要做 per-user 模拟时
    必须还能拿到个人层的结果，并且 `scope` 要如实标出来（面板据此显示警示色）。
    """
    from app.capabilities.judgments import simulate_thresholds
    from app.capabilities.types import SOURCE_PANEL, CapContext

    tenant, _product_id, uid = _d5_seed("explicit-uid", company_price=0.05, user_price=0.09)
    ctx = CapContext(tenant_id=tenant, user_id=uid, source=SOURCE_PANEL)

    res = simulate_thresholds(ctx, days=7, user_id=uid)
    assert res["ok"] is True, res
    assert res["scope"]["user_id"] == uid, res["scope"]
    assert "user" in res["scope"]["layers"], res["scope"]
    assert res["thresholds"]["current"]["price_p0_pct"] == 0.09, res["thresholds"]["current"]


def test_explain_does_not_present_user_layer_as_effective():
    """人话说明也不许把个人层说成「当前生效」（D5 的第二个出口）。

    `manifest`：库里有一条 user 层 diff 覆盖 `price_p0_pct=0.03`，公司层是 0.05。
    判定实际用 0.05，所以说明里一旦出现「0.03（来自个人覆盖）」+ 标题「当前生效」，
    用户就会以为「系统按我的 3% 在判」——这跟试算那个谎是同一个，只是换了出口。

    同时要防止修法过头：`notify` 组**必须**照常计入个人层（个人差异该落的地方），
    所以下面两段断言成对出现。
    """
    from app.capabilities.judgments import explain_thresholds
    from app.capabilities.types import SOURCE_PANEL, CapContext
    from app.services import settings_store

    tenant, _product_id, uid = _d5_seed("explain", company_price=0.05, user_price=0.03)
    ctx = CapContext(tenant_id=tenant, user_id=uid, source=SOURCE_PANEL)

    # ---- diff：个人层被剔掉，回落到公司层 ----
    diff = explain_thresholds(ctx, "diff")
    assert diff["ok"] is True, diff
    assert "0.03（来自个人覆盖）" not in diff["text"], diff["text"]
    assert diff["effective"]["price_p0_pct"] == 0.05, diff["effective"]
    assert diff["sources"]["price_p0_pct"] == "company", diff["sources"]
    assert diff["personal_layer_applied"] is False, diff
    assert len(diff["personal_overrides_not_applied"]) == 1, diff["personal_overrides_not_applied"]
    first = diff["personal_overrides_not_applied"][0]
    assert (first["key"], first["value"]) == ("price_p0_pct", 0.03), first
    # 不许静默剔除：人话里要有一句提示，让用户知道"我设的 3% 没生效、为什么"
    assert "不参与" in diff["text"], diff["text"]
    assert "0.03" in diff["text"], diff["text"]

    # ---- notify：个人层正常计入（防一刀切）----
    saved = settings_store.save(
        scope="user", scope_id=str(uid), group_name="notify",
        values={"cooldown_p0_hours": 12}, tenant_id=tenant,
    )
    assert saved["ok"] is True, saved

    notify = explain_thresholds(ctx, "notify")
    assert notify["ok"] is True, notify
    assert notify["effective"]["cooldown_p0_hours"] == 12, notify["effective"]
    assert notify["sources"]["cooldown_p0_hours"] == "user", notify["sources"]
    assert notify["personal_layer_applied"] is True, notify
    assert notify["personal_overrides_not_applied"] == [], notify
    assert "12 小时（来自个人覆盖）" in notify["text"], notify["text"]


def _peek_anchor(path: Path) -> dict:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT asin, current_price, daily_sales_avg, core_keywords FROM my_products LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return {"asin": row[0], "current_price": row[1], "daily_sales_avg": row[2],
            "core_keywords": row[3]}


def _peek_actions(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT description FROM actions")]
    finally:
        conn.close()


# ============================================================
# 运行器
# ============================================================


def main() -> int:
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed: list[str] = []
    defects: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []

    for name, fn in tests:
        try:
            fn()
            passed.append(name)
            print(f"  ✅ {name}")
        except Exception as exc:  # noqa: BLE001
            if name.startswith("test_defect_"):
                defects.append((name, f"{type(exc).__name__}: {exc}"))
                print(f"  ⚠️  {name}  （缺陷复现）")
            else:
                failed.append((name, traceback.format_exc()))
                print(f"  ❌ {name}")

    print("")
    print(f"通过 {len(passed)}/{len(tests)}　缺陷复现 {len(defects)}　意外失败 {len(failed)}")

    if defects:
        print("")
        print("=" * 70)
        print("缺陷复现（预期失败）")
        print("=" * 70)
        for name, tb in defects:
            print(f"\n--- {name} ---")
            print(tb)

    if failed:
        print("")
        print("=" * 70)
        print("意外失败")
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
