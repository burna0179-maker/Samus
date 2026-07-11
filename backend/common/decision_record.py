"""Decision records — the explainability spine (HOTL Tranche 4, framework Phase 4).

Every autonomous decision Samus makes (plan generation, replan, arbiter pick,
stake draft, promotion/demotion, enforcement application) can mint a
:class:`DecisionRecord` at the moment the choice is made. The record answers
the framework's four questions:

    why                    — the one-line rationale for the choice
    alternatives_considered — the options that were NOT taken (and why)
    data_used              — the evidence the decision leaned on
    expected_outcome       — what the actor predicts will happen

plus confidence, risk_level, ev_usd, and a timestamp.

WHERE IT LIVES
--------------
There is NO separate decision-record store. A DecisionRecord is carried on the
unified business-event stream as the ``metadata`` of a ``decision.made`` event
(:mod:`backend.common.business_events`). That keeps ONE reconstruction surface
for the whole journey — ``read_events(event_types=["decision.made"])`` is the
decision log, and every decision already shares the same prospect/opportunity
correlation keys as the events it acted on. :func:`record_decision` mints the
record, persists+emits it in one call, and returns it.

GRACEFUL DEGRADATION (non-negotiable)
-------------------------------------
``record_decision`` NEVER raises to callers: a telemetry hiccup must not break
the decision it instruments (mirrors ``emit_business_event`` /
``create_approval``). On any failure it logs a warning and returns the record
dict anyway so the caller can still inspect / log what would have been written.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from .dates import iso_now

_LOG = logging.getLogger("samus.common.decision_record")

# The metadata key under which the full record rides the decision.made event.
DECISION_RECORD_KEY = "decision_record"


@dataclass
class DecisionRecord:
    """One explainable autonomous decision.

    ``decision_id`` is a stable id an operator can drill into via
    ``GET /admin/decisions/{decision_id}``. ``actor`` names the subsystem that
    made the call ("planner", "arbiter", "stake_drafter", "enforcer", ...).
    """

    decision_id: str
    actor: str
    why: str
    alternatives_considered: list[Any] = field(default_factory=list)
    data_used: list[Any] = field(default_factory=list)
    expected_outcome: str = ""
    confidence: float = 0.0
    risk_level: str = "normal"
    ev_usd: float = 0.0
    ts: str = ""
    # Optional correlation — mirrored onto the carrying event so a decision
    # joins the same journey view as the events it acted on.
    prospect_id: str = ""
    opportunity_id: str = ""
    campaign_id: str = ""
    # Validation checks run BEFORE the decision was committed. Free-form list
    # of short slugs, e.g. ["schema_validate:proposal_v2", "budget_gate:passed",
    # "adversarial_verify:2of3"]. Empty list = no formal validation performed
    # (an unvalidated hunch). Additive, optional — omitting it is fine.
    validation_performed: list[str] = field(default_factory=list)
    # Structured memory-retrieval records — one dict per memory/precedent that
    # was retrieved AND used to shape this decision. Recommended keys per dict:
    #   {"source": "guidance_ledger|calibration|precedent|feedback|other",
    #    "id": "<stable id or slug>",
    #    "why": "one-line relevance rationale"}
    # Shape is loose (dict[str, Any]) so callers can extend without a schema
    # bump. Empty list = no memory recall instrumented for this decision.
    memories_retrieved: list[dict[str, Any]] = field(default_factory=list)
    # Free-form extra context (e.g. the plan diff for a replan). Kept small.
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def make_decision_record(
    actor: str,
    why: str,
    *,
    alternatives_considered: list[Any] | None = None,
    data_used: list[Any] | None = None,
    expected_outcome: str = "",
    confidence: float | None = None,
    risk_level: str = "normal",
    ev_usd: float | None = None,
    prospect_id: str | None = None,
    opportunity_id: str | None = None,
    campaign_id: str | None = None,
    validation_performed: list[str] | None = None,
    memories_retrieved: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
    decision_id: str | None = None,
) -> DecisionRecord:
    """Mint a :class:`DecisionRecord` (pure — no I/O). Never raises.

    ``validation_performed`` — optional list of validation-check slugs that
    were run BEFORE this decision was committed (self-checks that turn an
    unvalidated hunch into a rigorously-checked call). Defaults to ``[]``.

    ``memories_retrieved`` — optional list of retrieved-memory dicts, one per
    memory/precedent that was recalled and used. Recommended keys per dict:
    ``source``, ``id``, ``why``. Defaults to ``[]``.
    """
    return DecisionRecord(
        decision_id=decision_id or uuid.uuid4().hex,
        actor=str(actor or "unknown"),
        why=str(why or ""),
        alternatives_considered=list(alternatives_considered or []),
        data_used=list(data_used or []),
        expected_outcome=str(expected_outcome or ""),
        confidence=0.0 if confidence is None else round(float(confidence), 4),
        risk_level=(risk_level or "normal").strip().lower(),
        ev_usd=0.0 if ev_usd is None else round(float(ev_usd), 2),
        ts=iso_now(),
        prospect_id=str(prospect_id or ""),
        opportunity_id=str(opportunity_id or ""),
        campaign_id=str(campaign_id or ""),
        validation_performed=[str(v) for v in (validation_performed or [])],
        memories_retrieved=[dict(m) for m in (memories_retrieved or []) if isinstance(m, dict)],
        extra=dict(extra or {}),
    )


def record_decision(
    actor: str,
    why: str,
    *,
    workcell: str = "",
    alternatives_considered: list[Any] | None = None,
    data_used: list[Any] | None = None,
    expected_outcome: str = "",
    confidence: float | None = None,
    risk_level: str = "normal",
    ev_usd: float | None = None,
    cost_usd: float | None = None,
    prospect_id: str | None = None,
    opportunity_id: str | None = None,
    campaign_id: str | None = None,
    validation_performed: list[str] | None = None,
    memories_retrieved: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> DecisionRecord:
    """Mint + persist + emit a decision record in one call.

    Emits a ``decision.made`` business event whose ``metadata`` carries the
    full record under ``DECISION_RECORD_KEY`` (plus a flat ``decision_kind`` /
    ``actor`` for cheap filtering). Correlation ids (prospect/opportunity/
    campaign) are mirrored onto the event so the decision joins the journey.

    ``validation_performed`` — optional list of self-check slugs run before
    the decision was committed (e.g. ``["schema_validate:x", "budget_gate"]``).
    Defaults to ``[]`` (an unvalidated hunch).

    ``memories_retrieved`` — optional list of retrieved-memory dicts, one per
    memory/precedent used (recommended keys: ``source``, ``id``, ``why``).
    Defaults to ``[]``.

    Returns the minted :class:`DecisionRecord`. NEVER raises — a telemetry
    failure logs a warning and still returns the record.
    """
    record = make_decision_record(
        actor,
        why,
        alternatives_considered=alternatives_considered,
        data_used=data_used,
        expected_outcome=expected_outcome,
        confidence=confidence,
        risk_level=risk_level,
        ev_usd=ev_usd,
        prospect_id=prospect_id,
        opportunity_id=opportunity_id,
        campaign_id=campaign_id,
        validation_performed=validation_performed,
        memories_retrieved=memories_retrieved,
        extra=extra,
    )
    try:
        from .business_events import DECISION_MADE, emit_business_event

        # NOTE: the event's revenue_usd/cost_usd fields are reserved for
        # REALISED money — a decision's forecast ev_usd rides the embedded
        # record, never the event's money fields. Only cost_usd (an actual
        # spend, e.g. a metered LLM draft) is passed through when supplied.
        emit_business_event(
            DECISION_MADE,
            workcell=workcell or record.actor,
            prospect_id=record.prospect_id or None,
            opportunity_id=record.opportunity_id or None,
            campaign_id=record.campaign_id or None,
            cost_usd=cost_usd,
            metadata={
                "decision_kind": "decision_record",
                "actor": record.actor,
                DECISION_RECORD_KEY: record.to_dict(),
            },
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must never break callers
        _LOG.warning("record_decision emit failed actor=%s: %s", actor, exc)
    return record


def _extract_record(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return the embedded DecisionRecord dict from a decision.made event.

    Handles BOTH shapes on the stream: a full record minted here (carried
    under ``metadata[DECISION_RECORD_KEY]``) and the lighter approval-lifecycle
    / roll-up decision.made events that other subsystems emit (no embedded
    record — we synthesise a thin record from their metadata so the decision
    log is uniform). Returns None only for a non-dict event.
    """
    if not isinstance(event, dict):
        return None
    meta = event.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    embedded = meta.get(DECISION_RECORD_KEY)
    if isinstance(embedded, dict) and embedded.get("decision_id"):
        # Backfill correlation/ts from the carrying event if the record omitted
        # them (belt-and-suspenders — make_decision_record fills them already).
        rec = dict(embedded)
        rec.setdefault("ts", str(event.get("ts") or ""))
        rec.setdefault("prospect_id", str(event.get("prospect_id") or ""))
        rec.setdefault("opportunity_id", str(event.get("opportunity_id") or ""))
        # Backward-compat: older writers may not include the new fields.
        rec.setdefault("validation_performed", [])
        rec.setdefault("memories_retrieved", [])
        return rec
    # Thin synthesis for a bare decision.made event (e.g. approval lifecycle,
    # ROI nightly roll-up) so it still appears in the decision log with a why.
    kind = str(meta.get("decision_kind") or meta.get("decision") or "decision")
    actor = str(meta.get("actor") or event.get("workcell") or "unknown")
    return {
        "decision_id": str(event.get("event_id") or uuid.uuid4().hex),
        "actor": actor,
        "why": kind,
        "alternatives_considered": [],
        "data_used": [],
        "expected_outcome": "",
        "confidence": 0.0,
        "risk_level": str(meta.get("risk_level") or "normal"),
        "ev_usd": float(event.get("revenue_usd") or 0.0),
        "ts": str(event.get("ts") or ""),
        "prospect_id": str(event.get("prospect_id") or ""),
        "opportunity_id": str(event.get("opportunity_id") or ""),
        "campaign_id": str(event.get("campaign_id") or ""),
        "validation_performed": [],
        "memories_retrieved": [],
        "extra": {k: v for k, v in meta.items() if k != DECISION_RECORD_KEY},
    }


def list_decisions(
    *,
    actor: str | None = None,
    prospect_id: str | None = None,
    opportunity_id: str | None = None,
    since: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Read the decision log from the unified stream, newest first.

    Reconstructed from ``decision.made`` events via
    :func:`backend.common.business_events.read_events`. ``actor`` filters on
    the minted actor. Never raises (returns ``[]`` on failure).
    """
    try:
        from .business_events import read_events

        events = read_events(
            prospect_id=prospect_id,
            opportunity_id=opportunity_id,
            since=since,
            event_types=["decision.made"],
            limit=max(1, int(limit)) * 4,  # over-read; some may not synthesise
        )
        out: list[dict[str, Any]] = []
        for ev in reversed(events):  # newest first
            rec = _extract_record(ev)
            if rec is None:
                continue
            if actor and rec.get("actor") != actor:
                continue
            out.append(rec)
            if len(out) >= max(1, int(limit)):
                break
        return out
    except Exception as exc:  # noqa: BLE001 — operator read, never blocks
        _LOG.warning("list_decisions failed: %s", exc)
        return []


def get_decision(decision_id: str) -> dict[str, Any] | None:
    """One decision by id (drill-down). Scans the recent decision log.

    Never raises (returns None on failure / unknown id).
    """
    if not decision_id:
        return None
    try:
        for rec in list_decisions(limit=2000):
            if rec.get("decision_id") == decision_id:
                return rec
        return None
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("get_decision failed: %s", exc)
        return None


__all__ = [
    "DECISION_RECORD_KEY",
    "DecisionRecord",
    "make_decision_record",
    "record_decision",
    "list_decisions",
    "get_decision",
]
