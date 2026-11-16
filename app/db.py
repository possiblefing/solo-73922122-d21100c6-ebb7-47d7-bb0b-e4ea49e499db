"""SQLAlchemy 引擎与会话管理。

所有写操作都在单个事务内完成：服务函数正常返回时提交，任何异常都会回滚，
保证「失败整次不变」。重启后关系与旧统一号含义全部保留在 PostgreSQL 中。
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def _engine_kwargs(url: str) -> dict:
    if url.startswith("postgresql"):
        # READ COMMITTED 已足够：所有互斥依赖行锁保证，查询始终只能看到
        # 已提交的数据，不会读到半成品事务。
        return {
            "pool_pre_ping": True,
            "pool_size": 10,
            "max_overflow": 20,
            "connect_args": {"connect_timeout": 10},
        }
    # SQLite 仅用于离线单元测试（无 FOR UPDATE 行锁，并发语义以 PG 测试为准）。
    return {"connect_args": {"check_same_thread": False}}


engine = create_engine(settings.DATABASE_URL, future=True, **_engine_kwargs(settings.DATABASE_URL))
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_session() -> Iterator[Session]:
    """FastAPI 依赖：请求结束时统一提交或回滚并关闭会话。"""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    # 模型模块必须先导入，MetaData 才能收集到表定义。
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
