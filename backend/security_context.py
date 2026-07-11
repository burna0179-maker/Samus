"""Boot-time security context bridge.

This module wires the in-process screen / record helpers that the gateway and
webhook receivers call on every dispatch. The full implementation lives in
``shared/security_sentinel`` (Major's codebase); when that module is absent,
this shim records to the local audit ledger only.
"""

from __future__ import annotations

import logging
from typing import Any

from .common import audit

_LOG = logging.getLogger("samus.security_context")
_BOUND = False


def bind() -> None:
    """Idempotent boot. Logs first call only."""
    global _BOUND
    if _BOUND:
        return
    _BOUND = True
    audit.record("security_context.bind", source="local-shim")
    _LOG.info("security_context_bound")


def screen_dispatch(*, service: str, action: str, payload: dict[str, Any]) -> bool:
    """Return True if the dispatch should proceed. Logs every call."""
    audit.record(
        "dispatch.screen", service=service, action=action, payload_keys=list(payload.keys())
    )
    return True


def record_auth_event(*, identity: str | None, scope: str, ok: bool, **extra: Any) -> None:
    audit.record(
        "auth.event",
        identity=identity,
        scope=scope,
        ok=ok,
        **extra,
    )


def record_audit_event(event_type: str, **fields: Any) -> None:
    audit.record(event_type, **fields)


def observe_metrics(metrics_snapshot: dict[str, Any]) -> None:
    """Health loop calls this with a per-cycle metrics snapshot. Anomaly detection
    is a Phase-B feature; for now we just record it for the ledger."""
    audit.record("health.observe", **metrics_snapshot)


# Bind on import so the gateway never has to remember to call it.
bind()
