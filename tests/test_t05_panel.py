"""T05 面板验收：五视图数据自洽 / unknown 链路 / 试算方向性 / 错误路径 / 单 tab 隔离.

不用 pytest：`python tests/test_t05_panel.py`（沿用 tests/test_thresholds.py 的运行器风格）。

## 这个文件在验什么

面板最容易失信的地方不是 500，而是**两处数字对不上**：顶部写 11 条、点进去数出 10 条。
所以这里的断言几乎全是「总数与明细互相验算」，而不是「字段存在」。

## 为什么不连真库

`DATABASE_URL` 在 import `app.*` **之前**就指向临时目录，并且用子进程跑
`scripts/demo_offline.py` 造演示数据。跑完删临时目录，主库
`data/competitor_monitor.db` 一个字节都不会动（`test_never_touches_main_db` 兜底）。

## 「缺陷复现」怎么算

`KNOWN_DEFECTS` 里的用例断言的是**应有的行为**（例如「错误响应必须给 hint」）。
它们失败 = 已知缺陷仍在（计入「缺陷复现」，不算失败退出）；哪天被修好了，
用例自动变绿并计入通过——不需要有人回来改测试。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ============================================================
# 环境：临时库 + 演示数据（必须在 import app.* 之前）
# ============================================================

_TMP_DIR = Path(tempfile.mkdtemp(prefix="qa_t05_panel_"))
_DB = _TMP_DIR / "panel.db"
_MAIN_DB = ROOT / "data" / "competitor_monitor.db"
_MAIN_DB_SIG = None

os.environ["DATABASE_URL"] = f"sqlite:///{_DB.as_posix()}"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["LOG_LEVEL"] = "WARNING"

_seed = subprocess.run(
    [sys.executable, str(ROOT / "scripts" / "demo_offline.py"), "--quiet"],
    cwd=str(ROOT),
    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    capture_output=True,
    text=True,
)
if _seed.returncode != 0:  # 造数失败要让原因一眼可见，而不是后面几十个用例连环报错
    raise SystemExit(f"演示数据造数失败：\n{_seed.stdout}\n{_seed.stderr}")

from fastapi.testclient import TestClient  # noqa: E402

import app.db as app_db  # noqa: E402
from app.api.server import app  # noqa: E402
from app.config import settings  # noqa: E402

#: 本模块自己管库：import 期就起了子进程造演示数据，不能让 conftest 的
#: 每模块空库把它盖掉。`tests/conftest.py` 看到这个标记会跳过自动隔离夹具。
__own_db__ = True

# 原先是 `assert "qa_t05_panel_" in settings.database_url`。那句在
# `pytest tests/` 下**必然失败**：pytest 先 import 完所有模块，而 `settings`
# 在 app.config 被第一个模块 import 时就已经实例化，本模块之后写的
# `os.environ["DATABASE_URL"]` 对它无效，于是断言看到的是别人的库，
# 在 collection 阶段就把整轮测试打断。
# 正确的做法不是断言，而是**把 settings 掰回自己的库再重建 engine**：
# 这样无论 pytest（多模块同进程）还是脚本模式都能落到这里指定的文件上。
settings.database_url = f"sqlite:///{_DB.as_posix()}"
app_db.reset_engine()

if "qa_t05_panel_" not in settings.database_url:   # pragma: no cover
    raise SystemExit(
        f"临时库没有生效，实际连的是 {settings.database_url}——绝不能拿主库跑测试"
    )


try:                                    # pytest 下才需要这个夹具；脚本模式不装 pytest 也能跑
    import pytest
except ImportError:                     # pragma: no cover
    pytest = None

if pytest is not None:

    @pytest.fixture(scope="module", autouse=True)
    def _rebind_own_db():
        """把连接串掰回本模块的临时库。

        import 期已经设过一次，但 `pytest tests/` 会把**所有**模块先 import 完，
        之后每个模块的 conftest 夹具又各自改 `settings.database_url`——轮到本模块
        跑用例时，全局 settings 指向的是**别的模块**的库。所以必须在这里再掰一次。
        没有这一步，本模块的 5 个用例会拿别人的数据当自己的现场（实测：空库用例
        看到 47 个对比样本，断言直接崩）。
        """
        settings.database_url = f"sqlite:///{_DB.as_posix()}"
        app_db.reset_engine()
        app_db.init_db()
        yield

client = TestClient(app)


def _seed_fingerprint() -> str | None:
    """主库指纹：跑测试前后比一次，证明没被碰过。"""
    if not _MAIN_DB.exists():
        return None
    import hashlib

    return hashlib.sha256(_MAIN_DB.read_bytes()).hexdigest()


_MAIN_DB_SIG = _seed_fingerprint()


# ============================================================
# 小工具
# ============================================================


def _get(path: str):
    res = client.get(path)
    assert res.status_code == 200, f"{path} 返回 {res.status_code}"
    return res.json()


def _post(path: str, body) -> tuple[int, dict]:
    res = client.post(path, json=body)
    return res.status_code, res.json()


def _post_raw(path: str, raw: bytes, ctype: str = "application/json") -> tuple[int, dict]:
    res = client.post(path, content=raw, headers={"Content-Type": ctype})
    return res.status_code, res.json()


def _sql(query: str, args: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(f"file:{_DB.as_posix()}?mode=ro", uri=True)
    try:
        return list(con.execute(query, args))
    finally:
        con.close()


def _panel_html() -> str:
    res = client.get("/panel")
    assert res.status_code == 200
    return res.text


# ============================================================
# ① 页面本身：单文件、五视图、零外链
# ============================================================


def test_panel_page_is_single_file_with_all_views():
    """面板必须能在断网时打开：一个文件、一段内联脚本、零个外链资源。

    ## 视图清单为什么从 `VIEWS` 派生，而不是写死一份

    写死的清单一旦加视图就会红，而红的那条信息是"我加了一个视图"——
    这件事在 diff 里本来就看得见，测试不该重复喊。真正需要守的是
    **顺序一致性**（标签顺序 == section 顺序 == VIEWS 顺序），
    那个只能用派生验证。

    视图数量单独断言一个数字（下面那条），加视图时确实要改它——
    这就是 review 点，但也只改一个数字，不用同步维护一份名单。
    """
    from app.web.views import VIEWS

    html = _panel_html()

    assert len(re.findall(r"<script", html)) == 1, "面板只许有 1 段内联 <script>"
    assert not re.findall(r"<(?:script|link|img)[^>]+(?:src|href)=", html), (
        "面板不许引用任何外部资源（断了网就打不开）"
    )
    assert len(VIEWS) == 6, f"视图数量应当是 6，实际 {len(VIEWS)}：{[v.key for v in VIEWS]}"
    sections = re.findall(r'<section class="view" id="(view-[a-z]+)"', html)
    assert sections == [f"view-{v.key}" for v in VIEWS], sections
    assert html.count('class="view-body"') == len(VIEWS), "每个 view 必须恰好一个 .view-body"

    # 标签顺序 = section 顺序（都从 views.VIEWS 生成，这里验产物没走样）
    tabs = re.findall(r'data-view="([a-z]+)"', html)
    assert tabs == [s.replace("view-", "") for s in sections], tabs
    for key in tabs:
        assert f'id="view-{key}"' in html

    # 空库/坏库也要能打开：页面里不许内嵌任何一次数据查询的结果
    assert "加载中…" in html, "占位文案应该在服务端就写好（JS 挂掉也不至于全白）"


def test_safeload_is_the_only_loader_entry():
    """静态核对：所有网络请求只有一个出口（`api()`），五个 view 只有一个加载入口。"""
    web = ROOT / "app" / "web"
    sources = {p.name: p.read_text(encoding="utf-8") for p in web.rglob("*.py")}

    fetch_total = sum(s.count("fetch(") for s in sources.values())
    assert fetch_total == 1, f"`fetch(` 只许出现在 api() 里，实际 {fetch_total} 处"

    from app.web.views import VIEWS

    register_total = sum(s.count("register('") for s in sources.values())
    assert register_total == len(VIEWS), (
        f"应当每个视图各注册一个加载函数（{len(VIEWS)} 个），实际 {register_total}"
    )

    for mod in [v.key for v in VIEWS]:
        assert f"id=\"view-{mod}\"" in sources[f"{mod}.py"], mod
        assert sources[f"{mod}.py"].count('class="view-body"') == 1, mod


# ============================================================
# ② 看板：总数必须等于明细
# ============================================================


def test_board_summary_matches_items():
    board = _get("/api/panel/board")
    assert board["ok"] is True
    summary, items = board["summary"], board["items"]

    assert summary["total"] == len(items), "顶部总数与表格行数必须一致"

    actual: dict[str, int] = {}
    for it in items:
        tier = it.get("tier") or "unknown"
        actual[tier] = actual.get(tier, 0) + 1
    assert actual == summary["by_tier"], f"by_tier {summary['by_tier']} 与明细 {actual} 不一致"
    assert sum(summary["by_tier"].values()) == summary["total"]

    levels = summary["week_by_level"]
    assert summary["week_changes"] == sum(levels.values()), (
        f"近 7 天变化总数 {summary['week_changes']} ≠ P0+P1+P2 {sum(levels.values())}"
    )
    assert summary["undecided"] == summary["by_tier"].get("unknown", 0)

    # 看板不含已归档：池子 8 个，看板 7 行
    pool = _get("/api/panel/pool?status=active,paused,archived")
    assert summary["total"] == sum(
        1 for it in pool["items"] if it["status"] != "archived"
    ), "看板行数应当等于池子里非归档的竞品数"


def test_pool_counts_match_filters():
    pool = _get("/api/panel/pool?status=active,paused,archived")
    counts = pool["counts_by_status"]
    assert pool["count"] == len(pool["items"]) == sum(counts.values())

    for status, expected in counts.items():
        one = _get(f"/api/panel/pool?status={status}")
        assert one["count"] == expected, f"status={status} 筛出 {one['count']}，但计数说 {expected}"
        assert all(it["status"] == status for it in one["items"])

    # 计数基于未过滤的全量：取消勾选不会让芯片上的数字变
    part = _get("/api/panel/pool?status=active")
    assert part["counts_by_status"] == counts


# ============================================================
# ③ 阈值：schema 与生效值必须是同一套键
# ============================================================


def test_schema_keys_equal_effective_keys():
    schema = _get("/api/config/thresholds/schema")
    eff = _get("/api/config/thresholds")
    assert schema["ok"] and eff["ok"]

    schema_keys = {it["key"] for g in schema["groups"] for it in g["items"]}
    eff_keys = {k for g in eff["effective"].values() for k in g}
    assert schema_keys == eff_keys, (
        f"表单里有的键生效值里没有 {sorted(schema_keys - eff_keys)}；"
        f"生效值里有表单不认识的键 {sorted(eff_keys - schema_keys)}"
    )
    assert len(schema_keys) == 25, f"本轮 THRESHOLD_SPEC 应为 25 个键，实际 {len(schema_keys)}"
    assert [g["group"] for g in schema["groups"]] == ["diff", "tiering", "notify"]

    # 每个键都得能说出"当前来自哪一层"，否则面板上的「当前来自」会空着
    sources = {k for g in eff["sources"].values() for k in g}
    assert sources == schema_keys


# ============================================================
# ④ 数据健康：数字要能和库里的 collect_runs 对上
# ============================================================


def test_health_rounds_match_collect_runs():
    health = _get("/api/panel/health?days=7")
    assert health["ok"] is True
    rounds = health["rounds"]

    total_rows = _sql("select count(*) from collect_runs")[0][0]
    ok_rows = _sql("select count(*) from collect_runs where ok = 1")[0][0]
    assert rounds["total"] == total_rows, f"页面说 {rounds['total']} 轮，库里 {total_rows} 行"
    assert rounds["ok"] == ok_rows
    assert rounds["failed"] == total_rows - ok_rows
    assert rounds["success_rate"] == round(ok_rows / total_rows, 3)
    assert len(health["failures"]) == rounds["failed"], "失败轮次列出的条数要与计数一致"

    for day in health["price_trust"]:
        assert day["rounds"] == day["price_known"] + day["price_unknown"], (
            "价格可知 + 不可知 必须等于当天总数（不许把「不知道」算成 0）"
        )

    # 新鲜度只算在盯的（active + paused），归档的不进这张表
    in_pool = _sql("select count(*) from competitors where status in ('active','paused')")[0][0]
    assert len(health["snapshot_freshness"]) == in_pool


# ============================================================
# ⑤ unknown 的显示链路
# ============================================================


def test_unknown_tier_is_explained_not_hidden():
    res = _get("/api/cap/list_competitors?tier=unknown")  # GET 只给元数据
    assert res["ok"] is True and "params" in res

    status, data = _post("/api/cap/list_competitors", {"tier": "unknown"})
    assert status == 200 and data["ok"] is True
    assert data["count"] >= 1, "演示数据里刻意留了一个证据不足的竞品"
    for it in data["items"]:
        assert it["tier"] == "unknown"
        assert it["tier_label"] and "待判定" in it["tier_label"], it["tier_label"]
        assert it["tier_missing"], "「待判定」必须说清缺什么证据，否则用户以为是系统坏了"
        # 绝不能显示成 C 或空白
        assert it["tier_label"] not in ("C", "C 级", "")

    # 面板两处（看板 / 竞品池）拿到的是同一份结构
    board = {i["asin"]: i for i in _get("/api/panel/board")["items"]}
    pool = {i["asin"]: i for i in _get("/api/panel/pool?status=active,paused,archived")["items"]}
    unknown_pool = [a for a, i in pool.items() if i["tier"] == "unknown"]
    assert unknown_pool
    for asin in unknown_pool:
        if asin in board:
            assert board[asin]["tier_label"] == pool[asin]["tier_label"]
            assert board[asin]["tier_missing"] == pool[asin]["tier_missing"]


def test_unknown_competitor_does_not_break_threshold_or_binding_endpoints():
    """证据不足的竞品是所有接口都要能兜住的形状：阈值/绑定/历史都不许崩。"""
    _, listed = _post("/api/cap/list_competitors", {"tier": "unknown"})
    asin = listed["items"][0]["asin"]

    for name, body in (
        ("get_change_history", {"asin": asin, "days": 30}),
        ("list_bindings", {}),
        ("get_competitor_detail", {"asin": asin}),
        ("get_my_status", {}),
    ):
        status, data = _post(f"/api/cap/{name}", body)
        assert status == 200 and data["ok"] is True, f"{name} 在 unknown 竞品上失败：{data}"

    _, thr = _post("/api/cap/get_effective_thresholds", {})
    assert thr["ok"] is True and thr["effective"]["diff"]["price_p0_pct"]


# ============================================================
# ⑥ 试算：方向性 + 分等级 + 零写库
# ============================================================


def test_simulate_direction_is_graded_and_writes_nothing():
    current_p0 = _get("/api/config/thresholds")["effective"]["diff"]["price_p0_pct"]
    signals_before = _sql("select count(*) from signals")[0][0]

    def sim(value: float) -> dict:
        status, data = _post(
            "/api/config/thresholds/simulate",
            {"values": {"diff": {"price_p0_pct": value}}, "days": 7},
        )
        assert status == 200 and data["ok"] is True, data
        for key in ("p0", "p1", "p2", "total"):
            assert key in data["current"] and key in data["candidate"] and key in data["delta"], (
                f"试算必须分等级给出，缺 {key}"
            )
        assert data["current"]["total"] == sum(
            data["current"][k] for k in ("p0", "p1", "p2")
        ), "合计必须等于三档之和"
        return data

    loose = sim(0.10)     # 放松：P0 严格减少
    tight = sim(0.001)    # 收紧：P0 严格增加
    same = sim(current_p0)  # 拿当前值试算：delta 全 0

    assert loose["candidate"]["p0"] < loose["current"]["p0"], "放松阈值后 P0 应当减少"
    assert tight["candidate"]["p0"] > tight["current"]["p0"], "收紧阈值后 P0 应当增加"
    assert all(v == 0 for v in same["delta"].values()), f"当前值试算不该有变化：{same['delta']}"

    # 三个方向的 total 都不该变：价格变化只是换了个等级，信号条数没少
    assert loose["delta"]["total"] == 0 and tight["delta"]["total"] == 0
    assert signals_before == _sql("select count(*) from signals")[0][0], "试算必须零写库"


# ============================================================
# ⑥b 阈值页的「这段什么意思」：能力必须真的露在面板上
# ============================================================
#
# 背景：`explain_thresholds` 原本只有 REST / Agent 两个出口，面板零调用点。
# 对"只看面板"的主要用户，这个能力等于不存在——而阈值页最容易犯的错恰恰是
# "用户不知道这串数字意味着什么"。所以这一组用例把接线钉死，防止被删回去。


def test_config_view_wires_explain_lazily_and_with_product_id():
    """静态断言：`config.js()` 真的接了 explain，且是懒加载、且带 `my_product_id`。"""
    from app.web.views import config as config_view

    src = config_view.js()

    # 1) 真的请求了这个能力
    assert "/api/config/thresholds/explain" in src, (
        "阈值页没有接 explain——这个能力又变回只有 REST/Agent 能碰，"
        "对只看面板的用户等于不存在"
    )

    # 2) 懒加载：只能挂在 details 的 toggle 上，不能塞进 register('config')
    assert '<details class="exp" data-explain=' in src, "说明区不是可展开/收起的块"
    assert "addEventListener('toggle'" in src, (
        "说明区不是展开才加载——三个分组会把接口预拉三次"
    )
    register_block = re.search(r"register\('config',[\s\S]*?\n\}\);", src)
    assert register_block, "找不到 register('config')，用例失效"
    assert "explain" not in register_block.group(0), (
        "配置页在 register 里预拉了说明——懒加载退化，每次打开这一屏多打三次接口"
    )

    # 3) 拼 URL 时必须带 my_product_id（这是防回退的核心）
    fn = re.search(r"async function cfgExplain\(group\)\{([\s\S]*?)\n\}", src)
    assert fn, "找不到 cfgExplain，用例失效"
    body = fn.group(1)
    assert "my_product_id=" in body, (
        "explain 的 URL 没带 my_product_id——说明就只剩公司层，"
        "设过产品层覆盖的人会看到「不是当前生效的值」，还以为是系统算错"
    )
    assert "CFG.ctx.my_product_id" in body, "my_product_id 应取面板上下文里的锚点"
    assert "CFG.scope" not in body, (
        "my_product_id 不该跟 CFG.scope 走：芯片选的是「你要改哪一层」，"
        "而这段说明讲的是「当前实际生效的是哪一层」"
    )

    # 4) 个人覆盖必须单独渲染，不能只靠 text 里那句 ⚠
    assert "personal_overrides_not_applied" in body, (
        "个人覆盖只塞在 text 里了——用户扫一眼看不到「我设的 3% 没生效」，"
        "而这正是他这个人在本屏最该知道的一件事"
    )
    assert "warnbox" in body and "产品层或公司层" in body, "警示行没写「怎么才能让它生效」"


def test_explain_endpoint_works_with_and_without_product_id():
    """活调一次：带与不带 `my_product_id` 都必须 200 + ok，且来源层如实。"""
    ctx = _get("/api/panel/context")
    my_id = ctx.get("my_product_id")
    schema = _get("/api/config/thresholds/schema")
    diff_keys = {i["key"] for g in schema["groups"] if g["group"] == "diff" for i in g["items"]}

    # --- 不带 my_product_id：空库/无锚点也必须能看 ---
    plain = _get("/api/config/thresholds/explain?group_name=diff")
    assert plain["ok"] is True, plain
    assert plain["my_product_id"] is None, plain
    assert set(plain["effective"]) == diff_keys, "说明的键集与 schema 不一致"
    assert "（当前生效）" in plain["text"].splitlines()[0], plain["text"]
    # diff 组不含个人层：没传身份时当然也不该有
    assert plain["personal_layer_applied"] is False, plain
    assert plain["personal_overrides_not_applied"] == [], plain
    assert all(v in ("company", "product", "default") for v in plain["sources"].values()), (
        f"diff 的来源层里出现了个人层：{plain['sources']}"
    )

    # --- 带 my_product_id：产品层覆盖要能在说明里看见 ---
    if not my_id:                 # 演示库一定锚好了；真没有就只验上面那条
        return
    with_pid = _get(f"/api/config/thresholds/explain?group_name=diff&my_product_id={my_id}")
    assert with_pid["ok"] is True, with_pid
    assert with_pid["my_product_id"] == my_id, with_pid
    assert set(with_pid["effective"]) == diff_keys, "带锚点后键集不该变"
    assert "user" not in set(with_pid["sources"].values()), (
        "diff 组把个人层算进了「当前生效」——D5 复发"
    )


# ============================================================
# ⑦ 错误路径：永远 200 + ok:false + 人话
# ============================================================


def test_error_paths_are_200_with_hint():
    cases = [
        ("GET", "/api/panel/competitors/NOSUCHASIN999/changes?days=30", None),
        ("GET", "/api/panel/pool?status=bogus", None),
        ("GET", "/api/panel/actions?user_id=abc", None),
        ("POST", "/api/config/thresholds", {"scope": "company", "group_name": "diff",
                                            "values": {"price_p0_pct": 99}}),
        ("POST", "/api/config/thresholds/rollback", {"scope": "company", "group_name": "diff",
                                                     "version": 999}),
        ("POST", "/api/panel/pool/NOSUCH/status", {"status": "active"}),
        ("POST", "/api/panel/actions", {"action_type": None}),
    ]
    for method, path, body in cases:
        if method == "GET":
            res = client.get(path)
            data = res.json()
        else:
            res = client.post(path, json=body)
            data = res.json()
        assert res.status_code == 200, f"{path} 应当是 200（业务失败不是 HTTP 错误），实际 {res.status_code}"
        assert data.get("ok") is False, f"{path} 应当 ok:false，实际 {data.get('ok')}"
        assert data.get("error"), f"{path} 的 error 不能为空"
        assert data.get("hint"), f"{path} 的 hint 不能为空（前端靠它显示人话）"


def test_bad_json_on_config_endpoints_still_gives_hint():
    """坏 JSON 也是用户会遇到的（前端拼错、脚本贴错），它必须和别的错误一样能行动。

    已知缺陷：`app/api/routes/config.py` 的 `_body()` 在坏 JSON 时只给 error，不给 hint。
    """
    for path in ("/api/config/thresholds", "/api/config/thresholds/simulate",
                 "/api/config/thresholds/rollback"):
        status, data = _post_raw(path, b"{bad json")
        assert status == 200, f"{path} 坏 JSON 应当是 200，实际 {status}"
        assert data.get("ok") is False
        assert data.get("error")
        assert data.get("hint"), f"{path} 坏 JSON 时没有 hint（前端只能显示一行干巴巴的 error）"

    status, data = _post_raw("/api/config/thresholds/simulate", b"[1,2,3]")
    assert status == 200 and data.get("ok") is False and data.get("hint")


def test_capability_param_errors_carry_hint():
    """能力层「参数缺失 / 枚举非法」也必须给 hint —— 面板与 Agent 共用这条出口。

    已知缺陷：`app/capabilities/__init__.py` 的 ParamError 只带 error，不带 hint，
    而 `/api/cap/{name}` 原样返回，于是前端拿到一条没有建议的错误。
    """
    cases = [
        ("list_competitors", {"tier": "Z"}),               # 枚举非法
        ("get_competitor_detail", {}),                      # 缺必填参数 asin
    ]
    # 原先这里还有一条 `get_effective_thresholds {"scope": "nope"}`，以为它会报错。
    # 它不会：这个能力的参数表里**根本没有** `scope`，而未声明的参数是被刻意忽略的
    # （见 `_coerce_params` 的注释——模型常多带顺手字段，为此让整次调用失败不划算）。
    # 把"不存在的参数"当成"非法取值"来断言，测的是测试自己的误解，不是产品行为。
    for name, body in cases:
        status, data = _post(f"/api/cap/{name}", body)
        assert status == 200, f"{name} 应当是 200，实际 {status}"
        assert data.get("ok") is False, f"{name} 应当 ok:false"
        assert data.get("error"), f"{name} 的 error 不能为空"
        assert data.get("hint"), f"{name} 参数校验失败时没有 hint：{data.get('error')}"


def test_board_battlefield_column_agrees_with_pool():
    """看板「战场（自家产品）」列与竞品池必须对同一个竞品说同一句话。

    这条用例原来钉的是**缺陷本身**（看板按 `my_asin` 判、池子按 `binding_id` 判）。
    缺陷修完后再跑它会红，但**红的原因是它自己还在按旧判据算**——这正是
    "缺陷用例翻成正向断言"时必须小心的地方：用例变绿不能证明修好了。
    所以这里不是把 `my_asin` 换成 `binding_id` 就完事（那样两边同一个表达式，
    永远相等，等于什么都没验），而是同时钉三件事：

    1. 两边的判据必须一致（binding_id）；
    2. `my_asin` 对**未绑定**的竞品也有值——把这个"看着像证据其实不是"的字段
       钉死，防止有人再把绑定判断写回它；
    3. board.py 的源码里真的没有用 `bf.my_asin` 当条件（静态兜底，
       防"接口对了但渲染没改"）。
    """
    board = _get("/api/panel/board")["items"]
    pool = {i["asin"]: i for i in _get("/api/panel/pool?status=active,paused,archived")["items"]}

    disagreements = []
    unbound_with_my_asin = []
    for it in board:
        bf = it.get("battlefield") or {}
        board_claims_bound = bool(bf.get("binding_id"))     # 修好后的判据
        pool_claims_bound = bool(pool.get(it["asin"], {}).get("battlefield", {}).get("binding_id"))
        if board_claims_bound != pool_claims_bound:
            disagreements.append((it["asin"], bf.get("my_asin"), bf.get("binding_id")))
        if not bf.get("binding_id"):
            unbound_with_my_asin.append((it["asin"], bf.get("my_asin")))

    assert not disagreements, (
        f"{len(disagreements)} 个竞品两个页面说法不同（asin, my_asin, binding_id）：{disagreements}"
    )

    # 这条是本用例的"防回退"核心：只要还有未绑定的竞品带着非空 my_asin，
    # 就说明 `my_asin` 不能当绑定证据——谁把它改回判据，这里立刻有意义。
    stray = [x for x in unbound_with_my_asin if x[1]]
    assert stray, (
        "演示数据里已经没有『未绑定但 my_asin 有值』的竞品了，"
        "这条防回退断言失去意义——请补一条这样的数据，别把它删掉"
    )

    from app.web.views import board as board_view

    src = board_view.js()
    assert "bf.binding_id" in src, "看板渲染没用 binding_id 作判据"
    assert "bf.my_asin ?" not in src, (
        "看板渲染又改回按 bf.my_asin 判断绑定了——未绑定的竞品会显示成已绑定"
    )


# ============================================================
# ⑧ 空库：五视图都必须给空态，不许 500 / 白屏
# ============================================================


def test_empty_db_renders_all_views_without_500():
    """另起一个空库，把五视图的端点全打一遍。

    做法：把 `app.db` 的引擎/SessionLocal 换到空库，跑完还原。
    所有服务都是通过 `app.db.get_session` 拿会话的，所以换掉模块级的 SessionLocal
    就等于换库，不用重启进程。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    empty = _TMP_DIR / "empty.db"
    old_url = settings.database_url
    try:
        # 换库只能走 settings + reset_engine（不要直接改 app_db.engine）：
        # `get_session()` 里的 `_sync_engine()` 比的是 settings 与 engine
        # 实际绑的 url，直接改模块属性会被它在下一个 session 上纠正回来——
        # 于是"空库"用例读到的是上一个模块的数据（实测 pairs=47 而不是 0）。
        settings.database_url = f"sqlite:///{empty.as_posix()}"
        app_db.reset_engine()
        app_db.init_db()

        paths = [
            "/api/panel/context",
            "/api/panel/board",
            "/api/panel/pool?status=active,paused,archived",
            "/api/panel/actions?days=30",
            "/api/panel/health?days=7",
            "/api/config/thresholds/schema",
            "/api/config/thresholds",
        ]
        for path in paths:
            res = client.get(path)
            assert res.status_code == 200, f"空库上 {path} 返回 {res.status_code}"
            assert res.json().get("ok") is True, f"空库上 {path} 应当是 ok:true（空态不是错误）"

        board = _get("/api/panel/board")
        assert board["summary"]["total"] == 0 and board["items"] == []
        assert board["summary"]["by_tier"] == {}
        assert board["summary"]["week_changes"] == 0

        health = _get("/api/panel/health?days=7")
        assert health["rounds"] == {
            "total": 0, "ok": 0, "failed": 0, "partial": 0, "success_rate": None
        }, health["rounds"]
        assert health["price_trust"] == [] and health["snapshot_freshness"] == []

        # 面板页面本身与库无关：空库下仍是同一个 61KB 单文件
        html = _panel_html()
        from app.web.views import VIEWS

        assert html.count('class="view-body"') == len(VIEWS)
    finally:
        settings.database_url = old_url
        app_db.reset_engine()


def test_empty_db_simulate_and_pool_are_not_crashes():
    """空库上的写路径：不该 500，应该是 200 + ok:false + 人话。"""
    empty = _TMP_DIR / "empty2.db"
    old_url = settings.database_url
    try:
        settings.database_url = f"sqlite:///{empty.as_posix()}"
        app_db.reset_engine()
        app_db.init_db()

        status, data = _post(
            "/api/config/thresholds/simulate",
            {"values": {"diff": {"price_p0_pct": 0.05}}, "days": 7},
        )
        assert status == 200 and data["ok"] is True, data
        assert data["window"]["pairs"] == 0, "空库上没有可对比的样本，但结构要给全"
        assert data["current"] == {"p0": 0, "p1": 0, "p2": 0, "total": 0}

        status, data = _post("/api/panel/pool/B0NOPE/status", {"status": "paused"})
        assert status == 200 and data["ok"] is False and data["hint"]
    finally:
        settings.database_url = old_url
        app_db.reset_engine()


# ============================================================
# ⑨ 兜底：主库一个字节都不许动
# ============================================================


def test_never_touches_main_db():
    assert _seed_fingerprint() == _MAIN_DB_SIG, (
        "主库 data/competitor_monitor.db 被改动了——测试必须只碰临时库"
    )


# ============================================================
# 运行器
# ============================================================

#: 断言的是「应有行为」，失败即为已知缺陷（计入「缺陷复现」，不算意外失败）。
KNOWN_DEFECTS = {
    "test_bad_json_on_config_endpoints_still_gives_hint",
    "test_capability_param_errors_carry_hint",
    "test_board_battlefield_column_agrees_with_pool",
}


def main() -> int:
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    defects: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []

    try:
        for name, fn in tests:
            try:
                fn()
                passed += 1
                print(f"  ✅ {name}")
            except Exception:  # noqa: BLE001
                tb = traceback.format_exc()
                if name in KNOWN_DEFECTS:
                    defects.append((name, tb))
                    print(f"  ⚠️  {name}　（已知缺陷，仍可复现）")
                else:
                    failed.append((name, tb))
                    print(f"  ❌ {name}")

        print("")
        print(f"通过 {passed}/{len(tests)}　缺陷复现 {len(defects)}　意外失败 {len(failed)}")

        for name, tb in defects:
            last = [ln for ln in tb.strip().splitlines() if ln.strip()][-1]
            print(f"  ⚠️  {name}\n      {last[:400]}")

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
