"""pytest 夹具。

优先连接真实 PostgreSQL（环境变量 TEST_DATABASE_URL，默认本地便携实例）；
不可用时回退 SQLite 跑纯逻辑测试，并自动跳过依赖行锁的并发用例。
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db as db_module
from app.db import Base


def _make_engine():
    url = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql+psycopg2://postgres@127.0.0.1:5433/stationhub",
    )
    try:
        eng = create_engine(url, future=True)
        with eng.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        return eng, "postgresql"
    except Exception:
        eng = create_engine(
            "sqlite+pysqlite:///:memory:",
            future=True,
            connect_args={"check_same_thread": False},
        )
        return eng, "sqlite"


engine, dialect = _make_engine()
TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)

# 让应用模块（含 TestClient 的 startup 建表）统一使用测试引擎。
db_module.engine = engine
db_module.SessionLocal = TestingSessionLocal


@pytest.fixture(scope="session")
def dialect_name():
    return dialect


@pytest.fixture()
def session():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    s = TestingSessionLocal()
    try:
        yield s
        s.rollback()
        s.close()
    finally:
        Base.metadata.drop_all(bind=engine)
