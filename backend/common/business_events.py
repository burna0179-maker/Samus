"""Unified business-event ledger — the revenue-journey spine (HOTL Tranche 1).

Every revenue-bearing workcell (intake, prospecting, outreach, voice, CRM,
finance) emits one small, taxonomy-validated record per business event into a
single append-only ledger. That gives the operator ONE chronological journey
per prospect (``read_events(prospect_id=...)`` / ``GET /admin/journey/{id}``)
instead of six per-workcell ledgers that never join.

WHY A SHARED LEDGER, NOT A DDB TABLE
------------------------------------
This is best-effort telemetry, not authoritative state — the same rationale as
:mod:`backend.common.conversion_funnel` and
:mod:`backend.common.control_tick_ledger`. It rides
:func:`backend.common.persistence.open_ledger` so the jsonl backend (default)
and the Firestore backend (Cloud Run) both work from the same call site.

GRACEFUL DEGRADATION (non-negotiable)
-------------------------------------
``emit_business_event`` NEVER raises to callers: an invalid event_type logs a
warning and returns ``{}``; a ledger append failure logs a warning and returns
the record dict anyway. ``read_events`` never raises (returns ``[]``). A
telemetry hiccup must never break the send / call / payment it instruments.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Final

from .correlation import ensure_trace_id
from .dates import iso_now
from .persistence import open_ledger

_LOG = logging.getLogger("samus.common.business_events")

# --- Taxonomy -----------------------------------------------------------
# Module-level constants so call sites reference names, not string literals.
LEAD_CREATED: Final[str] = "lead.created"
LEAD_ENRICHED: Final[str] = "lead.enriched"
EMAIL_SENT: Final[str] = "email.sent"
EMAIL_OPENED: Final[str] = "email.opened"
EMAIL_CLICKED: Final[str] = "email.clicked"
CALL_PLACED: Final[str] = "call.placed"
CALL_ANSWERED: Final[str] = "call.answered"
MEETING_BOOKED: Final[str] = "meeting.booked"
PROPOSAL_SENT: Final[str] = "proposal.sent"
CONTRACT_SENT: Final[str] = "contract.sent"
CONTRACT_SIGNED: Final[str] = "contract.signed"
INVOICE_SENT: Final[str] = "invoice.sent"
PAYMENT_RECEIVED: Final[str] = "payment.received"
CUSTOMER_RETAINED: Final[str] = "customer.retained"
CUSTOMER_CHURNED: Final[str] = "customer.churned"
DECISION_MADE: Final[str] = "decision.made"
EXPERIMENT_ASSIGNED: Final[str] = "experiment.assigned"
# Strategic Compression Engine — one structured business-law promoted from
# distilled operational exhaust (consolidator.distill -> guidance_laws.jsonl).
LAW_PROMOTED: Final[str] = "law.promoted"
# Inbound email from a known/existing client (email listed in
# clients/<slug>/campaign.yaml, resolved via backend.crm.client_directory).
# Distinct from an anonymous prospect reply — signals customer-service intent
# and should route to a client-correspondence path, not the lead-nurture path.
CLIENT_CORRESPONDENCE: Final[str] = "client.correspondence"

# Two-way calendar sync: emitted by backend.intake.calendar_poller when a new
# event appears on samushustleforge@'s Google Calendar (either the intake
# pipeline projected it from an .ics/booking email, or the operator added
# it directly via the Calendar UI). The intake poller distinguishes
# operator-created events by the absence of the extendedProperties.private.
# source="samus.intake" tag. Both fire the same event type — the metadata
# carries the origin so downstream reasoning can differentiate.
CALENDAR_EVENT_SCHEDULED: Final[str] = "calendar.event_scheduled"
# Emitted when a previously-known event's end_time has passed. Lets the
# business-event journey view include "meeting happened" without polling
# per-client calendars.
CALENDAR_EVENT_COMPLETED: Final[str] = "calendar.event_completed"

BUSINESS_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        LEAD_CREATED,
        LEAD_ENRICHED,
        EMAIL_SENT,
        EMAIL_OPENED,
        EMAIL_CLICKED,
        CALL_PLACED,
        CALL_ANSWERED,
        MEETING_BOOKED,
        PROPOSAL_SENT,
        CONTRACT_SENT,
        CONTRACT_SIGNED,
        INVOICE_SENT,
        PAYMENT_RECEIVED,
        CUSTOMER_RETAINED,
        CUSTOMER_CHURNED,
        DECISION_MADE,
        EXPERIMENT_ASSIGNED,
        LAW_PROMOTED,
        CLIENT_CORRESPONDENCE,
        CALENDAR_EVENT_SCHEDULED,
        CALENDAR_EVENT_COMPLETED,
    }
)

# --- Ledger plumbing ------------------------------------------------------
# Same path convention as conversion_funnel.py / control_tick_ledger.py:
# env override -> /opt/samus/data/telemetry default. Firestore collection is
# the canonical "samus_business_events".
_LEDGER_PATH_DEFAULT: Final[str] = "/opt/samus/data/telemetry/business_events.jsonl"
_FIRESTORE_COLLECTION: Final[str] = "samus_business_events"

# Bounded read window for read_events — the journey view is per-prospect and
# low-frequency, so a generous tail covers a long operating history without
# an unbounded read on a hot ledger.
_READ_TAIL_MAX: Final[int] = 100_000


def _ledger_path() -> str:
    """Resolve the business-event ledger path (env override -> default)."""
    return os.getenv("SAMUS_BUSINESS_EVENTS_PATH", _LEDGER_PATH_DEFAULT)


def _ledger():
    return open_ledger(
        jsonl_path=_ledger_path(),
        collection=_FIRESTORE_COLLECTION,
    )


def emit_business_event(
    event_type: str,
    *,
    workcell: str,
    prospect_id: str | None = None,
    opportunity_id: str | None = None,
    campaign_id: str | None = None,
    variant_arm_id: str | None = None,
    cost_usd: float | None = None,
    revenue_usd: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict:
    """Append one taxonomy-validated business event to the unified ledger.

    THIS SIGNATURE IS A CONTRACT — sibling branches compile against it.

    Returns the full record dict on success. An unknown ``event_type`` logs a
    warning and returns ``{}`` (never raises — a taxonomy typo upstream must
    not fail a send/call/payment). A ledger failure logs a warning and returns
    the record dict anyway so callers can still inspect what would have been
    written.
    """
    if event_type not in BUSINESS_EVENT_TYPES:
        _LOG.warning(
            "business_events: rejecting unknown event_type=%r (workcell=%s)",
            event_type,
            workcell,
        )
        return {}
    record: dict[str, Any] = {
        "ts": iso_now(),
        "event_id": uuid.uuid4().hex,
        "trace_id": ensure_trace_id(),
        "event_type": event_type,
        "workcell": workcell or "",
        "prospect_id": prospect_id or "",
        "opportunity_id": opportunity_id or "",
        "campaign_id": campaign_id or "",
        "variant_arm_id": variant_arm_id or "",
        "cost_usd": None if cost_usd is None else round(float(cost_usd), 4),
        "revenue_usd": (None if revenue_usd is None else round(float(revenue_usd), 2)),
        "metadata": dict(metadata) if metadata else {},
    }
    try:
        _ledger().append(record)
    except Exception as exc:  # noqa: BLE001 — telemetry must never break callers
        _LOG.warning(
            "business_events append failed type=%s workcell=%s: %s",
            event_type,
            workcell,
            exc,
        )
    return record


def read_events(
    prospect_id: str | None = None,
    opportunity_id: str | None = None,
    since: str | None = None,
    event_types: Any = None,
    limit: int = 1000,
) -> list[dict]:
    """Read + filter the business-event ledger, chronological (oldest first).

    ``since`` is an ISO-8601 Z-suffixed timestamp (as stamped by
    ``emit_business_event``); records with ``ts >= since`` pass. ``event_types``
    is any iterable of taxonomy strings. When more than ``limit`` records
    match, the MOST RECENT ``limit`` are returned (still oldest-first).

    Never raises: any read failure logs a warning and returns ``[]``.
    """
    try:
        safe_limit = max(1, min(int(limit), _READ_TAIL_MAX))
        wanted_types = frozenset(event_types) if event_types else None
        rows = _ledger().tail(limit=_READ_TAIL_MAX)
        out: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if prospect_id and row.get("prospect_id") != prospect_id:
                continue
            if opportunity_id and row.get("opportunity_id") != opportunity_id:
                continue
            if wanted_types is not None and row.get("event_type") not in wanted_types:
                continue
            if since and str(row.get("ts") or "") < since:
                continue
            out.append(row)
        # Records are appended in time order, so tail order IS chronological.
        # Keep the most recent `limit` matches, oldest-first.
        return out[-safe_limit:]
    except Exception as exc:  # noqa: BLE001 — telemetry read, never blocks
        _LOG.warning("business_events read failed: %s", exc)
        return []


__all__ = [
    "BUSINESS_EVENT_TYPES",
    "LEAD_CREATED",
    "LEAD_ENRICHED",
    "EMAIL_SENT",
    "EMAIL_OPENED",
    "EMAIL_CLICKED",
    "CALL_PLACED",
    "CALL_ANSWERED",
    "MEETING_BOOKED",
    "PROPOSAL_SENT",
    "CONTRACT_SENT",
    "INVOICE_SENT",
    "PAYMENT_RECEIVED",
    "CUSTOMER_RETAINED",
    "CUSTOMER_CHURNED",
    "DECISION_MADE",
    "EXPERIMENT_ASSIGNED",
    "LAW_PROMOTED",
    "CLIENT_CORRESPONDENCE",
    "CALENDAR_EVENT_SCHEDULED",
    "CALENDAR_EVENT_COMPLETED",
    "emit_business_event",
    "read_events",
]
