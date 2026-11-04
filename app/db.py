"""数据库连接与会话管理。

MVP 阶段使用 SQLite；切换到 PostgreSQL 只需改 .env 里的 DATABASE_URL。
所有模型都通过 SQLAlchemy 抽象，不写任何方言相关的 SQL。
"""
from __future__ import annotations

from contextlib import contextmanager
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


def _build_engine():
    url = settings.database_url
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
    """对外暴露的 `engine` 名字，永远指向**当前**那个真实 engine。

    为什么不直接把 `engine` 换成新对象：`from app.db import engine` 拿到的是一根
    **绑定在 import 那一刻的值**。测试里 `from app.db import engine as app_engine`
    之后再去 `app_engine.connect()`，拿到的是旧 engine——于是"表明明建了，
    PRAGMA 出来却是空集"（实测 `test_04` 断言 actions 有 5 个新列，读到空集）。
    代理把这根引用的生命周期和 engine 的替换解耦：外部拿到的永远是同一个对象，
    它内部指向哪个库由 `reset_engine()` 决定。

    生产不会走到替换分支（配置在进程内不变），所以这层代理对生产是纯透明的。
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


_real: dict = {"engine": None}
_real["engine"] = _build_engine()


def _real_engine():
    return _real["engine"]


engine = _EngineProxy()
SessionLocal = sessionmaker(bind=_real_engine(), autoflush=False, expire_on_commit=False)

#: 当前 engine 绑的是哪个库（规范化后）。`_sync_engine()` 拿它和
#: `settings.database_url` 比，不一致就重建。
_bound_url = _normalized_url(settings.database_url)


def reset_engine() -> None:
    """强制重建 engine 与 session 工厂。**只给测试用。**

    生产为什么不需要它：生产只有一份配置，进程起来之后不该换库——换了意味着
    此前写入的数据在一个库、此后写入的在另一个库，而两者都"看起来正常"。
    这个函数存在的唯一理由是测试：pytest 在同一个进程里先 import 完所有模块
    再跑用例，于是各模块在 import 期设的 `DATABASE_URL` 只有最后一个生效，
    所有用例会挤在同一个库上互相污染。有了重绑能力，conftest 可以给每个
    测试模块一份独立的空库。
    """
    global SessionLocal, _bound_url
    try:
        _real_engine().dispose()
    finally:
        _real["engine"] = _build_engine()
        SessionLocal = sessionmaker(
            bind=_real_engine(), autoflush=False, expire_on_commit=False
        )
        _bound_url = _normalized_url(settings.database_url)


def _sync_engine() -> None:
    """`settings.database_url` 变了就换 engine。每次取会话/建表前调一次。"""
    if _normalized_url(settings.database_url) != _bound_url:
        logger.warning(
            "数据库连接串已变更，重建 engine：%s → %s",
            _bound_url,
            _normalized_url(settings.database_url),
        )
        reset_engine()


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
