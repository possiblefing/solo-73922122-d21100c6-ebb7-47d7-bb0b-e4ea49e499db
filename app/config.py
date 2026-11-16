"""运行期配置。

数据库连接串优先取环境变量 ``DATABASE_URL``，未设置时回退到本地开发用的
PostgreSQL 地址。Docker Compose 通过环境变量覆盖为容器内地址。
"""
from __future__ import annotations

import os


class Settings:
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg2://postgres:postgres@localhost:5432/stationhub",
    )
    # 每个来源允许导入的最大站点数（题目约束：每源至多 30 站）。
    MAX_STATIONS_PER_SOURCE: int = int(os.getenv("MAX_STATIONS_PER_SOURCE", "30"))
    # 数据库死锁/序列化失败时的服务端重试次数。
    MAX_RETRIES: int = 5


settings = Settings()
