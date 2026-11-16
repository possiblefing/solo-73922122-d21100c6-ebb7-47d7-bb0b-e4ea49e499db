"""事务级死锁/序列化失败重试。"""
from __future__ import annotations

import time

from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from .config import settings


def run_with_retry(session: Session, fn, *args, **kwargs):
    """整事务重试：捕获死锁/序列化冲突 → 回滚 → 指数退避后重新执行。

    所有依据写入与组重算都在单个事务内，回滚后数据库回到操作前状态，
    因此重试不会产生重复依据或半成品组。
    """
    last_exc: Exception | None = None
    for attempt in range(settings.MAX_RETRIES):
        try:
            return fn(session, *args, **kwargs)
        except (OperationalError, ProgrammingError) as exc:
            text = str(getattr(exc, "orig", exc))
            if "deadlock detected" not in text and "serialization failure" not in text:
                raise
            session.rollback()
            last_exc = exc
            time.sleep(0.05 * (2**attempt))
    raise last_exc  # type: ignore[misc]
