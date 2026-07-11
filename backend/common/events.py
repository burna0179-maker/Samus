"""Audit-event builder per doc §3.18.

``build_audit_event`` returns the canonical audit record dict that workcells
append to their per-service JsonlLedger. ``_deterministic_hash`` is the helper
used to fingerprint input/output payloads.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from . import correlation
from .dates import iso_now


def _deterministic_hash(payload: Any) -> str:
    """SHA-256 hex of canonical JSON (sorted keys, compact separators, default=str)."""
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_audit_event(
    service: str,
    task_id: str,
    action: str,
    input_payload: Any,
    output_payload: Any,
    status: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a canonical audit event dict."""
    return {
        "event_id": str(uuid.uuid4()),
        "ts": iso_now(),
        "trace_id": correlation.get_trace_id(),
        "service": service,
        "task_id": task_id,
        "action": action,
        "input_hash": _deterministic_hash(input_payload),
        "output_hash": _deterministic_hash(output_payload),
        "status": status,
        "metadata": metadata or {},
    }
