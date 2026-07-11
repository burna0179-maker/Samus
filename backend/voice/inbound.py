"""Inbound AI Digital Receptionist — end-of-call handling.

When a caller rings a receptionist client's DID, Vapi answers with that
client's inbound assistant and POSTs an ``end-of-call-report`` to the voice
workcell's ``/vapi/webhook``. :func:`backend.voice.service.handle_webhook_event`
detects the inbound direction, resolves the owning client via
:mod:`backend.voice.receptionist_config`, and hands the event here.

This module:

  1. extracts the assistant's structured :class:`InboundSummary`
  2. persists recording / transcript / voicemail + a normalized
     :class:`InboundCallRecord` (``calls/<id>/call.json``)
  3. writes an inbound :class:`Conversation` row + :class:`Artifact` rows to
     samus-crm (best-effort, signed-HMAC POST)
  4. opens an :class:`OperatorTask` when the caller asked for an appointment
     or a callback
  5. emails the client when a voicemail was left or the call was urgent

Every step is best-effort and independently guarded — a CRM outage or a mail
failure degrades the result but never fails the webhook (Vapi would just
retry and double-handle the call).

This module must NOT import :mod:`backend.voice.service`: service imports it
(the inbound fork in ``handle_webhook_event``), so the dependency is
one-directional.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from backend.common.config import get_settings
from backend.common.dates import iso_now
from backend.common.email_backend import EmailBackendError, send_email
from backend.common.http_client import signed_post_json

from . import inbound_storage
from .models import InboundCallRecord, InboundSummary, ReceptionistConfig


_LOG = logging.getLogger("samus.voice.inbound")

# Vapi's ``call.id`` is raw, unvalidated webhook JSON and is used to build the
# call's artifact directory path. Restrict it to a safe charset so a traversal
# payload (``../``, absolute paths) can never escape the artifacts root —
# CWE-22. A rejected id falls back to a synthesized safe default.
_VALID_CALL_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


# ---------------------------------------------------------------------------
# Outcome shape returned to the webhook router
# ---------------------------------------------------------------------------

@dataclass
class InboundCallOutcome:
    """Result of handling one inbound receptionist call.

    The webhook router folds this into a :class:`WebhookResult`. ``crm_ok``
    is True when every attempted CRM write succeeded (or was correctly
    skipped); a hard failure on any write flips it False so the operator
    sees the degradation in the audit ledger.
    """
    call_id: str
    customer_slug: str
    outcome: str = ""
    crm_ok: bool = False
    crm_error: str | None = None
    tasks_created: int = 0
    alert_sent: bool = False
    metered_ok: bool = False
    metering_note: str | None = None
    record: InboundCallRecord | None = None
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Extraction / classification helpers
# ---------------------------------------------------------------------------

def _extract_inbound_summary(structured: dict[str, Any] | None) -> InboundSummary:
    """Pull the assistant's structured ``inbound_summary`` from Vapi's payload.

    Mirrors :func:`backend.voice.service._extract_lead_summary`: the JSON
    arrives either nested under ``structuredData.inbound_summary`` (canonical)
    or flattened at the root. A missing / invalid block yields an empty
    :class:`InboundSummary` — the call is still logged, just without intent.
    """
    if not structured or not isinstance(structured, dict):
        return InboundSummary()
    candidate = structured.get("inbound_summary")
    if isinstance(candidate, dict):
        try:
            return InboundSummary.model_validate(candidate)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("structuredData.inbound_summary failed validation: %s", exc)
            return InboundSummary()
    try:
        return InboundSummary.model_validate(structured)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("structuredData root did not match InboundSummary: %s", exc)
        return InboundSummary()


def _was_answered(ended_reason: str) -> bool:
    """Heuristic: did the receptionist actually engage the caller?

    Vapi has no single "answered" flag. We treat the call as answered unless
    the ``endedReason`` clearly signals it never connected. PILOT NOTE: tune
    the token set against the first real inbound calls.
    """
    er = (ended_reason or "").lower()
    fail_tokens = ("no-answer", "failed", "error", "not-found", "busy")
    return not any(tok in er for tok in fail_tokens)


def _parse_iso(raw: str | None) -> datetime | None:
    """Parse a Vapi ISO-8601 timestamp (``...Z``) into a datetime, or None."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration_seconds(duration_seconds: float | None,
                      raw_call: dict[str, Any]) -> int:
    """Whole-second call duration.

    Prefers Vapi's ``durationSeconds`` off the end-of-call report; falls back
    to ``endedAt - startedAt`` on the call object. 0 when neither is usable —
    the metering layer simply reports nothing for a 0-second call.
    """
    if duration_seconds is not None:
        return max(0, int(round(duration_seconds)))
    started = _parse_iso(raw_call.get("startedAt"))
    ended = _parse_iso(raw_call.get("endedAt"))
    if started and ended and ended >= started:
        return int((ended - started).total_seconds())
    return 0


def _classify_outcome(summary: InboundSummary, *, answered: bool,
                      voicemail_left: bool) -> str:
    """Single-word call outcome for the Conversation row + briefing rollup."""
    if not answered:
        return "missed"
    if summary.transferred:
        return "transferred"
    if summary.appointment_requested:
        return "appointment_requested"
    if summary.callback_requested:
        return "callback_requested"
    if voicemail_left:
        return "voicemail"
    return "handled"


def _is_urgent(summary: InboundSummary, config: ReceptionistConfig,
               transcript: str, vapi_summary: str) -> bool:
    """True when the assistant flagged urgency or an escalation keyword hit."""
    if summary.urgent:
        return True
    haystack = f"{transcript}\n{vapi_summary}\n{summary.reason_for_call or ''}".lower()
    return any(kw.strip().lower() in haystack
               for kw in config.escalation_keywords if kw.strip())


# ---------------------------------------------------------------------------
# CRM dispatch (signed HMAC POST — best-effort, never raises)
# ---------------------------------------------------------------------------

async def _signed_crm_post(path: str, payload: dict[str, Any]) -> tuple[bool, str | None]:
    """POST ``payload`` to samus-crm at ``path``. Returns (ok, error_or_none)."""
    settings = get_settings()
    crm_url = settings.gateway_urls.get("crm")
    if not crm_url:
        return False, "crm_url_unset"
    if not settings.shared_hmac_key:
        return False, "shared_hmac_key_unset"
    try:
        resp = await signed_post_json(crm_url, path, payload, retries=2)
    except Exception as exc:  # noqa: BLE001 — network / circuit / 5xx
        _LOG.warning("crm dispatch failed (%s): %s", path, exc)
        return False, f"crm_dispatch_error: {exc}"
    if resp.status_code >= 400:
        return False, f"crm_http_{resp.status_code}: {(resp.text or '')[:120]}"
    return True, None


def _conversation_payload(record: InboundCallRecord, summary: InboundSummary,
                          transcript: str, outcome: str) -> dict[str, Any]:
    """Body for ``POST /crm/conversations`` — an inbound-direction Conversation.

    ``conversation_id`` is derived from the Vapi call id so a webhook
    redelivery overwrites the same row instead of fanning out duplicates.
    ``prospect_id`` is empty: an inbound caller is not a sales prospect.
    """
    return {
        "conversation_id": f"cv_vapi_{record.call_id}",
        "prospect_id": "",
        "channel": "call",
        "direction": "inbound",
        "customer_id": record.customer_slug,
        "caller_number": record.caller_number,
        "answered": record.answered,
        "voicemail_left": record.voicemail_left,
        "status": "completed",
        "started_at": record.started_at,
        "ended_at": record.ended_at,
        "duration_sec": record.duration_sec,
        "transcript": transcript,
        "summary": record.summary,
        "structured_data": {"inbound_summary": summary.model_dump()},
        "outcome": outcome,
        "recording_url": record.recording_path,
        "source": "voice.receptionist",
        "source_ref": record.call_id,
    }


def _artifact_payload(record: InboundCallRecord, kind: str, title: str,
                      storage_url: str, mime_type: str) -> dict[str, Any]:
    """Body for ``POST /crm/artifacts`` — a persisted call artifact."""
    return {
        "kind": kind,
        "owner_entity_kind": "customer",
        "owner_entity_id": record.customer_slug,
        "title": title,
        "storage_url": storage_url,
        "mime_type": mime_type,
        "source": "voice.receptionist",
        "created_by": "voice.receptionist",
    }


def _operator_task_payload(record: InboundCallRecord, summary: InboundSummary,
                           kind: str, title: str, description: str) -> dict[str, Any]:
    """Body for ``POST /crm/operator-tasks`` — an appointment / callback to-do."""
    return {
        "kind": kind,
        "title": title,
        "description": description,
        "related_entity_kind": "customer",
        "related_entity_id": record.customer_slug,
        "source": "voice.receptionist",
        "source_ref": record.call_id,
    }


# ---------------------------------------------------------------------------
# Metered usage dispatch (voice -> finance -> Stripe Meter)
# ---------------------------------------------------------------------------

async def _report_usage(config: ReceptionistConfig,
                        record: InboundCallRecord) -> tuple[bool, str | None]:
    """Report the call's billable minutes to finance's Stripe-Meter route.

    The Stripe write lives in the finance workcell (one container holds the
    Stripe secret); voice POSTs the duration over signed HMAC, mirroring how
    it already dispatches to CRM. A deliberate no-op (returns ``(True, note)``)
    when the client has no ``stripe_customer_id`` or the call had 0 duration —
    the call is still logged, just not billed.

    FIN-08 fail-closed: if the config loader could not confirm this client's
    ``stripe_customer_id`` (typo / stale / wrong id), the loader set
    ``metering_disabled`` and this returns ``(False, "metering_disabled_…")``
    so NO meter event is dispatched — never silently bill the wrong customer.
    """
    # FIN-08: refuse to dispatch when the loader flagged an unconfirmed id.
    # This is fail-closed: disable metering, never forward an unvalidated
    # customer id into a billing write. The call is still logged above.
    if getattr(config, "metering_disabled", False):
        return False, "metering_disabled_invalid_customer"
    if not config.stripe_customer_id:
        return True, "metering_skipped: no stripe_customer_id"
    if record.duration_sec <= 0:
        return True, "metering_skipped: zero duration"
    settings = get_settings()
    finance_url = settings.gateway_urls.get("finance")
    if not finance_url:
        return False, "finance_url_unset"
    if not settings.shared_hmac_key:
        return False, "shared_hmac_key_unset"
    payload = {
        "stripe_customer_id": config.stripe_customer_id,
        "call_id": record.call_id,
        "duration_seconds": float(record.duration_sec),
        "sku_id": config.billing_sku_id,
    }
    try:
        resp = await signed_post_json(finance_url, "/meter-event", payload, retries=2)
    except Exception as exc:  # noqa: BLE001 — network / circuit / 5xx
        _LOG.warning("usage metering dispatch failed: %s", exc)
        return False, f"metering_dispatch_error: {exc}"
    if resp.status_code >= 400:
        return False, f"metering_http_{resp.status_code}: {(resp.text or '')[:120]}"
    return True, None


# ---------------------------------------------------------------------------
# Client alert email
# ---------------------------------------------------------------------------

def _send_client_alert(config: ReceptionistConfig, record: InboundCallRecord,
                       summary: InboundSummary, *, urgent: bool) -> bool:
    """Email the client about a voicemail / urgent call. Best-effort.

    Returns True when an email was sent. Skips silently (returns False) when
    no ``voicemail_email`` is configured.
    """
    to = (config.voicemail_email or "").strip()
    if not to:
        return False
    flavour = "Urgent call" if urgent else "New voicemail"
    caller = record.caller_number or "unknown caller"
    lines = [
        f"{flavour} handled by your AI receptionist.",
        "",
        f"Business: {config.business_name or config.customer_slug}",
        f"Caller:   {caller}" + (f" ({summary.caller_name})" if summary.caller_name else ""),
        f"When:     {record.ended_at or record.started_at}",
        f"Duration: {record.duration_sec}s",
    ]
    if summary.reason_for_call:
        lines += ["", f"Reason for call: {summary.reason_for_call}"]
    if summary.message:
        lines += ["", "Message left:", summary.message]
    if summary.callback_requested and summary.callback_number:
        lines += ["", f"Callback requested: {summary.callback_number}"]
    if record.summary:
        lines += ["", "Call summary:", record.summary]
    body = "\n".join(lines)
    subject = f"[{config.business_name or 'Receptionist'}] {flavour} — {caller}"
    try:
        send_email(to, subject, body, reply_to=config.summary_email or None, message_kind="transactional")
    except (EmailBackendError, NotImplementedError, ValueError) as exc:
        _LOG.warning("client alert email failed for %s: %s", config.customer_slug, exc)
        return False
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def handle_inbound_end_of_call(
    event: Any, config: ReceptionistConfig,
) -> InboundCallOutcome:
    """Process one inbound ``end-of-call-report`` for a receptionist client.

    ``event`` is a :class:`backend.voice.models.VapiWebhookEvent`. Never
    raises: every external step is independently guarded.
    """
    msg = event.message
    raw_call = msg.call if isinstance(msg.call, dict) else {}
    call_id = str(raw_call.get("id") or "") or f"inbound_{iso_now()}"
    # Reject a malformed / traversal-bearing call id and fall back to a safe
    # synthesized default rather than letting it reach a filesystem path.
    if not _VALID_CALL_ID.fullmatch(call_id):
        _LOG.warning("inbound call id rejected (unsafe charset): %r", call_id)
        call_id = f"inbound_{iso_now()}"

    summary = _extract_inbound_summary(msg.structuredData)
    transcript = msg.transcript or ""
    vapi_summary = msg.summary or ""
    ended_reason = msg.endedReason or ""

    customer_block = raw_call.get("customer")
    caller_number = ""
    if isinstance(customer_block, dict):
        caller_number = str(customer_block.get("number") or "")

    answered = _was_answered(ended_reason)
    # A captured message IS the voicemail — Vapi gives no explicit voicemail
    # flag, so the assistant's structured ``message`` field is the signal.
    voicemail_left = bool(summary.message)
    duration_sec = _duration_seconds(msg.durationSeconds, raw_call)
    outcome = _classify_outcome(summary, answered=answered,
                                voicemail_left=voicemail_left)

    record = InboundCallRecord(
        call_id=call_id,
        customer_slug=config.customer_slug,
        caller_number=caller_number,
        started_at=str(raw_call.get("startedAt") or ""),
        ended_at=str(raw_call.get("endedAt") or ""),
        duration_sec=duration_sec,
        answered=answered,
        voicemail_left=voicemail_left,
        ended_reason=ended_reason,
        summary=vapi_summary,
        inbound_summary=summary,
    )

    # 1. Persist artifacts + call.json (fills *_path on the record).
    record = inbound_storage.persist_inbound_call(
        record,
        transcript=transcript,
        recording_url=msg.recordingUrl or "",
    )

    result = InboundCallOutcome(
        call_id=call_id, customer_slug=config.customer_slug,
        outcome=outcome, record=record,
    )

    # 2. CRM — inbound Conversation row.
    conv_ok, conv_err = await _signed_crm_post(
        "/crm/conversations",
        _conversation_payload(record, summary, transcript, outcome),
    )

    # 3. CRM — Artifact rows for whatever actually persisted to disk.
    artifact_errs: list[str] = []
    artifacts = [
        ("call_recording", "Inbound call recording", record.recording_path, "audio/mpeg"),
        ("call_transcript", "Inbound call transcript", record.transcript_path, "text/plain"),
        ("voicemail", "Voicemail", record.voicemail_path, "audio/mpeg"),
    ]
    for kind, title, path, mime in artifacts:
        if not path:
            continue
        a_ok, a_err = await _signed_crm_post(
            "/crm/artifacts", _artifact_payload(record, kind, title, path, mime),
        )
        if not a_ok and a_err:
            artifact_errs.append(f"{kind}:{a_err}")

    # 4. CRM — OperatorTask for an appointment or a callback request.
    if summary.appointment_requested and config.booking_capture:
        desc = (
            f"Inbound caller {caller_number or '(unknown)'}"
            f"{' — ' + summary.caller_name if summary.caller_name else ''} "
            f"asked to book an appointment.\n\n"
            f"Details: {summary.appointment_details or '(none captured)'}\n"
            f"Call: {call_id}"
        )
        t_ok, t_err = await _signed_crm_post(
            "/crm/operator-tasks",
            _operator_task_payload(
                record, summary, "schedule",
                f"Book appointment — {config.business_name or config.customer_slug}",
                desc,
            ),
        )
        if t_ok:
            result.tasks_created += 1
        elif t_err:
            artifact_errs.append(f"task_schedule:{t_err}")
    if summary.callback_requested:
        cb_number = summary.callback_number or caller_number or "(unknown)"
        desc = (
            f"Inbound caller requested a callback at {cb_number}.\n"
            f"Reason: {summary.reason_for_call or '(none captured)'}\n"
            f"Call: {call_id}"
        )
        t_ok, t_err = await _signed_crm_post(
            "/crm/operator-tasks",
            _operator_task_payload(
                record, summary, "call",
                f"Call back {cb_number} — {config.business_name or config.customer_slug}",
                desc,
            ),
        )
        if t_ok:
            result.tasks_created += 1
        elif t_err:
            artifact_errs.append(f"task_callback:{t_err}")

    # 5. Email the client on a voicemail or an urgent call.
    urgent = _is_urgent(summary, config, transcript, vapi_summary)
    if voicemail_left or urgent:
        result.alert_sent = _send_client_alert(config, record, summary, urgent=urgent)

    # 6. Report billable minutes to the Stripe Meter (via finance). Best-
    # effort — a metering failure is logged here + in meter_events.jsonl on
    # the finance side; it never fails the webhook.
    metered_ok, metering_note = await _report_usage(config, record)
    result.metered_ok = metered_ok
    result.metering_note = metering_note
    if not metered_ok:
        _LOG.warning("usage metering not recorded for %s: %s",
                     call_id, metering_note)

    # Roll the result up. Artifact / task / metering failures are surfaced but
    # do not by themselves flip crm_ok — the Conversation row is the primary
    # record of the call.
    result.crm_ok = conv_ok
    errs: list[str] = []
    if conv_err:
        errs.append(f"conversation:{conv_err}")
    errs.extend(artifact_errs)
    result.crm_error = "; ".join(errs) if errs else None
    return result
