"""請求關聯 ID（日誌與追蹤）。"""
from __future__ import annotations

import contextvars
import uuid

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")


def set_request_id(rid: str) -> None:
    _request_id.set(rid or "")


def get_request_id() -> str:
    return _request_id.get() or ""


def new_request_id() -> str:
    rid = uuid.uuid4().hex[:16]
    set_request_id(rid)
    return rid
