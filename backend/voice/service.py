"""Voice service — initiate Vapi calls + handle inbound webhook events.

Two surfaces:

  - :func:`initiate_call` — operator-driven outbound; calls VapiClient.create_call,
    audit-logs the result. Degrades when ``VAPI_API_KEY`` is unset (returns
    ``vapi_api_key_unset`` rather than raising).
  - :func:`handle_webhook_event` — Vapi-driven inbound; routes by message type.
    For ``end-of-call-report`` it extracts the structured ``lead_summary``,
    audit-logs it, and forwards a memory-store write to the memory workcell
    via the shared-HMAC client. The CRM persist side of Node 7 (a dedicated
    ``samus_voice_calls`` DDB table) is deferred until the table is
    provisioned — see :mod:`backend.voice.__init__` for status.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from backend.common import events, persistence
from backend.common.config import get_settings
from backend.common.dates import iso_now
from backend.common.http_client import signed_post_json
from backend.crm.call_outcomes import state_for_outcome
from backend.prospecting.contact_validation import assess_email

from . import autonomous, inbound
from .callback_lookup import index_outbound_call
from .client import VapiClient, VapiError
from backend.tools.wordpress_pages import submit_product_page
from .models import (
    CallListResult,
    CallStatusResult,
    InitiateCallRequest,
    InitiateCallResult,
    LeadSummary,
    VapiWebhookEvent,
    VoiceCallSummary,
    VoiceCallSummaryRow,
    WebhookResult,
)
from .receptionist_config import (
    resolve_customer_for_number,
    resolve_customer_for_vapi_number,
)


_LOG = logging.getLogger("samus.voice.service")
_AUDIT_PATH_DEFAULT = "/opt/samus/data/voice/voice_audit.jsonl"
# Rich-payload events ledger — separate from voice_audit (which hashes
# payloads by design). The morning-briefing rollup + dialer rollup both
# read from this. Dialer attempts AND webhook end-of-call summaries land
# here in their raw form so we can aggregate by outcome / tier / intent.
_EVENTS_PATH_DEFAULT = "/opt/samus/data/voice/voice_events.jsonl"
_MEMORY_NAMESPACE = "voice.calls"


# persistence.JsonlLedger uses an instance-local threading.Lock (self._lock),
# so two separate instances writing to the same file are not mutually excluded.
# Module-level singletons ensure all callers share one lock per file path.
_audit_ledger_instance: persistence.JsonlLedger | None = None
_events_ledger_instance: persistence.JsonlLedger | None = None


def _audit_ledger() -> persistence.JsonlLedger:
    global _audit_ledger_instance
    if _audit_ledger_instance is None:
        _audit_ledger_instance = persistence.JsonlLedger(
            os.getenv("SAMUS_VOICE_AUDIT_PATH", _AUDIT_PATH_DEFAULT),
        )
    return _audit_ledger_instance


def _events_ledger() -> persistence.JsonlLedger:
    global _events_ledger_instance
    if _events_ledger_instance is None:
        _events_ledger_instance = persistence.JsonlLedger(
            os.getenv("SAMUS_VOICE_EVENTS_PATH", _EVENTS_PATH_DEFAULT),
        )
    return _events_ledger_instance


def _append_event(record: dict[str, Any]) -> None:
    try:
        _events_ledger().append(record)
    except OSError as exc:
        _LOG.warning("voice event ledger append failed: %s", exc)


# ---------------------------------------------------------------------------
# Fast-ACK for the end-of-call-report webhook (GAP-9)
#
# ROOT CAUSE: Vapi's per-assistant ``server.timeoutSeconds`` (live value = 20s)
# bounds how long it waits for ``/vapi/webhook`` to ACK an ``end-of-call-report``
# before it ABANDONS delivery — and Vapi does NOT retry an end-of-call-report
# that times out. The outbound end-of-call branch below does several *sequential
# awaited* HTTP round-trips before returning 200: memory dispatch
# (``signed_post_json`` read=15s, retries=2), two CRM dispatches (Conversation +
# CallState, each read=15s, retries=2), a WordPress product-page POST, plus the
# session monitor. Any one slow/unhealthy peer pushes the ACK past 20s, Vapi
# gives up, and the terminal report never lands — which is exactly the observed
# symptom (only the trivial non-terminal branches, which just append an audit
# row, ever arrive).
#
# FIX: ACK Vapi within milliseconds and run the heavy memory/CRM/WordPress/
# session-monitor work in a detached background task. Idempotent + best-effort
# (it already was), so a fast 200 loses nothing the operator relied on.
#
# The flag defaults ON in production. Tests that assert on the *result* of the
# dispatch (memory_dispatch_ok / crm_dispatch_ok) set it to "0" so the handler
# stays synchronous and byte-identical to the pre-GAP-9 behaviour.
def _fast_ack_enabled() -> bool:
    # Explicit override always wins.
    raw = os.getenv("SAMUS_VOICE_WEBHOOK_FAST_ACK")
    if raw is not None:
        return raw.strip().lower() not in ("0", "false", "no", "off", "")
    # Under pytest the handler must run synchronously: the existing webhook
    # suite calls ``asyncio.run(handle_webhook_event(...))`` and asserts on the
    # dispatch *results* (memory_dispatch_ok / crm_dispatch_ok). A detached
    # background task would not run before that loop closes. Auto-default OFF in
    # tests preserves that contract without touching every test; the dedicated
    # fast-ACK tests opt back IN with the explicit env var above.
    if os.getenv("PYTEST_CURRENT_TEST"):
        return False
    return True


# In-process guard so a Vapi re-delivery (or our own duplicate) of the SAME
# end-of-call-report is processed once. Bounded ring to avoid unbounded growth.
_PROCESSED_CALL_IDS: "collections.OrderedDict[str, bool]" = None  # type: ignore[assignment]  # noqa: F821
_PROCESSED_MAX = 512
# Strong refs to in-flight background tasks so the event loop does not GC them
# mid-flight (asyncio only keeps a weak ref to scheduled tasks).
_BG_TASKS: set = set()


def _already_processed(call_id: str | None) -> bool:
    """True if this call_id's end-of-call-report was already handled.

    Marks unseen call_ids as processed. ``None``/empty call_id is never
    deduped (we can't correlate it) and always returns False.
    """
    import collections

    global _PROCESSED_CALL_IDS
    if _PROCESSED_CALL_IDS is None:
        _PROCESSED_CALL_IDS = collections.OrderedDict()
    if not call_id:
        return False
    if call_id in _PROCESSED_CALL_IDS:
        return True
    _PROCESSED_CALL_IDS[call_id] = True
    while len(_PROCESSED_CALL_IDS) > _PROCESSED_MAX:
        _PROCESSED_CALL_IDS.popitem(last=False)
    return False


def _spawn_bg(coro) -> None:
    """Schedule a detached background coroutine, keeping a strong ref.

    Used to run the heavy end-of-call processing after the webhook has already
    ACKed Vapi. Any exception is swallowed + logged inside the wrapper so a
    background failure never surfaces as an unhandled task exception.
    """

    async def _runner() -> None:
        try:
            await coro
        except Exception as exc:  # noqa: BLE001 — detached; log + drop
            _LOG.warning("end-of-call background processing failed: %s", exc)

    task = asyncio.ensure_future(_runner())
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


def _new_vapi_client() -> VapiClient | None:
    """Build a VapiClient from settings. Returns None when key is missing."""
    settings = get_settings()
    key = (settings.vapi_api_key or "").strip()
    if not key:
        return None
    return VapiClient(api_key=key)


# ---------------------------------------------------------------------------
# Outbound — operator initiates a call
# ---------------------------------------------------------------------------

# Durable daily-counter key for the constitutional call cap (HOTL T5).
_CALL_CAP_COUNTER_KEY = "voice.calls"


def _check_call_cap(req: InitiateCallRequest) -> InitiateCallResult | None:
    """Constitutional daily call cap — runs before every Vapi dial.

    Reads the durable per-day counter (survives worker restarts). At/over
    ``settings.samus_max_calls_per_day`` -> record + escalate + return a
    structured ``call_cap_reached`` result (NOT a raise — every caller already
    treats a non-None ``vapi_error`` as a failed dial). Returns ``None`` when
    the dial is under the cap. Fail-open on any unexpected error in the check.
    """
    try:
        from backend.common import daily_counter

        cap = int(getattr(get_settings(), "samus_max_calls_per_day", 40))
        if cap <= 0:
            return None
        already = daily_counter.count_today(_CALL_CAP_COUNTER_KEY)
        if already < cap:
            return None
        why = f"daily call cap reached: {already} calls today >= cap {cap}; Vapi dial blocked"
        _meta = req.metadata or {}
        from backend.common.business_events import DECISION_MADE, emit_business_event

        emit_business_event(
            DECISION_MADE,
            workcell="voice",
            prospect_id=(str(_meta.get("prospect_id") or "") or None),
            metadata={
                "decision": "call_cap_blocked",
                "why": why,
                "calls_today": already,
                "cap": cap,
            },
        )
        try:
            from backend.crm import service as crm
            from backend.crm.models import CreateOperatorTaskRequest

            crm.create_operator_task(
                CreateOperatorTaskRequest(
                    kind="review",
                    title=f"Daily call cap reached ({already}/{cap})",
                    description=why,
                    source="voice_call_cap",
                    source_ref=str(_meta.get("prospect_id") or ""),
                )
            )
        except Exception as exc:  # noqa: BLE001 — the block itself is the deliverable
            _LOG.warning("call-cap operator-task creation failed: %s", exc)
        ev = events.build_audit_event(
            service="voice",
            task_id="initiate_call",
            action="initiate_call",
            input_payload={
                "assistant_id": req.assistant_id,
                "phone_number_id": req.phone_number_id,
            },
            output_payload={"vapi_error": "call_cap_reached", "calls_today": already, "cap": cap},
            status="failed",
        )
        _append_audit(ev)
        _LOG.warning("%s", why)
        return InitiateCallResult(
            call_id="",
            status=None,
            vapi_error="call_cap_reached",
        )
    except Exception as exc:  # noqa: BLE001 — a counter fault never wedges dials
        _LOG.warning("call-cap check failed (allowing dial): %s", exc)
        return None


def initiate_call(req: InitiateCallRequest) -> InitiateCallResult:
    """``POST /voice/call`` — create a Vapi outbound call.

    Returns ``vapi_error="vapi_api_key_unset"`` (with ``call_id=""``) when the
    key isn't configured; otherwise returns the created call's id + status,
    or the Vapi error string on REST failure.

    HOTL Tranche 5: a constitutional daily call cap
    (``SAMUS_MAX_CALLS_PER_DAY``) blocks the dial once today's count is at/over
    the ceiling — returns ``vapi_error="call_cap_reached"`` (structured, never
    raises) so every existing caller treats it exactly like a failed dial.
    """
    capped = _check_call_cap(req)
    if capped is not None:
        return capped

    client = _new_vapi_client()
    if client is None:
        ev = events.build_audit_event(
            service="voice",
            task_id="initiate_call",
            action="initiate_call",
            input_payload={
                "assistant_id": req.assistant_id,
                "phone_number_id": req.phone_number_id,
            },
            output_payload={"vapi_error": "vapi_api_key_unset"},
            status="degraded",
        )
        _append_audit(ev)
        return InitiateCallResult(call_id="", status=None, vapi_error="vapi_api_key_unset")

    try:
        call = client.create_call(
            assistant_id=req.assistant_id,
            phone_number_id=req.phone_number_id,
            customer_number=req.customer_number,
            customer_name=req.customer_name,
            metadata=req.metadata or None,
        )
    except VapiError as exc:
        _LOG.warning("vapi create_call failed: %s", exc)
        ev = events.build_audit_event(
            service="voice",
            task_id="initiate_call",
            action="initiate_call",
            input_payload={
                "assistant_id": req.assistant_id,
                "phone_number_id": req.phone_number_id,
            },
            output_payload={"vapi_error": str(exc)},
            status="failed",
        )
        _append_audit(ev)
        return InitiateCallResult(call_id="", status=None, vapi_error=str(exc))

    ev = events.build_audit_event(
        service="voice",
        task_id=call.id,
        action="initiate_call",
        input_payload={
            "assistant_id": req.assistant_id,
            "phone_number_id": req.phone_number_id,
            "customer_number_tail": req.customer_number[-4:],
        },
        output_payload={"call_id": call.id, "status": call.status},
        status="completed",
    )
    _append_audit(ev)
    # Unified business-event ledger (HOTL Tranche 1) — one call.placed row per
    # successfully created outbound call. prospect_id rides in on the caller's
    # metadata (outreach.send_message / dialer both stamp it). Fail-soft by
    # contract: emit_business_event never raises.
    from backend.common.business_events import CALL_PLACED, emit_business_event

    _meta = req.metadata or {}
    emit_business_event(
        CALL_PLACED,
        workcell="voice",
        prospect_id=(str(_meta.get("prospect_id") or "") or None),
        campaign_id=(str(_meta.get("campaign_id") or "") or None),
        metadata={
            "call_id": call.id,
            "status": call.status or "",
            "source": str(_meta.get("source") or ""),
        },
    )
    # Tick the durable constitutional call-cap counter for this real dial
    # (HOTL T5). Best-effort — a counter fault never fails a placed call.
    try:
        from backend.common import daily_counter

        daily_counter.increment(_CALL_CAP_COUNTER_KEY)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("call-cap counter increment failed: %s", exc)
    return InitiateCallResult(call_id=call.id, status=call.status, vapi_error=None)


def get_call_status(call_id: str) -> CallStatusResult:
    """``GET /voice/call/{id}`` — fetch a single call's current state."""
    client = _new_vapi_client()
    if client is None:
        return CallStatusResult(call=None, vapi_error="vapi_api_key_unset")
    try:
        call = client.get_call(call_id)
    except VapiError as exc:
        _LOG.warning("vapi get_call failed: %s", exc)
        return CallStatusResult(call=None, vapi_error=str(exc))
    return CallStatusResult(call=call, vapi_error=None)


def list_recent_calls(limit: int = 10) -> CallListResult:
    """``GET /voice/calls`` — list recent calls."""
    client = _new_vapi_client()
    if client is None:
        return CallListResult(calls=[], vapi_error="vapi_api_key_unset")
    try:
        calls = client.list_calls(limit=limit)
    except VapiError as exc:
        _LOG.warning("vapi list_calls failed: %s", exc)
        return CallListResult(calls=[], vapi_error=str(exc))
    return CallListResult(calls=calls, vapi_error=None)


# ---------------------------------------------------------------------------
# Morning-briefing rollup over voice_events.jsonl (last 24h by default)
# ---------------------------------------------------------------------------


def _parse_audit_ts(raw: str | None) -> Any:
    """Parse an iso_now() string back into a tz-aware UTC datetime, or None."""
    from datetime import datetime, timezone

    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def get_voice_call_summary(window_hours: int = 24) -> VoiceCallSummary:
    """Roll up the voice_events.jsonl ledger for the briefing.

    Walks every line; filters by ``event.ts`` within the trailing
    ``window_hours``; aggregates:

      - ``dial_attempt``  -> dial_attempt_count + by-outcome + per-number initiated counts
      - ``end_of_call``   -> tier/intent/recommended_action + per-number answer tracking
      - ``dnc_write``     -> dnc_count
    """
    from datetime import datetime, timedelta, timezone

    path = Path(os.getenv("SAMUS_VOICE_EVENTS_PATH", _EVENTS_PATH_DEFAULT))

    # Retry queue depth — read independently of window; depth is current state.
    retry_queue_depth = 0
    try:
        from .retry import get_retry_entries

        retry_queue_depth = len(get_retry_entries(max_age_hours=48))
    except Exception:  # noqa: BLE001
        pass

    if not path.exists():
        return VoiceCallSummary(
            window_hours=window_hours,
            total_calls=0,
            initiated_count=0,
            end_of_call_count=0,
            dial_attempt_count=0,
            dial_attempts_by_outcome={},
            by_recommended_action={},
            by_tier={},
            avg_intent_score=None,
            booked_calls=[],
            recent_high_intent=[],
            answer_rate_by_number={},
            dnc_count=0,
            retry_queue_depth=retry_queue_depth,
            log_loaded=False,
            ts=iso_now(),
        )

    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    dial_attempts = 0
    dial_outcomes: dict[str, int] = {}
    eoc_rows: list[dict] = []
    dnc_count = 0
    # Per-number tracking: initiated and answered counts.
    # "Answered" = end_of_call that is NOT voicemail/no-answer (endedReason
    # doesn't contain the no-answer family) and duration_sec > 0.
    _NO_ANSWER_REASONS = frozenset(
        {
            "no-answer",
            "voicemail",
            "machine_detected_silence",
            "machine_end_beep",
            "machine_end_silence",
            "machine_end_other",
        }
    )
    num_initiated: dict[str, int] = {}  # outbound_number_id -> initiated count
    num_answered: dict[str, int] = {}  # outbound_number_id -> answered count

    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_audit_ts(rec.get("ts"))
            if ts is None or ts < cutoff:
                continue
            kind = rec.get("kind") or ""
            if kind == "dial_attempt":
                dial_attempts += 1
                outcome = rec.get("outcome") or "(unknown)"
                dial_outcomes[outcome] = dial_outcomes.get(outcome, 0) + 1
                if outcome == "initiated":
                    nid = rec.get("outbound_number_id") or "(unknown)"
                    num_initiated[nid] = num_initiated.get(nid, 0) + 1
            elif kind == "end_of_call":
                eoc_rows.append(rec)
                nid = rec.get("outbound_number_id") or "(unknown)"
                ended_reason = (rec.get("ended_reason") or "").lower()
                if nid != "(unknown)" and ended_reason not in _NO_ANSWER_REASONS:
                    num_answered[nid] = num_answered.get(nid, 0) + 1
            elif kind == "dnc_write":
                dnc_count += 1

    initiated = dial_outcomes.get("initiated", 0)

    # Answer rate per number: answered / initiated (both from this window).
    answer_rate_by_number: dict[str, float] = {}
    for nid, init_count in num_initiated.items():
        answered = num_answered.get(nid, 0)
        answer_rate_by_number[nid] = round(answered / init_count, 3) if init_count else 0.0

    by_action: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    scores: list[int] = []
    booked: list[VoiceCallSummaryRow] = []
    high_intent: list[VoiceCallSummaryRow] = []
    for rec in eoc_rows:
        tier = rec.get("tier") or ""
        rec_action = rec.get("recommended_action") or ""
        intent = rec.get("intent_score")
        company = rec.get("company") or ""
        row = VoiceCallSummaryRow(
            call_id=rec.get("call_id") or "",
            received_at=rec.get("ts") or "",
            company=company,
            tier=tier,
            intent_score=int(intent) if isinstance(intent, (int, float)) else None,
            recommended_action=rec_action,
        )
        by_action[rec_action or "(none)"] = by_action.get(rec_action or "(none)", 0) + 1
        by_tier[tier or "(none)"] = by_tier.get(tier or "(none)", 0) + 1
        if isinstance(intent, (int, float)):
            scores.append(int(intent))
        if rec_action == "book_call":
            booked.append(row)
        if tier in ("high", "priority"):
            high_intent.append(row)

    avg_intent = round(sum(scores) / len(scores), 1) if scores else None

    return VoiceCallSummary(
        window_hours=window_hours,
        total_calls=initiated + len(eoc_rows),
        initiated_count=initiated,
        end_of_call_count=len(eoc_rows),
        dial_attempt_count=dial_attempts,
        dial_attempts_by_outcome=dial_outcomes,
        by_recommended_action=by_action,
        by_tier=by_tier,
        avg_intent_score=avg_intent,
        booked_calls=sorted(booked, key=lambda r: r.received_at, reverse=True)[:5],
        recent_high_intent=sorted(high_intent, key=lambda r: r.received_at, reverse=True)[:5],
        answer_rate_by_number=answer_rate_by_number,
        dnc_count=dnc_count,
        retry_queue_depth=retry_queue_depth,
        log_loaded=True,
        ts=iso_now(),
    )


# ---------------------------------------------------------------------------
# Inbound — Vapi pushes a webhook
# ---------------------------------------------------------------------------


def _extract_lead_summary(structured: dict[str, Any] | None) -> LeadSummary | None:
    """Pull Morgan's lead_summary from Vapi's structuredData block.

    Vapi's contract: the agent's end-of-call structured JSON arrives at
    ``message.structuredData``. Per the Morgan recovery doc, the structure
    is either ``lead_summary: {...}`` (canonical) or the bare fields at the
    root of structuredData (some Vapi assistant configs flatten it).
    """
    if not structured or not isinstance(structured, dict):
        return None
    candidate = structured.get("lead_summary")
    if isinstance(candidate, dict):
        try:
            return LeadSummary.model_validate(candidate)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("structuredData.lead_summary failed validation: %s", exc)
            return None
    # Fall back to root-level fields.
    try:
        return LeadSummary.model_validate(structured)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("structuredData root did not match LeadSummary: %s", exc)
        return None


def _memory_dispatch_payload(
    call_id: str, lead: LeadSummary, webhook_msg: dict[str, Any]
) -> dict[str, Any]:
    """Shape the body the memory workcell's POST /write expects."""
    return {
        "namespace": _MEMORY_NAMESPACE,
        "key": call_id,
        "value": {
            "call_id": call_id,
            "ts": iso_now(),
            "lead_summary": lead.model_dump(),
            "ended_reason": webhook_msg.get("endedReason"),
            "summary": webhook_msg.get("summary"),
            "recording_url": webhook_msg.get("recordingUrl"),
        },
    }


async def _post_to_memory(payload: dict[str, Any]) -> tuple[bool, str | None]:
    """POST end-of-call structured data to the memory workcell.

    Uses the shared-HMAC client. Memory base-URL comes from
    ``settings.gateway_urls['memory']`` (env: ``MEMORY_URL``); when unset
    the dispatch is skipped with an explicit error string in the result.
    """
    settings = get_settings()
    memory_url = settings.gateway_urls.get("memory")
    if not memory_url:
        return False, "memory_url_unset"
    if not settings.shared_hmac_key:
        return False, "shared_hmac_key_unset"
    try:
        resp = await signed_post_json(memory_url, "/write", payload, retries=2)
    except Exception as exc:  # noqa: BLE001 — network / circuit / 5xx
        _LOG.warning("memory dispatch failed: %s", exc)
        return False, f"memory_dispatch_error: {exc}"
    if resp.status_code >= 400:
        body_preview = (resp.text or "")[:120]
        return False, f"memory_http_{resp.status_code}: {body_preview}"
    return True, None


# Vapi's three agent verbs -> the canonical call-outcome taxonomy
# (backend.crm.call_outcomes). The spelling differs ("book_call" vs "booked"),
# so they are translated, not passed through — that is what makes a Vapi-booked
# deal and an operator-booked deal the same outcome in the CRM.
_VAPI_ACTION_TO_OUTCOME: dict[str, str] = {
    "book_call": "booked",
    "follow_up": "follow_up",
    "disqualify": "disqualified",
    "gatekeeper": "gatekeeper",
}


def _vapi_call_outcome(lead: LeadSummary | None, ended_reason: str | None) -> str:
    """Resolve a finished Vapi call to a canonical call outcome.

    When Morgan reached a person it emits a ``recommended_action`` — that is
    the outcome. When it did not (no lead summary), Vapi's mechanical
    ``endedReason`` still says *why*: a voicemail, or an unanswered / busy
    ring. Anything else — a connected call with no summary, a pipeline error —
    yields ``""``: we record no outcome rather than invent one. The empty
    string maps to CallState ``completed`` via ``state_for_outcome``.
    """
    action = getattr(lead, "recommended_action", None) if lead else None
    if action in _VAPI_ACTION_TO_OUTCOME:
        return _VAPI_ACTION_TO_OUTCOME[action]
    reason = (ended_reason or "").strip().lower()
    if "voicemail" in reason:
        return "voicemail"
    if "did-not-answer" in reason or "no-answer" in reason or "busy" in reason:
        return "no_answer"
    return ""


def _build_conversation_payload(
    call_id: str, lead: LeadSummary | None, prospect_id: str, webhook_msg: dict[str, Any]
) -> dict[str, Any]:
    """Shape the body samus-crm's POST /crm/conversations expects.

    conversation_id is derived from the Vapi call_id so the row is
    deterministically addressable — retries / re-deliveries overwrite the
    same row instead of fanning out duplicates.
    """
    ts = iso_now()
    structured: dict[str, Any] = {}
    if lead is not None:
        structured["lead_summary"] = lead.model_dump()
        # A contact handed to Morgan on the call (e.g. a gatekeeper's "email
        # the owner at...") is validated here — parity with the operator's
        # Log-Call.ps1 gatekeeper sub-prompt. A malformed / off-domain verdict
        # rides on the Conversation so the misdirection is on the record.
        offered = (lead.contact_offered or "").strip()
        if offered:
            verdict = assess_email(offered, business_name=lead.company or "")
            structured["contact_assessment"] = {
                "email": verdict.email,
                "verdict": verdict.verdict,
                "valid_syntax": verdict.valid_syntax,
                "reasons": verdict.reasons,
            }
    return {
        "conversation_id": f"cv_vapi_{call_id}",
        "prospect_id": prospect_id or "",
        "channel": "call",
        "status": "completed",
        "ended_at": ts,
        "transcript": webhook_msg.get("transcript") or "",
        "summary": webhook_msg.get("summary") or "",
        "structured_data": structured,
        "outcome": _vapi_call_outcome(lead, webhook_msg.get("endedReason")),
        "recording_url": webhook_msg.get("recordingUrl") or "",
        "source": "vapi",
        "source_ref": call_id,
    }


def _build_call_state_payload(
    call_id: str,
    lead: LeadSummary | None,
    prospect_id: str,
    ended_reason: str | None = None,
) -> dict[str, Any] | None:
    """Shape the body samus-crm's POST /crm/call-state/{prospect_id} expects.

    Returns None when we lack a prospect_id (CallState PK is prospect_id —
    no point dispatching without one; the caller logs + skips).

    ``state`` is derived from the canonical outcome via ``state_for_outcome``
    — NOT hardcoded ``completed``. A Vapi call that hit a voicemail or rang out
    now records ``voicemail`` / ``no_answer`` like the operator path, instead
    of being mislabelled a completed call.
    """
    if not prospect_id:
        return None
    outcome = _vapi_call_outcome(lead, ended_reason)
    # NB: prospect_id goes in the URL path (POST /crm/call-state/{prospect_id}),
    # NOT the body — CRM's UpsertCallStateBody is extra="forbid" and rejects it
    # (HTTP 422). Including it here silently 422'd every call's CRM call-state
    # dispatch since a CRM model refactor (~2026-06-23), so no call outcome
    # reached CRM and the HUD showed 0 calls despite live dialing. Fixed 7/08.
    return {
        "state": state_for_outcome(outcome),
        "last_call_id": call_id,
        "last_outcome": outcome,
        "updated_at": iso_now(),
    }


async def _post_to_crm_conversation(
    payload: dict[str, Any],
) -> tuple[bool, str | None]:
    """POST a Conversation upsert to samus-crm. Best-effort — never raises."""
    settings = get_settings()
    crm_url = settings.gateway_urls.get("crm")
    if not crm_url:
        return False, "crm_url_unset"
    if not settings.shared_hmac_key:
        return False, "shared_hmac_key_unset"
    try:
        resp = await signed_post_json(
            crm_url,
            "/crm/conversations",
            payload,
            retries=2,
        )
    except Exception as exc:  # noqa: BLE001 — network / circuit / 5xx
        _LOG.warning("crm conversation dispatch failed: %s", exc)
        return False, f"crm_dispatch_error: {exc}"
    if resp.status_code >= 400:
        body_preview = (resp.text or "")[:120]
        return False, f"crm_http_{resp.status_code}: {body_preview}"
    return True, None


async def _post_to_crm_call_state(
    prospect_id: str,
    payload: dict[str, Any],
) -> tuple[bool, str | None]:
    """POST a CallState upsert to samus-crm. Best-effort — never raises."""
    settings = get_settings()
    crm_url = settings.gateway_urls.get("crm")
    if not crm_url:
        return False, "crm_url_unset"
    if not settings.shared_hmac_key:
        return False, "shared_hmac_key_unset"
    path = f"/crm/call-state/{prospect_id}"
    try:
        resp = await signed_post_json(crm_url, path, payload, retries=2)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("crm call-state dispatch failed: %s", exc)
        return False, f"crm_dispatch_error: {exc}"
    if resp.status_code >= 400:
        body_preview = (resp.text or "")[:120]
        return False, f"crm_http_{resp.status_code}: {body_preview}"
    return True, None


async def _dispatch_to_crm(
    call_id: str,
    lead: LeadSummary | None,
    prospect_id: str,
    webhook_msg: dict[str, Any],
) -> tuple[bool, str | None]:
    """Fire both CRM writes (Conversation + CallState).

    Returns (ok_both, combined_error). A partial success (Conversation OK,
    CallState skipped because no prospect_id) is still ``ok_both=True`` since
    we did everything we could — the missing piece is a data gap upstream,
    not a CRM-side failure. Hard CRM failures on either dispatch flip the
    overall result to False so the operator sees the degradation.
    """
    conv_payload = _build_conversation_payload(
        call_id,
        lead,
        prospect_id,
        webhook_msg,
    )
    conv_ok, conv_err = await _post_to_crm_conversation(conv_payload)

    state_payload = _build_call_state_payload(
        call_id,
        lead,
        prospect_id,
        webhook_msg.get("endedReason"),
    )
    if state_payload is None:
        # No prospect_id -> can't upsert call-state. Treat as a non-error
        # data gap; the Conversation row carries the full transcript so we
        # haven't lost anything that the CallState would have stored.
        state_ok, state_err = True, None
    else:
        state_ok, state_err = await _post_to_crm_call_state(
            prospect_id,
            state_payload,
        )

    if conv_ok and state_ok:
        return True, None
    parts: list[str] = []
    if not conv_ok and conv_err:
        parts.append(f"conversation:{conv_err}")
    if not state_ok and state_err:
        parts.append(f"call_state:{state_err}")
    return False, "; ".join(parts) if parts else "crm_dispatch_unknown_error"


def _append_audit(ev: dict[str, Any]) -> None:
    try:
        _audit_ledger().append(ev)
    except OSError as exc:
        _LOG.warning("voice audit append failed: %s", exc)


# ---------------------------------------------------------------------------
# Post-call remediate -> next-touch loop (wired-DORMANT)
#
# Closes the outbound "audit -> remediate -> next-touch" loop. When
# SAMUS_POSTCALL_REMEDIATE_ENABLED is armed AND the finished call's classified
# outcome is a real buying signal (same classifier the buying-signal route uses
# — book_call / tier in high|priority / intent_score >= threshold; disqualify
# never qualifies), we:
#   (a) DELIVER the SEO remediation deliverable to the prospect by reusing the
#       existing backend.fulfill.fulfill_customer seam (audit -> render -> email
#       -> advance state), and
#   (b) ENQUEUE a next-touch by reusing the existing buying_signal enrollment
#       seam (maybe_enroll_buying_signal writes a warm-sequence enrollment).
#
# HARD GUARDRAIL: this NEVER initiates a phone call. "next-touch" is
# enrollment/queue only; any actual dial stays fenced by VR-G5 / ADR-002 behind
# the human FIRE gate. Both steps are best-effort + fail-soft — a delivery /
# enroll error is logged and swallowed, never propagated out of the webhook.
def _maybe_postcall_remediate(
    lead: LeadSummary | None,
    prospect_id: str,
    call_id: str | None,
    raw_metadata: dict[str, Any],
    settings: Any,
) -> None:
    """Best-effort post-call remediation + next-touch. Never raises."""
    try:
        if not getattr(settings, "postcall_remediate_enabled", False):
            return
        if lead is None or not prospect_id:
            return

        # Same "interested/positive" trigger the buying-signal route uses.
        from backend.outreach.buying_signal_route import (
            is_buying_signal,
            maybe_enroll_buying_signal,
        )

        threshold = int(getattr(settings, "outreach_buying_signal_intent_threshold", 70))
        if not is_buying_signal(lead, intent_threshold=threshold):
            return

        # Resolve the delivery target. Email: the address the prospect offered
        # on the call wins; else the enriched owner_email carried on the dial
        # metadata. URL: the prospect's site carried on the dial metadata.
        email = (getattr(lead, "contact_offered", None) or "").strip()
        if not email:
            email = str(raw_metadata.get("owner_email") or "").strip()
        audit_url = str(raw_metadata.get("website_url") or "").strip()
        company = getattr(lead, "company", None) or str(raw_metadata.get("company_name") or "")

        # (a) SEO remediation deliverable — reuse the fulfill seam. Fail-soft:
        # missing email/url just skips delivery (nothing to send to / audit).
        if email and audit_url:
            try:
                from backend.fulfill import fulfill_customer

                fulfill_customer(
                    email=email,
                    audit_url=audit_url,
                    company=company,
                    name=str(raw_metadata.get("owner_name") or ""),
                )
                _LOG.info(
                    "postcall_remediate: delivered SEO remediation prospect=%s",
                    prospect_id,
                )
            except Exception as exc:  # noqa: BLE001 — best-effort, never block
                _LOG.warning(
                    "postcall_remediate delivery failed prospect=%s: %s",
                    prospect_id,
                    exc,
                )
        else:
            _LOG.info(
                "postcall_remediate: no email/url for prospect=%s; delivery skipped",
                prospect_id,
            )

        # (b) Next-touch — reuse the buying_signal enrollment seam (queue only,
        # no dial). maybe_enroll_buying_signal is itself best-effort + gated;
        # here we ALWAYS enroll on a qualifying outcome (this loop's own flag is
        # the gate). It is idempotent on an active enrollment.
        try:
            maybe_enroll_buying_signal(
                prospect_id=prospect_id,
                lead=lead,
                now_iso=iso_now(),
                email=email,
                company=company,
                call_id=call_id or "",
                source="postcall_remediate",
                # This loop's own flag (postcall_remediate_enabled) is the gate;
                # enroll even if the cold buying-signal route is unarmed.
                enabled_override=True,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort, never block
            _LOG.warning(
                "postcall_remediate enroll failed prospect=%s: %s",
                prospect_id,
                exc,
            )
    except Exception as exc:  # noqa: BLE001 — defensive top-level guard
        _LOG.warning("postcall_remediate unexpected error: %s", exc)


# ---------------------------------------------------------------------------
# DNC suppression write
# ---------------------------------------------------------------------------

_suppression_table = None
_suppression_fail_count = 0
_SUPPRESSION_FAIL_ALERT_THRESHOLD = 3


def _get_suppression_table():
    global _suppression_table
    if _suppression_table is None:
        try:
            import boto3

            table_name = get_settings().ddb_suppression_table or "samus_suppression"
            _suppression_table = boto3.resource(
                "dynamodb",
                region_name="us-west-1",
            ).Table(table_name)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("suppression_table: boto3 init failed: %s", exc)
            return None
    return _suppression_table


def _normalize_phone_e164(phone: str) -> str:
    """Normalise a phone string to E.164 (mirrors callback_lookup._normalize_phone)."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return phone.strip() if phone.startswith("+") else f"+{digits}"


def _write_suppression(phone: str, prospect_id: str, call_id: str | None) -> None:
    """Write a DNC suppression record. Best-effort — logs on failure, never raises."""
    global _suppression_fail_count
    phone_e164 = _normalize_phone_e164(phone)
    if not phone_e164 or phone_e164 in ("+", "+1"):
        _LOG.warning("suppression: skipping write — invalid phone %r", phone)
        return

    table = _get_suppression_table()
    if table is None:
        _LOG.warning("suppression: DynamoDB table unavailable, skipping write for %s", phone_e164)
        return

    item = {
        "phone": phone_e164,
        "prospect_id": prospect_id or "",
        "source": "morgan_disqualify",
        "call_id": call_id or "",
        "added_at": iso_now(),
    }
    try:
        table.put_item(Item=item)
        _suppression_fail_count = 0
        _LOG.info("suppression: added %s (prospect=%s call=%s)", phone_e164, prospect_id, call_id)
        _append_event(
            {
                "ts": iso_now(),
                "kind": "dnc_write",
                "phone": phone_e164,
                "prospect_id": prospect_id or "",
                "call_id": call_id or "",
                "source": "morgan_disqualify",
            }
        )
    except Exception as exc:  # noqa: BLE001
        _suppression_fail_count += 1
        if _suppression_fail_count >= _SUPPRESSION_FAIL_ALERT_THRESHOLD:
            _LOG.error(
                "ALERT: samus_suppression DynamoDB write has failed %d consecutive times "
                "(phone=%s, prospect=%s). DNC requests may not be persisted.",
                _suppression_fail_count,
                phone_e164,
                prospect_id,
            )
        else:
            _LOG.warning("suppression write failed for %s: %s", phone_e164, exc)


# ---------------------------------------------------------------------------
# Inbound — AI Digital Receptionist fork
# ---------------------------------------------------------------------------


def _is_inbound_call(raw_call: dict[str, Any], settings: Any) -> bool:
    """True when an end-of-call-report is for an inbound receptionist call.

    Primary signal: Vapi tags inbound calls ``type == "inboundPhoneCall"``.
    Fallback: the call's ``phoneNumberId`` matches the configured inbound DID
    — covers Vapi payload-shape drift on the ``type`` field.
    """
    if str(raw_call.get("type") or "") == "inboundPhoneCall":
        return True
    pid = str(raw_call.get("phoneNumberId") or "")
    inbound_pid = (settings.vapi_inbound_phone_number_id or "").strip()
    return bool(pid and inbound_pid and pid == inbound_pid)


def _dialed_number(raw_call: dict[str, Any]) -> str:
    """Best-effort extraction of the dialed (our) E.164 from a Vapi call."""
    pn = raw_call.get("phoneNumber")
    if isinstance(pn, dict):
        return str(pn.get("number") or "")
    return ""


async def _handle_inbound_end_of_call(
    event: VapiWebhookEvent,
    raw_call: dict[str, Any],
    call_id: str | None,
) -> WebhookResult:
    """Route an inbound end-of-call-report to the receptionist handler.

    Resolves the owning client (by Vapi phone-number id, then dialed E.164).
    A call to a DID no active client claims is logged and dropped — never
    persisted to a client's call history, never billed.
    """
    pid = str(raw_call.get("phoneNumberId") or "")
    config = resolve_customer_for_vapi_number(pid) if pid else None
    if config is None:
        dialed = _dialed_number(raw_call)
        if dialed:
            config = resolve_customer_for_number(dialed)

    if config is None:
        ev = events.build_audit_event(
            service="voice",
            task_id=call_id or "inbound_unknown",
            action="webhook_inbound_unmatched",
            input_payload={"call_id": call_id, "phone_number_id": pid},
            output_payload={"matched": False},
            status="degraded",
        )
        _append_audit(ev)
        _LOG.warning("inbound call to unrecognized DID (phone_number_id=%s)", pid)
        return WebhookResult(
            received=True,
            message_type="end-of-call-report",
            call_id=call_id,
            lead_summary=None,
            memory_dispatch_ok=False,
            memory_dispatch_error="inbound_no_matching_receptionist_client",
            crm_dispatch_ok=False,
            crm_dispatch_error=None,
        )

    try:
        outcome = await inbound.handle_inbound_end_of_call(event, config)
        crm_ok, crm_err = outcome.crm_ok, outcome.crm_error
    except Exception as exc:  # noqa: BLE001 — inbound handler is best-effort
        _LOG.warning("inbound handler unexpected error: %s", exc)
        outcome = None
        crm_ok, crm_err = False, f"inbound_handler_error: {exc}"

    rec = outcome.record if outcome else None
    ev = events.build_audit_event(
        service="voice",
        task_id=call_id or "inbound_unknown",
        action="webhook_inbound_end_of_call",
        input_payload={
            "call_id": call_id,
            "customer_slug": config.customer_slug,
            "phone_number_id": pid,
        },
        output_payload={
            "outcome": outcome.outcome if outcome else "",
            "crm_dispatch_ok": crm_ok,
            "crm_dispatch_error": crm_err,
            "tasks_created": outcome.tasks_created if outcome else 0,
            "alert_sent": outcome.alert_sent if outcome else False,
            "metered_ok": outcome.metered_ok if outcome else False,
        },
        status="completed" if crm_ok else "degraded",
    )
    _append_audit(ev)
    # Distinct ``kind`` from outbound ``end_of_call`` so the outbound morning
    # briefing rollup (get_voice_call_summary) is not inflated by inbound
    # calls; the receptionist call-summary email reads calls/<id>/call.json.
    _append_event(
        {
            "ts": iso_now(),
            "kind": "inbound_end_of_call",
            "direction": "inbound",
            "call_id": call_id,
            "customer_slug": config.customer_slug,
            "outcome": outcome.outcome if outcome else "",
            "answered": rec.answered if rec else None,
            "voicemail_left": rec.voicemail_left if rec else None,
            "duration_sec": rec.duration_sec if rec else 0,
            "crm_dispatch_ok": crm_ok,
            "vapi_cost": raw_call.get("cost"),
            "duration_seconds": event.message.durationSeconds,
        }
    )
    return WebhookResult(
        received=True,
        message_type="end-of-call-report",
        call_id=call_id,
        lead_summary=None,
        memory_dispatch_ok=False,
        memory_dispatch_error=None,
        crm_dispatch_ok=crm_ok,
        crm_dispatch_error=crm_err,
    )


async def handle_webhook_event(event: VapiWebhookEvent) -> WebhookResult:
    """Route one Vapi webhook event.

    For ``end-of-call-report`` the structured ``lead_summary`` is extracted
    and forwarded to the memory workcell. For other message types the event
    is audit-logged with no downstream dispatch (status updates, transcripts,
    function-calls, etc. are observability data — they don't drive state).

    Async because the memory dispatch goes through ``signed_post_json`` which
    runs the request inside an event loop — FastAPI awaits this directly, and
    sync callers (tests, future SQS worker) wrap with ``asyncio.run``.
    """
    msg = event.message
    raw_call = msg.call if isinstance(msg.call, dict) else {}
    call_id = str(raw_call.get("id") or "") or None

    if msg.type != "end-of-call-report":
        # B2 — mid-call adaptation bridge (DORMANT by default). When
        # SAMUS_VOICE_MIDCALL_ENABLED is set, a live transcript turn is fed to
        # the adaptive layer and the recommendation is recorded on the audit
        # event. Default OFF → process_midcall_transcript returns None and this
        # is byte-identical to the prior log-and-drop behaviour. It never drives
        # a dial (see backend.voice.autonomous — voice_dial stays VR-G5-blocked).
        out_payload = {"dispatched": False, "reason": "non_terminal_event"}
        try:
            midcall = autonomous.process_midcall_transcript(msg)
        except Exception as exc:  # noqa: BLE001 — never let adaptation fault a webhook
            _LOG.warning("midcall adaptation failed: %s", exc)
            midcall = None
        if midcall is not None:
            out_payload = {
                "dispatched": False,
                "reason": "midcall_adaptation",
                "adaptation": midcall,
            }
        ev = events.build_audit_event(
            service="voice",
            task_id=call_id or msg.type,
            action="webhook",
            input_payload={"message_type": msg.type, "call_id": call_id},
            output_payload=out_payload,
            status="completed",
        )
        _append_audit(ev)
        return WebhookResult(
            received=True,
            message_type=msg.type,
            call_id=call_id,
            lead_summary=None,
            memory_dispatch_ok=False,
            memory_dispatch_error=None,
            crm_dispatch_ok=False,
            crm_dispatch_error=None,
        )

    # GAP-9 fast-ACK: an end-of-call-report triggers heavy, slow downstream
    # work (memory + CRM + WordPress + session monitor). Vapi only waits
    # server.timeoutSeconds (=20s live) for the ACK and will NOT retry a
    # timed-out report. So when fast-ACK is enabled we run that work in a
    # detached background task and return an immediate 200 here. Idempotent:
    # a re-delivered report for an already-seen call_id is dropped.
    settings = get_settings()
    if _fast_ack_enabled():
        if _already_processed(call_id):
            _LOG.info("end-of-call-report for %s already processed; ack-only", call_id)
            return WebhookResult(
                received=True,
                message_type="end-of-call-report",
                call_id=call_id,
                lead_summary=None,
                memory_dispatch_ok=False,
                memory_dispatch_error="duplicate_already_processed",
                crm_dispatch_ok=False,
                crm_dispatch_error=None,
            )
        if _is_inbound_call(raw_call, settings):
            _spawn_bg(_handle_inbound_end_of_call(event, raw_call, call_id))
        else:
            _spawn_bg(_process_outbound_end_of_call(event, raw_call, call_id, settings))
        return WebhookResult(
            received=True,
            message_type="end-of-call-report",
            call_id=call_id,
            lead_summary=None,
            memory_dispatch_ok=False,
            memory_dispatch_error="accepted_async",
            crm_dispatch_ok=False,
            crm_dispatch_error=None,
        )

    # Synchronous path (fast-ACK disabled — tests, or operator opt-out).
    if _is_inbound_call(raw_call, settings):
        return await _handle_inbound_end_of_call(event, raw_call, call_id)
    return await _process_outbound_end_of_call(event, raw_call, call_id, settings)


async def _process_outbound_end_of_call(
    event: VapiWebhookEvent,
    raw_call: dict[str, Any],
    call_id: str | None,
    settings: Any,
) -> WebhookResult:
    """Heavy outbound end-of-call processing: lead extraction, memory + CRM
    dispatch, callback index, suppression, WordPress draft, retry queue,
    buying-signal enroll, audit/event ledgers, session monitor.

    Runs either inline (fast-ACK disabled) or as a detached background task
    after the webhook has already ACKed Vapi (fast-ACK enabled). Every step is
    best-effort and must not raise — the caller may be a fire-and-forget task.
    """
    msg = event.message
    lead = _extract_lead_summary(msg.structuredData)
    # Vapi outbound dialer threads ``metadata.prospect_id`` onto the call so
    # the webhook can correlate back to the source row. May be missing for
    # inbound calls or manual Vapi-dashboard tests — empty string is fine.
    raw_metadata = raw_call.get("metadata") if isinstance(raw_call.get("metadata"), dict) else {}
    prospect_id = str(raw_metadata.get("prospect_id") or "")

    dispatched_ok = False
    dispatch_err: str | None = None
    if lead is not None and call_id:
        payload = _memory_dispatch_payload(
            call_id,
            lead,
            {
                "endedReason": msg.endedReason,
                "summary": msg.summary,
                "recordingUrl": msg.recordingUrl,
            },
        )
        dispatched_ok, dispatch_err = await _post_to_memory(payload)
    elif lead is None:
        dispatch_err = "no_lead_summary_in_structured_data"
    elif not call_id:
        dispatch_err = "no_call_id_in_event"

    # CRM dispatch — parallel to memory, best-effort. Fire when we have a
    # call_id even without a lead_summary so the transcript still lands as
    # a Conversation row (lead_summary becomes the empty structured_data).
    crm_ok = False
    crm_err: str | None = None
    if call_id:
        crm_webhook_msg = {
            "endedReason": msg.endedReason,
            "summary": msg.summary,
            "recordingUrl": msg.recordingUrl,
            "transcript": msg.transcript,
        }
        try:
            crm_ok, crm_err = await _dispatch_to_crm(
                call_id,
                lead,
                prospect_id,
                crm_webhook_msg,
            )
        except Exception as exc:  # noqa: BLE001 — defensive, _dispatch_to_crm catches internally
            _LOG.warning("crm dispatch unexpected error: %s", exc)
            crm_ok, crm_err = False, f"crm_dispatch_unexpected: {exc}"
    else:
        crm_err = "no_call_id_in_event"

    # Callback index — write phone -> prospect context so the inbound
    # Receptionist can surface who's calling back. Best-effort; never
    # blocks or fails the webhook. Skipped when no prospect phone in metadata.
    prospect_phone = str(raw_metadata.get("prospect_phone") or "")
    if prospect_phone and call_id:
        from backend.common.dates import iso_now as _iso_now

        index_outbound_call(
            prospect_phone=prospect_phone,
            prospect_id=prospect_id,
            company_name=getattr(lead, "company", None) or "" if lead else "",
            owner_name=str(raw_metadata.get("owner_name") or ""),
            offer_summary=str(raw_metadata.get("offer_summary") or ""),
            last_intent_score=getattr(lead, "intent_score", None) if lead else None,
            outbound_number_id=str(raw_metadata.get("phone_number_id") or ""),
            last_call_ts=_iso_now(),
        )

    # Enrich the callback lookup record with post-call data if available.
    if prospect_phone and (msg.endedReason or msg.summary):
        from .callback_lookup import enrich_after_call

        enrich_after_call(
            prospect_phone=prospect_phone,
            ended_reason=msg.endedReason or "",
            call_summary=(msg.summary or "")[:500],
        )

    # DNC write: if Morgan disqualified the prospect, add to samus_suppression
    if lead and getattr(lead, "recommended_action", None) == "disqualify" and prospect_phone:
        _write_suppression(prospect_phone, prospect_id, call_id)

    # Product page draft: if Morgan flagged a validated offer, submit it to
    # WordPress as a draft for Alex to review and publish. Best-effort —
    # a failure here never blocks the webhook response.
    if lead and lead.validated_product_name:
        call_context = (
            f"{lead.company or 'prospect'} | score={lead.intent_score} | call_id={call_id}"
        )
        try:
            result = submit_product_page(
                product_name=lead.validated_product_name,
                description=lead.validated_product_description or "",
                price_usd=lead.validated_product_price or "Contact for pricing",
                key_deliverables=lead.validated_product_deliverables,
                call_context=call_context,
            )
            status = result.get("status", "")
            if status == "skipped_existing_sku":
                _LOG.info("wordpress_draft skipped (existing SKU): %s", result.get("slug"))
            else:
                _LOG.info("wordpress_draft: %s", result.get("message", status))
        except Exception as _wp_exc:  # noqa: BLE001
            _LOG.warning("wordpress_draft failed (non-fatal): %s", _wp_exc)

    # Retry queue: no-answer/voicemail prospects get a second chance tomorrow —
    # UNLESS this was a no-callback call. Operator directive (ADR-018): governed
    # autonomous B2B calls are one-and-done, no scheduled re-dials. A call tagged
    # metadata.no_callback (set by the governed dial_fn) or the global
    # SAMUS_VOICE_NO_CALLBACK_ENABLED flag skips the retry enqueue entirely.
    _no_callback = str(raw_metadata.get("no_callback", "")).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ) or (
        os.getenv("SAMUS_VOICE_NO_CALLBACK_ENABLED", "").strip().lower()
        in (
            "1",
            "true",
            "yes",
            "on",
        )
    )
    from .retry import add_retry_entry, NO_ANSWER_REASONS

    ended_reason = msg.endedReason or ""
    if not _no_callback and ended_reason in NO_ANSWER_REASONS and prospect_phone:
        add_retry_entry(
            prospect_id=prospect_id,
            prospect_phone=prospect_phone,
            company_name=getattr(lead, "company", None)
            or str(raw_metadata.get("company_name", "")),
            priority=str(raw_metadata.get("priority", "warm")),
            run_id=str(raw_metadata.get("run_id", "")),
            ended_reason=ended_reason,
            ts=iso_now(),
        )

    # Buying-signal email route (wired-DORMANT): if the call surfaced real intent
    # and OUTREACH_BUYING_SIGNAL_ROUTE_ENABLED is armed, enroll the prospect into
    # the warm follow-up sequence instead of the cold path. Best-effort + gated
    # internally; never fails the webhook. Sending is a separate dormant step.
    if lead and prospect_id:
        try:
            from backend.outreach.buying_signal_route import maybe_enroll_buying_signal

            maybe_enroll_buying_signal(
                prospect_id=prospect_id,
                lead=lead,
                now_iso=iso_now(),
                email=(getattr(lead, "contact_offered", None) or ""),
                company=getattr(lead, "company", None) or "",
                call_id=call_id or "",
            )
        except Exception as _bsr_exc:  # noqa: BLE001 — best-effort, never block
            _LOG.warning("buying_signal enroll failed (non-fatal): %s", _bsr_exc)

    # Post-call remediate -> next-touch loop (wired-DORMANT): when
    # SAMUS_POSTCALL_REMEDIATE_ENABLED is armed AND this call is a positive/
    # interested outcome, deliver the SEO remediation deliverable (backend.fulfill
    # seam) + enqueue a follow-up touch (buying_signal enroll seam). Best-effort +
    # fail-soft internally; never initiates a dial. Flag OFF -> pure no-op.
    _maybe_postcall_remediate(lead, prospect_id, call_id, raw_metadata, settings)

    ev = events.build_audit_event(
        service="voice",
        task_id=call_id or "unknown_call",
        action="webhook_end_of_call",
        input_payload={
            "call_id": call_id,
            "prospect_id": prospect_id,
            "ended_reason": msg.endedReason,
            "has_summary": bool(msg.summary),
            "has_structured_data": bool(msg.structuredData),
            "tier": getattr(lead, "tier", None) if lead else None,
            "intent_score": getattr(lead, "intent_score", None) if lead else None,
        },
        output_payload={
            "memory_dispatch_ok": dispatched_ok,
            "memory_dispatch_error": dispatch_err,
            "crm_dispatch_ok": crm_ok,
            "crm_dispatch_error": crm_err,
            "recommended_action": getattr(lead, "recommended_action", None) if lead else None,
        },
        status="completed" if (dispatched_ok and crm_ok) else "degraded",
    )
    _append_audit(ev)
    # Rich-payload row for the briefing rollup — audit row hashes the fields
    # we need to aggregate (tier/intent_score/recommended_action), so we keep
    # an unhashed copy here.
    outbound_number_id = str(raw_metadata.get("phone_number_id") or "") or str(
        raw_call.get("phoneNumberId") or ""
    )
    _append_event(
        {
            "ts": iso_now(),
            "kind": "end_of_call",
            "call_id": call_id,
            "ended_reason": msg.endedReason,
            "company": getattr(lead, "company", None) if lead else None,
            "tier": getattr(lead, "tier", None) if lead else None,
            "intent_score": getattr(lead, "intent_score", None) if lead else None,
            "recommended_action": getattr(lead, "recommended_action", None) if lead else None,
            "memory_dispatch_ok": dispatched_ok,
            "memory_dispatch_error": dispatch_err,
            "crm_dispatch_ok": crm_ok,
            # Without the error string a dispatch failure is indistinguishable
            # from a skip — the 7/02 crm_url_unset outage sat invisible for a
            # full production day because only the boolean landed in the ledger.
            "crm_dispatch_error": crm_err,
            "outbound_number_id": outbound_number_id,
            "vapi_cost": raw_call.get("cost"),
            "duration_seconds": msg.durationSeconds,
        }
    )

    # Unified business-event ledger (HOTL Tranche 1) — a call.answered row for
    # every outbound end-of-call that a human actually answered. "Answered"
    # mirrors the briefing rollup's definition: endedReason NOT in the
    # voicemail / no-answer family. Fail-soft by contract: emit_business_event
    # never raises.
    _no_answer_reasons = frozenset(
        {
            "no-answer",
            "voicemail",
            "machine_detected_silence",
            "machine_end_beep",
            "machine_end_silence",
            "machine_end_other",
        }
    )
    if (ended_reason or "").lower() not in _no_answer_reasons:
        from backend.common.business_events import CALL_ANSWERED, emit_business_event

        emit_business_event(
            CALL_ANSWERED,
            workcell="voice",
            prospect_id=(prospect_id or None),
            cost_usd=(
                float(raw_call["cost"]) if isinstance(raw_call.get("cost"), (int, float)) else None
            ),
            metadata={
                "call_id": call_id or "",
                "ended_reason": ended_reason or "",
                "duration_seconds": msg.durationSeconds,
                "tier": getattr(lead, "tier", None) if lead else None,
                "intent_score": getattr(lead, "intent_score", None) if lead else None,
            },
        )

    # ── Intraday session monitor (DORMANT: SAMUS_VOICE_SESSION_MONITOR=1 to arm)
    try:
        from .call_session_monitor import check_session

        adj = check_session()
        if adj:
            _LOG.info(
                "session_monitor: mid-session adjustment fired — %s (%d recs)",
                adj.trigger,
                len(adj.recommendations),
            )
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("session_monitor check skipped: %s", exc)

    return WebhookResult(
        received=True,
        message_type=msg.type,
        call_id=call_id,
        lead_summary=lead,
        memory_dispatch_ok=dispatched_ok,
        memory_dispatch_error=dispatch_err,
        crm_dispatch_ok=crm_ok,
        crm_dispatch_error=crm_err,
    )
