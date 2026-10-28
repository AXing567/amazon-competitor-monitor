"""数据库连接与会话管理。

MVP 阶段使用 SQLite；切换到 PostgreSQL 只需改 .env 里的 DATABASE_URL。
所有模型都通过 SQLAlchemy 抽象，不写任何方言相关的 SQL。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


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


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """建表。幂等，可重复调用。"""
    from app import models  # noqa: F401  确保模型被注册

    Base.metadata.create_all(bind=engine)


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """同步上下文管理器，供领域服务内部使用。"""
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
