"""FastAPI 入口：建表、路由与网页界面。"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from .db import init_db
from .routers import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 等待 Compose 中 PostgreSQL 就绪后建表；表已存在则无操作。
    last_exc: Exception | None = None
    for attempt in range(10):
        try:
            init_db()
            break
        except Exception as exc:  # pragma: no cover - 容器启动时序
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
    else:
        raise last_exc  # type: ignore[misc]
    yield


app = FastAPI(
    title="站点目录归一系统 StationHub",
    description="跨机构站点同站/异站判定、传递分组与统一号版本管理",
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(router)


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


_INDEX = Path(__file__).with_name("web").joinpath("index.html")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> str:
    return _INDEX.read_text(encoding="utf-8")
