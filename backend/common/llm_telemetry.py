"""Cognitive LLM call-trace -- per-reasoning-call routing + budget observability.

Every budgeted LLM call (``llm_client.anthropic_messages``) is the atom of
Samus's reasoning: cognition, planning, and drafting all bottom out here.
Before this module the ONLY signal of which backend a call took -- paid OpenAI
vs free local LM Studio -- was a single import-time log line, and the ONLY
signal a call was throttled was a buried per-deny log. An observer therefore
could not reconstruct, from durable telemetry, the questions the Cognitive
Observer must answer about every reasoning act:

    * was this reasoning paid (OpenAI) or free (local)?
    * was it throttled, and if so by WHICH budget control?
    * what did it cost and how long did it take?

This module closes that blind spot additively, without touching routing:

    * a dedicated append-only ledger (``telemetry/llm_calls.jsonl``) with one
      reconstructable record per call -- backend, decision (routed|denied),
      control (which budget gate denied), model, est/used/quota tokens, actual
      cost, outcome, latency;
    * two Prometheus counters (routing-by-backend, denials-by-control) so a
      dashboard can plot "how much reasoning is billing paid vs running free"
      and alert when a workcell's reasoning is going dark on budget throttle;
    * one concise structured log line per call so the backend choice is visible
      in ``docker logs`` immediately, not just at process import.

It is DEDICATED -- a separate ledger from the business-event funnel
(:mod:`backend.common.business_events`) -- so high-frequency per-call reasoning
traces never swamp the low-frequency revenue-journey view.

GRACEFUL DEGRADATION (non-negotiable)
-------------------------------------
``record_llm_call`` NEVER raises to callers and NEVER alters routing -- it
mirrors ``emit_business_event`` / ``record_decision``. A telemetry hiccup must
not break the reasoning call it instruments.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Final

from .correlation import ensure_trace_id
from .dates import iso_now
from .persistence import open_ledger

_LOG = logging.getLogger("samus.common.llm_telemetry")

# --- Decision taxonomy --------------------------------------------------
# ROUTED: every budget gate cleared and the call was dispatched to a backend.
# DENIED: a budget control blocked the call pre-flight (no HTTP call made).
ROUTED: Final[str] = "routed"
DENIED: Final[str] = "denied"

# --- Control taxonomy (which gate denied a call) ------------------------
# Kept low-cardinality so the ``control`` counter label stays a small, stable
# set an operator can alert on.
CONTROL_GLOBAL_CAP: Final[str] = "global_cap"        # Control A: global $-cap
CONTROL_WORKCELL_QUOTA: Final[str] = "workcell_quota"  # per-workcell token quota
CONTROL_CIRCUIT: Final[str] = "circuit"              # Control C: circuit breaker
CONTROL_FROZEN: Final[str] = "frozen"                # nonessential freeze
CONTROL_BROKER: Final[str] = "broker"                # meta-governance broker
CONTROL_MODEL_FLOOR: Final[str] = "model_floor"      # Control B: expensive model

# --- Backend taxonomy ---------------------------------------------------
BACKEND_OPENAI: Final[str] = "openai"  # paid
BACKEND_LOCAL: Final[str] = "local"    # free LM Studio

# --- Ledger plumbing ----------------------------------------------------
# Same convention as business_events / control_tick_ledger: env override ->
# /opt/samus/data/telemetry default. Firestore collection is canonical on
# Cloud Run.
_LEDGER_PATH_DEFAULT: Final[str] = "/opt/samus/data/telemetry/llm_calls.jsonl"
_FIRESTORE_COLLECTION: Final[str] = "samus_llm_calls"
_READ_TAIL_MAX: Final[int] = 100_000


def _ledger_path() -> str:
    """Resolve the reasoning-call ledger path (env override -> default)."""
    return os.getenv("SAMUS_LLM_TELEMETRY_PATH", _LEDGER_PATH_DEFAULT)


def _ledger():
    return open_ledger(
        jsonl_path=_ledger_path(), collection=_FIRESTORE_COLLECTION,
    )


def classify_deny_control(reason: str) -> str:
    """Map a ``QuotaDecision.reason`` string to a low-cardinality control label.

    The per-workcell gate surfaces plain quota exhaustion, an open circuit
    breaker, a nonessential freeze, and (translated) a broker denial all as a
    single denied ``QuotaDecision`` with different ``reason`` prefixes. This
    normalises them so the ``denials_total`` counter attributes each denial to
    the control that actually fired. Unknown reasons fall back to the plain
    per-workcell quota (the common case).
    """
    r = (reason or "").strip().lower()
    if r.startswith("broker") or "broker:" in r:
        return CONTROL_BROKER
    if r.startswith("circuit") or "circuit_open" in r:
        return CONTROL_CIRCUIT
    if "frozen" in r or "nonessential" in r:
        return CONTROL_FROZEN
    return CONTROL_WORKCELL_QUOTA


def _inc_counters(
    *, decision: str, workcell: str, backend: str, control: str,
) -> None:
    """Best-effort Prometheus counter increment. Never raises."""
    try:
        from . import metrics as _metrics

        if decision == ROUTED:
            _metrics.SAMUS_LLM_ROUTING_TOTAL.labels(
                workcell=workcell or "", backend=backend or "",
            ).inc()
        elif decision == DENIED:
            _metrics.SAMUS_LLM_DENIALS_TOTAL.labels(
                workcell=workcell or "", control=control or "",
            ).inc()
    except Exception as exc:  # noqa: BLE001 -- telemetry must never break callers
        _LOG.debug("llm_telemetry counter publish skipped: %s", exc)


def record_llm_call(
    *,
    workcell: str,
    backend: str,
    decision: str,
    model: str = "",
    control: str = "",
    reason: str = "",
    est_tokens: int = 0,
    used_tokens: int = 0,
    quota_tokens: int = 0,
    actual_cost_usd: float = 0.0,
    outcome: str = "",
    latency_ms: float = 0.0,
) -> dict[str, Any]:
    """Record one reasoning-call trace. Fail-soft: NEVER raises.

    ``decision`` is :data:`ROUTED` (gates cleared, dispatched to a backend) or
    :data:`DENIED` (a budget control blocked it pre-flight). ``backend`` is
    :data:`BACKEND_OPENAI` (paid) or :data:`BACKEND_LOCAL` (free). ``control``
    is populated for denials with the gate that fired.

    Writes the record to the dedicated ledger, increments the routing/denial
    counters, and logs one concise line (ROUTED at INFO -- the newly-visible
    backend/cost signal; DENIED at DEBUG -- the ledger + counter carry it, and
    ``llm_client`` already logs the human-readable deny). Returns the record
    dict even when persistence fails, so a caller can still inspect it.
    """
    record: dict[str, Any] = {
        "ts": iso_now(),
        "call_id": uuid.uuid4().hex,
        "trace_id": ensure_trace_id(),
        "workcell": workcell or "",
        "backend": backend or "",
        "decision": decision or "",
        "control": control or "",
        "reason": (reason or "")[:200],
        "model": model or "",
        "est_tokens": int(est_tokens or 0),
        "used_tokens": int(used_tokens or 0),
        "quota_tokens": int(quota_tokens or 0),
        "actual_cost_usd": round(float(actual_cost_usd or 0.0), 6),
        "outcome": outcome or "",
        "latency_ms": round(float(latency_ms or 0.0), 1),
    }
    try:
        _ledger().append(record)
    except Exception as exc:  # noqa: BLE001 -- telemetry must never break callers
        _LOG.debug("llm_telemetry append failed workcell=%s: %s", workcell, exc)

    _inc_counters(
        decision=decision, workcell=workcell, backend=backend, control=control,
    )

    try:
        if decision == ROUTED:
            _LOG.info(
                "llm_call workcell=%s backend=%s model=%s outcome=%s "
                "cost=$%.6f used=%s/%s lat=%.0fms",
                workcell, backend, model, outcome or "?",
                record["actual_cost_usd"], used_tokens, quota_tokens, latency_ms,
            )
        else:
            _LOG.debug(
                "llm_call DENIED workcell=%s backend=%s control=%s reason=%s "
                "used=%s/%s",
                workcell, backend, control, record["reason"],
                used_tokens, quota_tokens,
            )
    except Exception:  # noqa: BLE001 -- a logging fault must not break callers
        pass
    return record


def read_calls(
    *,
    workcell: str | None = None,
    decision: str | None = None,
    backend: str | None = None,
    since: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Read + filter the reasoning-call ledger, oldest-first. Never raises.

    The Cognitive Observer's reconstruction surface: ``read_calls(
    workcell="cognition", decision="denied")`` reconstructs every throttled
    reasoning cycle; ``read_calls(workcell="cognition", backend="openai")``
    surfaces every paid reasoning call. Returns ``[]`` on any failure.
    """
    try:
        safe_limit = max(1, min(int(limit), _READ_TAIL_MAX))
        rows = _ledger().tail(limit=_READ_TAIL_MAX)
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if workcell and row.get("workcell") != workcell:
                continue
            if decision and row.get("decision") != decision:
                continue
            if backend and row.get("backend") != backend:
                continue
            if since and str(row.get("ts") or "") < since:
                continue
            out.append(row)
        return out[-safe_limit:]
    except Exception as exc:  # noqa: BLE001 -- telemetry read, never blocks
        _LOG.debug("llm_telemetry read failed: %s", exc)
        return []


__all__ = [
    "ROUTED",
    "DENIED",
    "CONTROL_GLOBAL_CAP",
    "CONTROL_WORKCELL_QUOTA",
    "CONTROL_CIRCUIT",
    "CONTROL_FROZEN",
    "CONTROL_BROKER",
    "CONTROL_MODEL_FLOOR",
    "BACKEND_OPENAI",
    "BACKEND_LOCAL",
    "classify_deny_control",
    "record_llm_call",
    "read_calls",
]
