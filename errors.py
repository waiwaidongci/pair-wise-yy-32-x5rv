"""共享异常类型，供批次服务与清场复核模块使用。"""
from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message); self.status, self.message = status, message
