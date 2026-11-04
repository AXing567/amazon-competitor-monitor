"""数据库连接与会话管理。

MVP 阶段使用 SQLite；切换到 PostgreSQL 只需改 .env 里的 DATABASE_URL。
所有模型都通过 SQLAlchemy 抽象，不写任何方言相关的 SQL。

## 双模式（2026-11-04 新增）

面板可以在运行时切换看**两个物理隔离的库**：

    真实模式  LIVE -> settings.database_url      （data/competitor_monitor.db）
    Mock 模式 MOCK -> settings.mock_database_url （data/mock.db）

模式存在 `contextvars.ContextVar` 里，而不是某个全局可变变量：FastAPI 的同步端点
跑在线程池里，一个请求把模式改成 mock 的同时，另一个请求可能正在读真实库。
ContextVar 的每个请求一份副本，正是这里需要的隔离粒度（线程局部变量做不到——
同一个线程会串行处理不同请求，残留值会跨请求泄漏）。

为什么必须切库而不是切 `tenant_id`：
1. Mock 数据集要能**整体重建、整体丢弃**，切租户做不到「这条库里的数据全不算数」；
2. Mock 库还要自带用户/阈值/绑定才自洽，塞进真实库就等于「每张表都有两种行」，
   而任何一处查询忘了过滤都不会报错——只会让 Mock 竞品安静地混进真实池子。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import logging
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


# ============================================================
# 数据模式
# ============================================================

#: 真实数据。这是唯一会被当真、会被推送、会被拿去做决定的地方。
DATA_MODE_LIVE = "live"
#: Mock 数据。整套演示数据，可随时重建；**任何结论都不该据此下决定**。
DATA_MODE_MOCK = "mock"

DATA_MODES: tuple[str, ...] = (DATA_MODE_LIVE, DATA_MODE_MOCK)

MODE_LABELS: dict[str, str] = {
    DATA_MODE_LIVE: "真实数据",
    DATA_MODE_MOCK: "Mock 数据",
}

_current_mode: ContextVar[str] = ContextVar("cm_data_mode", default=DATA_MODE_LIVE)


def get_data_mode() -> str:
    """当前请求正看着哪个库。默认真实模式——**失败要往「当真」的方向失败**。"""
    return _current_mode.get()


def coerce_data_mode(raw) -> str:
    """把外部输入（请求头 / query / 表单）归一成合法模式，非法值一律抛错。

    为什么不静默退回 live：一个拼错的 `X-Data-Mode: mok` 如果被悄悄当成 live，
    用户会以为自己在看演示数据，实际看的却是真实库——反过来也一样危险。
    """
    mode = (raw or "").strip().lower()
    if not mode:
        return DATA_MODE_LIVE
    if mode not in DATA_MODES:
        raise ValueError(
            f"数据模式只能是 {'/'.join(DATA_MODES)}，收到 {raw!r}"
        )
    return mode


def set_data_mode(mode: str) -> str:
    """设定当前上下文的数据模式。调用方负责先 `coerce_data_mode`。"""
    if mode not in DATA_MODES:
        raise ValueError(f"数据模式只能是 {'/'.join(DATA_MODES)}，收到 {mode!r}")
    _current_mode.set(mode)
    return mode


@contextmanager
def using_data_mode(mode: str):
    """临时切模式，出块自动还原。给脚本和测试用。"""
    token = _current_mode.set(coerce_data_mode(mode))
    try:
        yield _current_mode.get()
    finally:
        _current_mode.reset(token)


def bind_data_mode(mode) -> object:
    """给 ASGI 中间件用：钉住本请求的模式，返回 token。

    为什么不直接用 `using_data_mode` 上下文管理器：中间件要 `await` 下游应用，
    而 `with` 语句跨 await 会把"进入/退出"和"请求处理"绑在同一个协程帧里，
    一旦下游抛异常、或者框架内部做了 task 分组，退出就会发生在错误的上下文里。
    token 由中间件显式 `reset`，语义是"这个请求从开始到结束都是这个模式"。
    """
    return _current_mode.set(coerce_data_mode(mode))


def unbind_data_mode(token) -> None:
    """还原中间件钉的模式。必须放在 `finally` 里——否则一个请求的 Mock 标记
    会漏给下一个复用同一上下文的请求（表现为"切回真实模式后还在看 Mock 库"，
    而且刷新页面就好了，查起来极费劲）。"""
    try:
        _current_mode.reset(token)
    except ValueError:      # pragma: no cover —— token 已被别的上下文重置
        pass


def url_for_mode(mode: str) -> str:
    return settings.mock_database_url if mode == DATA_MODE_MOCK else settings.database_url


def db_file_path(url: str | None = None, mode: str | None = None) -> Path | None:
    """连接串 → 库文件绝对路径。非 SQLite 返回 None（PostgreSQL 没有"这个文件"）。

    为什么要规范化成绝对路径：`data/x.db`、`./data/x.db`、`sqlite:///data/x.db`
    指的是同一个文件，但字符串互不相等。判断「Mock 和真实是不是同一个库」时
    只比字符串会漏判——而漏判的后果是 Mock 数据写进真实库，且此后分不清。
    """
    raw_url = url or url_for_mode(mode or get_data_mode())
    if not raw_url.startswith("sqlite:///"):
        return None
    path = Path(raw_url.split("sqlite:///", 1)[-1])
    if not path.is_absolute():
        path = settings_base_dir() / path
    try:
        return path.resolve()
    except OSError:            # pragma: no cover —— 盘符不存在等
        return path


def _ensure_sqlite_dir(url: str) -> None:
    """确保 SQLite 文件的父目录存在。

    `sqlite:///./data/xxx.db` 这种相对路径，目录不存在时 SQLite 不会自动创建，
    直接报 "unable to open database file"——第一次跑必踩，所以这里主动兜住。
    """
    path = url.split("sqlite:///", 1)[-1]
    if not path or path == ":memory:":
        return
    db_file = Path(path)
    if not db_file.is_absolute():
        db_file = settings_base_dir() / db_file
    db_file.parent.mkdir(parents=True, exist_ok=True)


def settings_base_dir() -> Path:
    from app.config import BASE_DIR

    return BASE_DIR


def _build_engine(url: str):
    kwargs: dict = {"echo": False, "future": True}

    if url.startswith("sqlite"):
        # SQLite 需要允许跨线程使用（FastAPI 在线程池里跑同步代码）
        kwargs["connect_args"] = {"check_same_thread": False}
        _ensure_sqlite_dir(url)
    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


def _normalized_url(url: str) -> str:
    """把连接串规范化，用于比较「还是不是同一个库」。

    为什么不能直接字符串比较：`sqlite:///./data/x.db`、`sqlite:///data/x.db`、
    `sqlite:///C:/proj/data/x.db` 指的是同一个文件，但三个字符串都不相等。
    直接比会得到「永远不相等 → 每次都重建 engine」——测试里表现为每个 session
    都 dispose 一次（慢但能跑），生产里则是连接池永远失效。
    反过来只比文件名会得到「永远相等 → 换了库也不重建」，那才是真正危险的。
    """
    if url.startswith("sqlite:///"):
        raw = url.split("sqlite:///", 1)[1]
        if raw and raw != ":memory:":
            path = Path(raw)
            if not path.is_absolute():
                path = settings_base_dir() / path
            try:
                resolved = path.resolve()
            except OSError:            # Windows 上盘符不存在等情况
                resolved = path
            return "sqlite:///" + resolved.as_posix()
    return url


class _EngineProxy:
    """对外暴露的 `engine` 名字，永远指向**当前模式**那个真实 engine。

    为什么不直接把 `engine` 换成新对象：`from app.db import engine` 拿到的是一根
    **绑定在 import 那一刻的值**。测试里 `from app.db import engine as app_engine`
    之后再去 `app_engine.connect()`，拿到的是旧 engine——于是"表明明建了，
    PRAGMA 出来却是空集"（实测 `test_04` 断言 actions 有 5 个新列，读到空集）。
    代理把这根引用的生命周期和 engine 的替换解耦：外部拿到的永远是同一个对象，
    它内部指向哪个库由「当前数据模式 + 连接串是否变过」决定。

    引入双模式后这层代理更有必要了：engine 现在不只是测试期会换，
    面板上一个下拉框就能换掉它，而所有 `from app.db import engine` 的旧引用
    必须跟着换——否则它们会继续往旧库写，而读的是新库。
    """

    __slots__ = ()

    def __getattr__(self, name):                 # 方言、url、pool 等一律转发
        return getattr(_real_engine(), name)

    def connect(self, *args, **kwargs):
        return _real_engine().connect(*args, **kwargs)

    def begin(self, *args, **kwargs):
        return _real_engine().begin(*args, **kwargs)

    def dispose(self):
        return _real_engine().dispose()

    def __repr__(self) -> str:                   # 日志里能看到真实连接串
        return f"<EngineProxy {_real_engine().url}>"


#: 每个模式一个 engine + 一个 session 工厂 + 它当前绑的规范化连接串。
#: 按模式分开缓存，而不是「一个全局 engine 用完就扔」：切换模式是面板上的常规操作，
#: 每次都 dispose 再重连会让「点一下 Mock 看一眼再切回来」变成几秒的卡顿，
#: 而连接池本来就是为这种情况存在的。
_engines: dict[str, object] = {}
_sessionmakers: dict[str, sessionmaker] = {}
_bound_urls: dict[str, str] = {}


def _real_engine():
    return _engine_for(get_data_mode())


def _engine_for(mode: str):
    """取该模式的 engine；连接串变过（测试改了配置）就重建。"""
    url = url_for_mode(mode)
    normalized = _normalized_url(url)
    cached = _engines.get(mode)
    if cached is not None and _bound_urls.get(mode) == normalized:
        return cached

    if cached is not None:
        logger.warning(
            "数据模式 %s 的连接串已变更，重建 engine：%s → %s",
            mode,
            _bound_urls.get(mode),
            normalized,
        )
        try:
            cached.dispose()
        except Exception:      # pragma: no cover —— 旧 engine 关不掉不该阻塞重建
            pass
    _engines[mode] = _build_engine(url)
    _bound_urls[mode] = normalized
    _sessionmakers.pop(mode, None)
    return _engines[mode]


def _sessionmaker_for(mode: str) -> sessionmaker:
    factory = _sessionmakers.get(mode)
    if factory is None:
        factory = sessionmaker(
            bind=_engine_for(mode), autoflush=False, expire_on_commit=False
        )
        _sessionmakers[mode] = factory
    return factory


class _SessionFactoryProxy:
    """`SessionLocal` 这个名字的可调用代理：`SessionLocal()` 给当前模式的会话工厂。

    为什么还要留 `SessionLocal` 这个名字而不是改叫 `session_factory()`：
    测试里有 3 处直接替换模块属性 `db_mod.SessionLocal = sessionmaker(...)` 来把整个
    应用钉到临时库上。改名会让那些替换静默失效——而失效的表现是"测试写进了真实库"，
    不是报错。保持可替换，同时让它按模式解析。
    """

    __slots__ = ()

    def __call__(self, **kwargs) -> Session:
        return _sessionmaker_for(get_data_mode())(**kwargs)

    def __repr__(self) -> str:
        return f"<SessionFactoryProxy mode={get_data_mode()}>"


engine = _EngineProxy()
SessionLocal = _SessionFactoryProxy()


def reset_engine() -> None:
    """强制重建**所有模式**的 engine 与 session 工厂。**只给测试用。**

    生产为什么不需要它：生产只有一份配置，进程起来之后不该换库——换了意味着
    此前写入的数据在一个库、此后写入的在另一个库，而两者都"看起来正常"。
    （面板切模式走的是 `set_data_mode`，那是**选择**看哪个库，不是把库换掉：
    两个 engine 各自指向自己的文件，没有"数据散在两个库"的问题。）

    这个函数存在的唯一理由是测试：pytest 在同一个进程里先 import 完所有模块
    再跑用例，于是各模块在 import 期设的 `DATABASE_URL` 只有最后一个生效，
    所有用例会挤在同一个库上互相污染。有了重绑能力，conftest 可以给每个
    测试模块一份独立的空库。
    """
    for mode, cached in list(_engines.items()):
        try:
            cached.dispose()
        except Exception:      # pragma: no cover
            pass
    _engines.clear()
    _sessionmakers.clear()
    _bound_urls.clear()
    _initialized.clear()


def _sync_engine() -> None:
    """确保当前模式的 engine 与配置一致。每次取会话/建表前调一次。"""
    _engine_for(get_data_mode())


#: 哪些模式的库已经建过表。只在进程内记，进程重启后第一访问会重建一次（幂等）。
_initialized: set[str] = set()


def ensure_mode_ready(mode: str | None = None) -> list[str]:
    """确保该模式的库存在且表齐全。返回本次真正应用的迁移动作。

    为什么要单独一个入口：面板一启动就会读「当前模式」的库来决定显示什么，
    而 Mock 库很可能是**第一次被访问**（文件都还不存在）。此时如果先读后建，
    用户看到的是「no such table: my_products」——一句和「你还没生成 Mock 数据」
    完全无关的错误。
    """
    target = mode or get_data_mode()
    if target in _initialized:
        return []
    with using_data_mode(target):
        applied = init_db()
    _initialized.add(target)
    return applied


def init_db() -> list[str]:
    """建表 + 幂等迁移。可重复调用，返回本次真正应用的迁移动作（首次之后为空列表）。

    **顺序必须是「先 create_all 再 migrate」，不能反**：
    migrate 的 `PRAGMA table_info` 在表不存在时返回空集，于是该表的加列与建索引
    都被跳过（`migrate_report()["skipped"]` 能看到原因），交给 create_all 整表建好；
    反过来的话，新建的表会绕过 migrate 的加列逻辑。本轮新表本来就是全新建的
    （create_all 直接建全列），但下一轮给新表加字段时，这个顺序差就会变成 bug。
    """
    from app import models  # noqa: F401  确保模型被注册

    _sync_engine()                          # 连接串被换过就先重建（测试用）
    Base.metadata.create_all(bind=engine)   # 新表建表（幂等：已存在则跳过）

    from app.migrations import migrate

    applied = migrate(engine)               # 老表加列 + 补索引（幂等、原子）
    if applied:
        logger.info("迁移已应用：%s", applied)
    return applied


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """同步上下文管理器，供领域服务内部使用。"""
    _sync_engine()
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def db_session_dependency() -> Generator[Session, None, None]:
    """FastAPI 依赖注入版本。"""
    _sync_engine()
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def now() -> datetime:
    return datetime.now()
