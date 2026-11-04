"""数据模式：两个库必须真的隔离，切错了要能立刻看出来。

这一组测试守的是「面板上那个下拉框到底切了什么」。它出错的形态很安静：
用户以为在看 Mock，实际读的是真实库——页面上数字齐全、坐标正常、
没有任何报错，只是结论是错的。所以每条断言都尽量做成「两条路各走一遍再对比」，
而不是只验一条路通了。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import (
    DATA_MODE_LIVE,
    DATA_MODE_MOCK,
    bind_data_mode,
    coerce_data_mode,
    db_file_path,
    get_data_mode,
    get_session,
    unbind_data_mode,
    url_for_mode,
    using_data_mode,
)
from app.models import MyProduct


# ============================================================
# 模式值的解析
# ============================================================


def test_coerce_accepts_both_modes_case_insensitively():
    assert coerce_data_mode("live") == DATA_MODE_LIVE
    assert coerce_data_mode("mock") == DATA_MODE_MOCK
    assert coerce_data_mode("MOCK") == DATA_MODE_MOCK
    assert coerce_data_mode("  Mock  ") == DATA_MODE_MOCK


def test_coerce_defaults_to_live_when_absent():
    """没传就是真实模式。判据的方向很重要：宁可"当真"，不可"当假"。"""
    assert coerce_data_mode(None) == DATA_MODE_LIVE
    assert coerce_data_mode("") == DATA_MODE_LIVE


def test_coerce_rejects_unknown_value():
    """拼错的值必须炸，不能悄悄退回某一个模式。

    退回 live：用户以为在看 Mock，实际看真库 → 把真数据当演示。
    退回 mock：用户以为在看真数据，实际看假的 → 拿假数据做决定。
    两个方向都有害，所以第三条路是报错。
    """
    with pytest.raises(ValueError) as exc:
        coerce_data_mode("mok")
    assert "mok" in str(exc.value)
    assert "live" in str(exc.value) and "mock" in str(exc.value)


# ============================================================
# 路径规范化：守卫靠它，错一次就白守
# ============================================================


def test_db_file_path_normalizes_to_absolute(tmp_path, monkeypatch):
    """同一个文件的不同写法必须得到同一个路径。

    `Mock 数据和真实数据是不是同一个库` 这个判断全靠它。
    只比字符串的话，`data/x.db` 和 `./data/x.db` 会被判成两个库——
    而那正好是守卫放行的方向。
    """
    from app.config import settings

    target = tmp_path / "same.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{target.as_posix()}")
    a = db_file_path(mode=DATA_MODE_LIVE)

    monkeypatch.setattr(
        settings, "database_url", f"sqlite:///{tmp_path.as_posix()}/./same.db"
    )
    b = db_file_path(mode=DATA_MODE_LIVE)

    assert a == b
    assert a is not None and a.is_absolute()


def test_db_file_path_returns_none_for_non_sqlite(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")
    assert db_file_path(mode=DATA_MODE_LIVE) is None


# ============================================================
# 上下文切换
# ============================================================


def test_using_data_mode_restores_on_exit():
    assert get_data_mode() == DATA_MODE_LIVE
    with using_data_mode(DATA_MODE_MOCK):
        assert get_data_mode() == DATA_MODE_MOCK
    assert get_data_mode() == DATA_MODE_LIVE


def test_using_data_mode_restores_even_on_exception():
    with pytest.raises(RuntimeError):
        with using_data_mode(DATA_MODE_MOCK):
            raise RuntimeError("boom")
    assert get_data_mode() == DATA_MODE_LIVE


def test_bind_and_unbind_pair():
    """中间件用的那一对。unbind 忘了调 = Mock 标记漏给下一个请求。"""
    token = bind_data_mode(DATA_MODE_MOCK)
    try:
        assert get_data_mode() == DATA_MODE_MOCK
    finally:
        unbind_data_mode(token)
    assert get_data_mode() == DATA_MODE_LIVE


# ============================================================
# 两个库真的不同
# ============================================================


def test_two_modes_point_at_different_files():
    live = db_file_path(mode=DATA_MODE_LIVE)
    mock = db_file_path(mode=DATA_MODE_MOCK)
    assert live is not None and mock is not None
    assert live != mock, "两个模式指向同一个文件——Mock 数据会污染真实库"
    assert url_for_mode(DATA_MODE_LIVE) != url_for_mode(DATA_MODE_MOCK)


def test_write_in_mock_is_invisible_in_live():
    """在一个模式里写一行，另一个模式必须看不见。这是整个功能的底线。

    注意开头的 `ensure_mode_ready`：conftest 只给**当前模式**建了表，
    Mock 库可能一次都没被访问过（文件都不存在）。直接切过去写就会撞
    `no such table: my_products`——那是一个和"隔离"完全无关的错误，
    排查时会往完全错误的方向找。API 路径由中间件统一兜住，脚本要自己调。
    """
    from app.db import ensure_mode_ready

    asin = "B0ISOLATE1"
    ensure_mode_ready(DATA_MODE_MOCK)

    with using_data_mode(DATA_MODE_MOCK):
        with get_session() as s:
            s.add(MyProduct(tenant_id="default", asin=asin, marketplace="US"))

    with using_data_mode(DATA_MODE_MOCK):
        with get_session() as s:
            found = s.execute(
                select(MyProduct).where(MyProduct.asin == asin)
            ).scalar_one_or_none()
        assert found is not None, "Mock 库自己应该读得到刚写的行"

    with using_data_mode(DATA_MODE_LIVE):
        with get_session() as s:
            leaked = s.execute(
                select(MyProduct).where(MyProduct.asin == asin)
            ).scalar_one_or_none()
        assert leaked is None, "Mock 库写入的行泄漏到了真实库"


def test_current_mode_does_not_leak_between_contexts():
    """管道两端各切各的模式，互不影响（ContextVar 的意义就在这）。"""
    with using_data_mode(DATA_MODE_MOCK):
        assert get_data_mode() == DATA_MODE_MOCK
    with using_data_mode(DATA_MODE_LIVE):
        assert get_data_mode() == DATA_MODE_LIVE


# ============================================================
# 中间件
# ============================================================


def test_middleware_switches_mode_by_header():
    from app.api.server import app

    with TestClient(app) as c:
        assert c.get("/api/dataset").json()["current"] == DATA_MODE_LIVE
        assert (
            c.get("/api/dataset", headers={"X-Data-Mode": "mock"}).json()["current"]
            == DATA_MODE_MOCK
        )


def test_middleware_rejects_bad_mode_with_400():
    from app.api.server import app

    with TestClient(app) as c:
        res = c.get("/api/dataset", headers={"X-Data-Mode": "mok"})
        assert res.status_code == 400
        body = res.json()
        assert body["ok"] is False
        assert "mok" in body["error"]


def test_middleware_falls_back_to_live_without_header():
    from app.api.server import app

    with TestClient(app) as c:
        assert c.get("/api/dataset").json()["current"] == DATA_MODE_LIVE


def test_dataset_reports_both_modes_with_file_paths():
    from app.api.server import app

    with TestClient(app) as c:
        body = c.get("/api/dataset").json()

    modes = {m["mode"]: m for m in body["modes"]}
    assert set(modes) == {DATA_MODE_LIVE, DATA_MODE_MOCK}
    for m in modes.values():
        assert m["db_file"], "面板要能显示这两个库各在哪个文件"
        assert m["error"] is None
    assert modes[DATA_MODE_LIVE]["db_file"] != modes[DATA_MODE_MOCK]["db_file"]
