"""Campaign audit ledger — every node execution is recorded, tamper-evident.

Composes the canonical hash-chained :class:`backend.common.audit_ledger.
AuditLedger` (append-only JSONL, HMAC-chained per canonical §6) rather than
inventing a parallel sink. Each node execution emits one ``campaign.node``
record whose ``payload`` carries exactly the fields deliverable §7 requires:

    event_id, trace_id, campaign_id, client_id, node_id, node_type,
    target_workcell, capability, input_hash, output_hash, status, severity,
    approval_state, timestamp, duration_ms, error_summary, artifact_refs,
    kpi_refs

No raw secrets, no unnecessary PII: request/response payloads are reduced to
SHA-256 hashes (:func:`hash_payload`) and only artifact *references* are kept.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.common.audit_ledger import AuditLedger
from backend.common.correlation import get_trace_id
from backend.common.state_paths import state_path

CAMPAIGN_NODE_EVENT = "campaign.node"
CAMPAIGN_LIFECYCLE_EVENT = "campaign.lifecycle"

_LEDGER_PATH_ENV = "SAMUS_CAMPAIGN_LEDGER_PATH"


def hash_payload(value: Any) -> str:
    """Deterministic SHA-256 over a JSON-canonicalized value (secret-safe)."""
    material = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _default_ledger_path() -> Path:
    override = os.getenv(_LEDGER_PATH_ENV, "").strip()
    if override:
        return Path(override)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return state_path("campaigns", "audit", f"ledger-{day}.jsonl")


class CampaignAuditLedger:
    """Tamper-evident append-only ledger for campaign node executions."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else _default_ledger_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._ledger = AuditLedger(self._path)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def emit_node_event(
        self,
        *,
        campaign_id: str,
        client_id: str,
        node_id: str,
        node_type: str,
        target_workcell: str,
        capability: str,
        status: str,
        severity: str,
        approval_state: str,
        input_hash: str = "",
        output_hash: str = "",
        duration_ms: float = 0.0,
        error_summary: str = "",
        artifact_refs: list[str] | None = None,
        kpi_refs: list[str] | None = None,
        trace_id: str | None = None,
    ) -> str:
        """Append one node-execution record; returns its ``event_id``."""
        event_id = uuid.uuid4().hex
        payload = {
            "event_id": event_id,
            "trace_id": trace_id or get_trace_id() or "",
            "campaign_id": campaign_id,
            "client_id": client_id,
            "node_id": node_id,
            "node_type": node_type,
            "target_workcell": target_workcell,
            "capability": capability,
            "input_hash": input_hash,
            "output_hash": output_hash,
            "status": status,
            "severity": severity,
            "approval_state": approval_state,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_ms": round(float(duration_ms), 3),
            "error_summary": error_summary[:500],
            "artifact_refs": list(artifact_refs or []),
            "kpi_refs": list(kpi_refs or []),
        }
        with self._lock:
            self._ledger.record(CAMPAIGN_NODE_EVENT, payload)
        return event_id

    def emit_lifecycle_event(
        self,
        *,
        campaign_id: str,
        client_id: str,
        from_state: str,
        to_state: str,
        reason: str = "",
    ) -> str:
        """Record a campaign-level state transition."""
        event_id = uuid.uuid4().hex
        payload = {
            "event_id": event_id,
            "trace_id": get_trace_id() or "",
            "campaign_id": campaign_id,
            "client_id": client_id,
            "from_state": from_state,
            "to_state": to_state,
            "reason": reason[:300],
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        with self._lock:
            self._ledger.record(CAMPAIGN_LIFECYCLE_EVENT, payload)
        return event_id

    def events_for(self, campaign_id: str, *, limit: int = 5000) -> list[dict[str, Any]]:
        """Return this campaign's audit payloads, oldest first.

        Reads the ledger tail (bounded by ``limit``) and filters by
        ``campaign_id``. The ledger is HMAC-chained; callers wanting integrity
        assurance call :meth:`verify`.
        """
        with self._lock:
            records = self._ledger.tail(max(1, int(limit)))
        out: list[dict[str, Any]] = []
        for rec in records:
            payload = rec.get("payload") or {}
            if payload.get("campaign_id") == campaign_id:
                out.append({**payload, "type": rec.get("type"), "seq": rec.get("seq")})
        return out

    def verify(self) -> bool:
        """True iff the underlying hash chain is intact (tamper-evidence)."""
        return self._ledger.verify().ok


_default: CampaignAuditLedger | None = None
_default_lock = threading.Lock()


def get_campaign_ledger() -> CampaignAuditLedger:
    """Process-lazy singleton ledger (mirrors get_default_ledger())."""
    global _default
    if _default is not None:
        return _default
    with _default_lock:
        if _default is None:
            _default = CampaignAuditLedger()
        return _default


def reset_campaign_ledger() -> None:
    """Test helper — drop the cached singleton."""
    global _default
    with _default_lock:
        _default = None
