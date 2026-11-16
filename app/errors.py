"""业务异常，由 HTTP 层统一翻译为状态码。"""
from __future__ import annotations


class ServiceError(Exception):
    def __init__(self, status_code: int, message: str, details: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.details = details or {}


class NotFoundError(ServiceError):
    def __init__(self, message: str):
        super().__init__(404, message)


class ConflictError(ServiceError):
    def __init__(self, message: str, conflicts: list[dict] | None = None):
        super().__init__(409, message, {"conflicts": conflicts or []})


class ValidationError(ServiceError):
    def __init__(self, message: str):
        super().__init__(400, message)
