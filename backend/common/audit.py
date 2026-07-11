"""Structured JSON audit logger + canonical-ledger sink.

Two-channel audit per the Samus arch doc §7:

  * one JSON line on the ``samus.audit`` Python logger (for log aggregator
    pickup);
  * one canonical HMAC-chained entry appended to today's
    :class:`backend.common.audit_ledger.AuditLedger` (tamper-evident
    forensic trail).

Existed in the arch doc as ``backend/common/audit.py`` but had no
corresponding file in the as-shipped tree — ``backend/security_context.py``
imported it and crashed on first use. This module closes that gap without
modifying the rebuilt domain-module audit shape
(:func:`backend.common.events.build_audit_event`) which workcells still
use for per-task in/out event records.

Public surface:
  * :func:`record` — fire-and-forget structured audit; never raises out
    of the shell into the caller's hot path. Errors land on the
    ``samus.audit.sink_error`` logger so they can't go silently missing
    AND a corrupt ledger cannot stall dispatch.
  * :func:`tail` — read recent canonical ledger entries (returns the
    canonical {seq, ts, type, prev_hash, payload, hmac} shape; useful for
    /health enrichment).
  * :func:`verify` — full chain verify; returns the
    :class:`backend.common.audit_ledger.LedgerState` dataclass.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from . import correlation
from .audit_ledger import LedgerState, get_default_ledger

_LOG = logging.getLogger("samus.audit")
_SINK_ERR = logging.getLogger("samus.audit.sink_error")


def record(event_type: str, **fields: Any) -> None:
    """Append one structured audit event to log + canonical ledger.

    Field convention: ``event_type`` is the canonical ``type`` (e.g.
    ``"dispatch.queued"``, ``"auth.event"``). Extra kwargs become the
    ``payload``. ``trace_id`` is injected from the correlation
    contextvar.
    """
    trace_id = correlation.get_trace_id()
    payload: Dict[str, Any] = dict(fields)
    payload.setdefault("trace_id", trace_id)

    # Channel 1: log line — always fires, even if the ledger sink errors
    # below. Use json.dumps with default=str so non-JSON values (datetimes,
    # paths, exceptions) don't fail the log call.
    try:
        line = json.dumps(
            {"type": event_type, **payload},
            default=str,
            sort_keys=True,
        )
        _LOG.info(line)
    except Exception as exc:  # never let logging crash the caller
        _SINK_ERR.exception("audit log serialise failed: %s", exc)

    # Channel 2: canonical hash-chained ledger. Wrap in try/except so a
    # disk-full / permission-denied / shutdown race never stalls the
    # gateway — the log line above is still the operator's belt.
    try:
        ledger = get_default_ledger()
        ledger.record(event_type, payload)
    except Exception as exc:
        _SINK_ERR.exception("audit ledger append failed: %s", exc)


def tail(n: int = 20) -> list[dict]:
    """Recent canonical entries from today's ledger; empty list on error."""
    try:
        return get_default_ledger().tail(n)
    except Exception as exc:
        _SINK_ERR.exception("audit ledger tail failed: %s", exc)
        return []


def verify() -> Optional[LedgerState]:
    """Full chain verify; None on unexpected error."""
    try:
        return get_default_ledger().verify()
    except Exception as exc:
        _SINK_ERR.exception("audit ledger verify failed: %s", exc)
        return None
