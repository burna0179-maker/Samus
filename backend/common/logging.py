"""Structured JSON logging per doc §3.1.

``configure_logging`` is idempotent and installs a single stdout handler with
a JSON formatter that includes trace_id from the correlation ContextVar.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from . import correlation


class JsonFormatter(logging.Formatter):
    """Renders one JSON object per log line."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """UTC ISO-8601 with microseconds.

        The base ``logging.Formatter.formatTime`` routes ``datefmt`` through
        ``time.strftime``, whose C library does NOT support ``%f``: on Windows
        that raises ``ValueError: Invalid format string`` on EVERY emit, and on
        glibc it silently yields a literal ``.fZ`` (and uses localtime despite
        the ``Z`` suffix). ``datetime.strftime`` supports ``%f`` on every
        platform, so format the UTC timestamp here instead.
        """
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return dt.strftime(datefmt or "%Y-%m-%dT%H:%M:%S.%fZ")

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        trace_id = correlation.get_trace_id()
        if trace_id:
            payload["trace_id"] = trace_id
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        # Include any extra fields the caller passed via logger.X(extra={...}).
        for key, value in record.__dict__.items():
            if key in (
                "msg",
                "args",
                "exc_info",
                "exc_text",
                "stack_info",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
                "name",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "asctime",
            ):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = str(value)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Install JSON stdout handler. Safe to call multiple times."""
    root = logging.getLogger()
    if getattr(root, "_samus_json_configured", False):
        return
    root._samus_json_configured = True  # type: ignore[attr-defined]
    root.setLevel(level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]
