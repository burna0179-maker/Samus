"""Dead-letter queue per doc §3.6.

JsonlLedger-backed per-service failure store + a single global
``replayed_archive.jsonl`` for status transitions. All DLQ data lives under
``SAMUS_DLQ_ROOT`` (default ``/opt/samus/data/dlq/``).
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from . import correlation
from .dates import iso_now
from .persistence import JsonlLedger

# A workcell/service name is a short lowercase identifier. Anything outside this
# charset (path separators, ``..``, drive letters, NUL) is rejected before the
# value is ever interpolated into a filesystem path — CWE-22 defense.
_VALID_SERVICE = re.compile(r"^[a-z_]{2,32}$")


def _dlq_root() -> Path:
    root = Path(os.getenv("SAMUS_DLQ_ROOT", "/opt/samus/data/dlq"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _validate_service(service: str) -> str:
    """Return ``service`` unchanged if it is a safe identifier, else raise.

    Raises ``ValueError`` for anything not matching ``^[a-z_]{2,32}$`` so a
    traversal payload (``../``, absolute paths, etc.) can never reach a path.
    """
    if not isinstance(service, str) or not _VALID_SERVICE.fullmatch(service):
        raise ValueError(f"invalid service name: {service!r}")
    return service


def _failures_path(service: str) -> Path:
    """Resolve the per-service failures ledger path, jailed under the DLQ root.

    Validates ``service`` (CWE-22) and, as defense-in-depth, asserts the
    resolved path is contained under the resolved DLQ root.
    """
    _validate_service(service)
    root = _dlq_root().resolve()
    path = (root / f"{service}_failures.jsonl").resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"resolved path escapes DLQ root: {service!r}")
    return path


def _archive_path() -> Path:
    return _dlq_root() / "replayed_archive.jsonl"


def enqueue_failure(
    service: str,
    *,
    task_id: str,
    target: str,
    payload: dict[str, Any],
    error: str,
    attempt: int = 1,
) -> str:
    """Append a failure record. Returns the ``event_id``."""
    event_id = str(uuid.uuid4())
    JsonlLedger(_failures_path(service)).append(
        {
            "event_id": event_id,
            "ts": iso_now(),
            "trace_id": correlation.get_trace_id(),
            "service": service,
            "task_id": task_id,
            "target": target,
            "payload": payload,
            "error": error,
            "attempt": attempt,
            "status": "pending_retry",
        }
    )
    return event_id


def read_pending(service: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return the last ``limit`` failure records for a service."""
    return JsonlLedger(_failures_path(service)).tail(limit=limit)


def mark_replayed(service: str, event_id: str, *, replay_status: str) -> None:
    """Append a status transition (``replayed`` | ``skipped`` | ``replay_failed``)."""
    archive = _archive_path()
    archive.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {
            "event_id": event_id,
            "ts": iso_now(),
            "trace_id": correlation.get_trace_id(),
            "service": service,
            "status": replay_status,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    with archive.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def read_archive(limit: int = 100) -> list[dict[str, Any]]:
    archive = _archive_path()
    if not archive.exists():
        return []
    return JsonlLedger(archive).tail(limit=limit)
