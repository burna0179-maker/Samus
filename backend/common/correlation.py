"""Correlation / trace ID propagation via contextvars."""
from __future__ import annotations

import contextvars
import uuid
from typing import Any

_TRACE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("samus_trace_id", default=None)


def new_trace_id() -> str:
    return uuid.uuid4().hex


def set_trace_id(trace_id: str) -> None:
    _TRACE_ID.set(trace_id)


def get_trace_id() -> str | None:
    return _TRACE_ID.get()


def ensure_trace_id() -> str:
    tid = _TRACE_ID.get()
    if not tid:
        tid = new_trace_id()
        _TRACE_ID.set(tid)
    return tid


def headers() -> dict[str, str]:
    """HTTP headers carrying the current trace context."""
    tid = ensure_trace_id()
    return {"X-Correlation-Id": tid, "X-Trace-Id": tid}


def with_trace(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a log-record-ready dict that includes trace id."""
    out: dict[str, Any] = {"trace_id": ensure_trace_id()}
    if extra:
        out.update(extra)
    return out
