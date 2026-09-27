"""共享工具：时间、JSON、API 错误与身份校验。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def after_now(value: str | None = None) -> bool:
    if not value:
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) > datetime.now(timezone.utc)
    except ValueError:
        return False


def hours_from_now(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds").replace("+00:00", "Z")


def j(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message); self.status, self.message = status, message


def require_actor(actor: str | None, role: str | None, allowed: set[str]) -> str:
    if not actor: raise ApiError(401, "缺少身份")
    if role not in allowed: raise ApiError(403, "角色无权执行此操作")
    return actor
