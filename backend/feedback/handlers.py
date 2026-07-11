"""Pure-Python handlers for SES feedback events (doc §9).

Each ``record_*`` function takes a parsed SES payload and applies the side
effects: write to the suppression table (bounce/complaint) and always append a
feedback-events row. All DDB calls are wrapped in ``try/except`` because the
local/test environment frequently has no AWS credentials — the FeedbackResult
is still populated so callers can audit the intent.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

from backend.common import aws, persistence
from backend.common.dates import iso_now
from backend.common.settings import get_settings

from .models import (
    FeedbackResult,
    SesBouncePayload,
    SesComplaintPayload,
    SesDeliveryPayload,
    SesNotification,
)

_LOG = logging.getLogger("samus.feedback.handlers")

_AUDIT_PATH_DEFAULT = "/opt/samus/data/feedback/feedback_audit.jsonl"


def _suppression_table() -> Any:
    settings = get_settings()
    return aws.table(settings.ddb_suppression_table, settings.aws_region)


def _feedback_events_table() -> Any:
    settings = get_settings()
    return aws.table(settings.ddb_feedback_table, settings.aws_region)


def _audit_ledger() -> persistence.JsonlLedger:
    path = os.getenv("SAMUS_FEEDBACK_AUDIT_PATH", _AUDIT_PATH_DEFAULT)
    return persistence.JsonlLedger(path)


def parse_sns_message(raw: str) -> SesNotification:
    """Parse the SNS-wrapped JSON-string Message field into a SesNotification."""
    decoded = json.loads(raw)
    return SesNotification.model_validate(decoded)


def _safe_put(table: Any, item: dict[str, Any], *, context: str) -> bool:
    """Wrap put_item so AWS misconfig in local/test doesn't poison the result."""
    try:
        table.put_item(Item=item)
        return True
    except Exception as exc:  # pragma: no cover - exercised via mocked failure
        _LOG.warning("feedback %s put_item failed: %s", context, exc)
        return False


def _event_id() -> str:
    return f"fbk_{uuid.uuid4().hex[:16]}"


def _record_feedback_event(
    *,
    notification_type: str,
    task_id: str,
    event_id: str,
    recipients: list[str],
    extra: dict[str, Any] | None = None,
) -> None:
    item: dict[str, Any] = {
        "event_id": event_id,
        "task_id": task_id,
        "notification_type": notification_type,
        "recipients": recipients,
        "ts": iso_now(),
    }
    if extra:
        item.update(extra)
    try:
        tbl = _feedback_events_table()
    except Exception as exc:  # pragma: no cover - boto bootstrap failure
        _LOG.warning("feedback events table lookup failed: %s", exc)
        return
    _safe_put(tbl, item, context="feedback_events")


# --------------------------------------------------------------------------
# Cash-engine re-engagement bridge
# --------------------------------------------------------------------------
# The cash engine parks a deal "dormant" after the initial outreach and waits
# for a prospect signal. These are the signals: positive engagement (a reply,
# an open, a web visit, or a manual operator log) wakes the deal and drafts a
# fresh voicemail; a negative deliverability event (bounce / complaint /
# unsubscribe) halts it. The feedback workcell is where those signals land, so
# this is where they fan into the engine.

_POSITIVE_EVENTS = frozenset({"reply", "replied", "open", "opened", "visit", "web_visit", "manual"})
_NEGATIVE_EVENTS = frozenset({"bounce", "complaint", "unsubscribe", "spam"})
_HOTLEAD_SIGNALS = frozenset({"reply", "replied", "open", "opened"})


def fire_cash_engine_signal(
    *,
    event: str,
    opportunity_id: str = "",
    prospect_id: str = "",
    crm: Any = None,
) -> dict[str, Any]:
    """Route a prospect-engagement signal into the cash engine.

    Resolves ``opportunity_id`` from ``prospect_id`` when only the latter is
    known. Positive events -> ``worker.reengage`` (drafts a voicemail +
    operator task, but only if the deal is dormant); negative events ->
    ``worker.halt``. Gated on ``cash_engine_enabled`` and fully best-effort:
    any failure returns a structured reason and never raises, so feedback
    ingestion is never broken by an engine fault.
    """
    ev = (event or "").strip().lower()
    settings = get_settings()
    if not getattr(settings, "cash_engine_enabled", False):
        return {"ok": False, "reason": "cash_engine_disabled"}

    if crm is None:
        from backend.crm import service as crm  # local import: avoid cycle

    oid = (opportunity_id or "").strip()
    if not oid and prospect_id:
        try:
            opp = crm.get_opportunity_for_prospect(prospect_id)
            oid = opp.opportunity_id if opp is not None else ""
        except Exception as exc:  # noqa: BLE001 — resolution is best-effort
            _LOG.warning("cash_engine signal: opportunity resolve failed: %s", exc)
            return {"ok": False, "reason": "resolve_failed"}
    if not oid:
        return {"ok": False, "reason": "no_opportunity"}

    if ev in _HOTLEAD_SIGNALS:
        try:
            from backend.heat.service import _log_hotlead_event

            sig = "replied" if ev in ("reply", "replied") else "opened"
            _log_hotlead_event(
                {"prospect_id": prospect_id, "email": "", "company": ""},
                sig,
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("hotlead journal from cash-engine signal failed: %s", exc)

    try:
        from backend.cash_engine import worker as cash_worker

        if ev in _NEGATIVE_EVENTS:
            return {"event": ev, **cash_worker.halt(opportunity_id=oid, reason=ev)}
        if ev in _POSITIVE_EVENTS:
            return {
                "event": ev,
                **cash_worker.reengage(
                    opportunity_id=oid,
                    event=ev,
                    crm=crm,
                ),
            }
        return {"ok": False, "reason": f"unknown_event:{ev}"}
    except Exception as exc:  # noqa: BLE001 — never break feedback ingestion
        _LOG.warning("cash_engine signal failed opp=%s: %s", oid, exc)
        return {"ok": False, "reason": f"error:{exc}"}


def _halt_cash_engine_for_emails(emails: list[str], event: str) -> None:
    """Resolve each bounced/complained address via the recipient index and
    halt its cash-engine deal.

    This is the negative half of the re-engagement loop: SES only gives us the
    email, so the index (written when the engine composed outreach to that
    address) maps it back to the deal. Entirely best-effort — an unknown
    address, an unconfigured index, or any fault is a clean no-op and never
    affects suppression/feedback ingestion.
    """
    for email in emails:
        try:
            from backend.common.recipient_index import lookup_recipient

            rec = lookup_recipient(email)
            if not rec:
                continue
            fire_cash_engine_signal(
                event=event,
                opportunity_id=rec.get("opportunity_id", ""),
                prospect_id=rec.get("prospect_id", ""),
            )
        except Exception as exc:  # noqa: BLE001 — bounce-halt never breaks feedback
            _LOG.warning("cash_engine bounce-halt failed for %s: %s", email, exc)


def record_bounce(payload: SesBouncePayload, *, task_id: str) -> FeedbackResult:
    """Write a suppression row per bounced recipient, plus a feedback event."""
    event_id = _event_id()
    suppressed: list[str] = []
    try:
        tbl = _suppression_table()
    except Exception as exc:  # pragma: no cover - boto bootstrap failure
        _LOG.warning("suppression table lookup failed: %s", exc)
        tbl = None

    ts = iso_now()
    subtype = payload.bounceSubType or "unknown"
    for recipient in payload.bouncedRecipients:
        addr = recipient.emailAddress
        suppressed.append(addr)
        if tbl is None:
            continue
        _safe_put(
            tbl,
            {
                "email": addr,
                "reason": "bounce",
                "subtype": subtype,
                "ts": ts,
                "task_id": task_id,
            },
            context="suppression_bounce",
        )

    _record_feedback_event(
        notification_type="Bounce",
        task_id=task_id,
        event_id=event_id,
        recipients=suppressed,
        extra={"bounce_type": payload.bounceType, "bounce_subtype": subtype},
    )

    # A bounced address is dead — halt any cash-engine deal pointed at it.
    _halt_cash_engine_for_emails(suppressed, "bounce")

    return FeedbackResult(
        notification_type="Bounce",
        recipient_count=len(suppressed),
        suppressed=suppressed,
        event_id=event_id,
    )


def record_complaint(payload: SesComplaintPayload, *, task_id: str) -> FeedbackResult:
    """Write a suppression row per complainant, plus a feedback event."""
    event_id = _event_id()
    suppressed: list[str] = []
    try:
        tbl = _suppression_table()
    except Exception as exc:  # pragma: no cover - boto bootstrap failure
        _LOG.warning("suppression table lookup failed: %s", exc)
        tbl = None

    ts = iso_now()
    feedback_type = payload.complaintFeedbackType or "unknown"
    for recipient in payload.complainedRecipients:
        addr = recipient.emailAddress
        suppressed.append(addr)
        if tbl is None:
            continue
        _safe_put(
            tbl,
            {
                "email": addr,
                "reason": "complaint",
                "subtype": feedback_type,
                "ts": ts,
                "task_id": task_id,
            },
            context="suppression_complaint",
        )

    _record_feedback_event(
        notification_type="Complaint",
        task_id=task_id,
        event_id=event_id,
        recipients=suppressed,
        extra={"feedback_type": feedback_type},
    )

    # A complaint is hostile — halt any cash-engine deal pointed at it.
    _halt_cash_engine_for_emails(suppressed, "complaint")

    return FeedbackResult(
        notification_type="Complaint",
        recipient_count=len(suppressed),
        suppressed=suppressed,
        event_id=event_id,
    )


def record_delivery(payload: SesDeliveryPayload, *, task_id: str) -> FeedbackResult:
    """Log a delivery — no suppression write, only a feedback event."""
    event_id = _event_id()
    recipients = list(payload.recipients or [])

    _record_feedback_event(
        notification_type="Delivery",
        task_id=task_id,
        event_id=event_id,
        recipients=recipients,
        extra={"processing_time_ms": payload.processingTimeMillis},
    )

    return FeedbackResult(
        notification_type="Delivery",
        recipient_count=len(recipients),
        suppressed=[],
        event_id=event_id,
    )


__all__ = [
    "parse_sns_message",
    "fire_cash_engine_signal",
    "record_bounce",
    "record_complaint",
    "record_delivery",
    "_suppression_table",
    "_feedback_events_table",
    "_audit_ledger",
    "SesNotification",
]
