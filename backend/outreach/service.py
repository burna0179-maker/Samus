"""Outreach service orchestrator — FSM advance + outcome logging."""
from __future__ import annotations

import datetime as _dt
import logging
import os

from backend.common import events, persistence
from backend.common.config import get_settings
from backend.common.dates import iso_now
from backend.common.http_client import signed_post_json_sync
from backend.common.idempotency import GLOBAL_IDEMPOTENCY_STORE

from . import fsm, metrics, ses_adapter
from .models import (
    OutreachAdvanceRequest,
    OutreachLogRequest,
    OutreachMessageRequest,
    OutreachMetricsSnapshot,
    OutreachOutcome,
    OutreachStep,
)

_LOG = logging.getLogger("samus.outreach.service")

_AUDIT_PATH_DEFAULT = "/opt/samus/data/outreach/outreach_audit.jsonl"

# A sent outreach message surfaces as a follow-up call this many days later —
# see crm.service.list_follow_ups_due + the morning brief's follow-up lane.
_FOLLOW_UP_DELAY_DAYS = 2


def _audit_ledger() -> persistence.JsonlLedger:
    return persistence.JsonlLedger(
        os.getenv("SAMUS_OUTREACH_AUDIT_PATH", _AUDIT_PATH_DEFAULT),
    )


def advance_call(req: OutreachAdvanceRequest) -> OutreachStep:
    """Run one FSM tick — returns the next state + action the caller should perform."""
    cache_key = f"outreach.advance:{req.prospect_id}:{req.current_state}"
    cached = GLOBAL_IDEMPOTENCY_STORE.get(cache_key)
    if cached is not None and isinstance(cached, dict):
        _LOG.info("advance_call cache hit prospect=%s state=%s",
                  req.prospect_id, req.current_state)
        return OutreachStep.model_validate(cached)

    context = {
        "objection": req.objection_detected,
        "resistance": "not interested" in (req.user_input or "").lower(),
    }
    nxt = fsm.next_state(req.current_state, context)
    action = fsm.decide_action(req.current_state, req.intel, req.objection_response)

    products = req.intel.products or {}
    step = OutreachStep(
        current_state=req.current_state,
        next_state=nxt,
        action=action,
        primary_product=products.get("primary", ""),
        secondary_product=products.get("secondary"),
    )

    ev = events.build_audit_event(
        service="outreach",
        task_id=req.prospect_id,
        action="advance_call",
        input_payload=req.model_dump(),
        output_payload=step.model_dump(),
        status="completed",
    )
    try:
        _audit_ledger().append(ev)
    except OSError as exc:
        _LOG.warning("outreach audit append failed: %s", exc)

    GLOBAL_IDEMPOTENCY_STORE.set(cache_key, step.model_dump())
    return step


def log_outcome(req: OutreachLogRequest) -> OutreachOutcome:
    """Record one call outcome + return a fresh metrics snapshot."""
    metrics.log_interaction(
        prospect_id=req.prospect_id,
        outcome=req.outcome,
        objection=req.objection,
        product=req.product,
        angle=req.angle,
    )
    snap = metrics.snapshot()
    outcome = OutreachOutcome(
        prospect_id=req.prospect_id,
        outcome=req.outcome,
        ts=iso_now(),
        metrics_snapshot=snap,
    )
    ev = events.build_audit_event(
        service="outreach",
        task_id=req.prospect_id,
        action="log_outcome",
        input_payload=req.model_dump(),
        output_payload={"outcome": req.outcome},
        status="completed",
    )
    try:
        _audit_ledger().append(ev)
    except OSError as exc:
        _LOG.warning("outreach audit append failed: %s", exc)
    return outcome


def get_metrics() -> OutreachMetricsSnapshot:
    """Read-only view of aggregate metrics."""
    return OutreachMetricsSnapshot(
        top_objections=metrics.get_top_objections(),
        best_products=metrics.get_best_products(),
        angle_performance=metrics.get_angle_performance(),
    )


def _dispatch_outreach_to_crm(req: OutreachMessageRequest, sent_ts: str) -> None:
    """Record a sent outreach message in the CRM so it enters the follow-up
    cadence: a Conversation (the touch, ``outcome='outreach_sent'``) plus a
    CallState moved to ``outreach_sent`` with ``next_attempt_at`` scheduled
    ``_FOLLOW_UP_DELAY_DAYS`` out. The morning brief reads that CallState to
    surface the prospect as a follow-up call — see
    ``crm.service.list_follow_ups_due``.

    Best-effort: a config gap or CRM / network failure is logged and swallowed.
    The message has already been sent + audited; a downstream bookkeeping miss
    must not undo that or fail the caller.
    """
    settings = get_settings()
    crm_url = settings.gateway_urls.get("crm")
    if not crm_url or not settings.shared_hmac_key:
        _LOG.debug("outreach crm dispatch skipped: crm_url / shared_hmac_key unset")
        return

    sent_date = (sent_ts or "")[:10]
    try:
        follow_up_on = (
            _dt.date.fromisoformat(sent_date)
            + _dt.timedelta(days=_FOLLOW_UP_DELAY_DAYS)
        ).isoformat()
    except ValueError:
        _LOG.warning("outreach crm dispatch skipped: unparseable sent_ts %r", sent_ts)
        return

    conversation = {
        "conversation_id": f"cv_outreach_{req.prospect_id}_{sent_date}",
        "prospect_id": req.prospect_id,
        "channel": req.channel,
        "status": "completed",
        "started_at": sent_ts,
        "ended_at": sent_ts,
        "summary": req.subject or "",
        "outcome": "outreach_sent",
        "source": "outreach",
        "source_ref": req.template_id,
        "structured_data": {
            "company": req.company,
            "phone": req.phone,
            "channel": req.channel,
            "subject": req.subject or "",
            "to": req.to or "",
            "emailed_on": sent_date,
            "follow_up_on": follow_up_on,
        },
    }
    call_state = {
        "prospect_id": req.prospect_id,
        "state": "outreach_sent",
        "next_attempt_at": follow_up_on,
        "last_outcome": f"outreach {req.channel} sent",
        "updated_at": sent_ts,
    }
    try:
        for path, payload in (
            ("/crm/conversations", conversation),
            (f"/crm/call-state/{req.prospect_id}", call_state),
        ):
            resp = signed_post_json_sync(crm_url, path, payload, retries=2)
            if resp.status_code >= 400:
                _LOG.warning("outreach crm dispatch %s -> HTTP %s",
                             path, resp.status_code)
    except Exception as exc:  # noqa: BLE001 — best-effort bookkeeping
        _LOG.warning("outreach crm dispatch failed prospect=%s: %s",
                     req.prospect_id, exc)


class HarmSuppressedSend(RuntimeError):
    """A send blocked by real-time harm suppression (complaint/unsubscribe).

    Raised by :func:`send_message` BEFORE any channel dispatch. Existing
    callers already wrap sends in try/except (the cash-engine live-send seam,
    the worker), so a suppression surfaces exactly like a failed send —
    except nothing left the building.
    """

    def __init__(self, reason: str, signals: dict | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.signals = dict(signals or {})


# Durable daily-counter key for the constitutional send cap (HOTL T5).
_SEND_CAP_COUNTER_KEY = "outreach.sends"


class SendCapExceeded(RuntimeError):
    """A send blocked because the hard daily send cap was already reached.

    Constitutional constraint (``SAMUS_MAX_SENDS_PER_DAY``): raised by
    :func:`send_message` BEFORE any channel dispatch once today's send count is
    at or above the cap. Surfaces exactly like a failed send to existing
    try/except callers — nothing left the building.
    """

    def __init__(self, reason: str, *, sent_today: int, cap: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.sent_today = int(sent_today)
        self.cap = int(cap)


def _check_send_cap(req: OutreachMessageRequest) -> None:
    """Constitutional daily send cap — runs before every MESSAGING send.

    Scoped to the messaging channels (email); the call/voicemail path has its
    own independent ceiling (``SAMUS_MAX_CALLS_PER_DAY``) enforced in
    ``voice.service.initiate_call``, so the two caps never cross-gate each
    other. Reads the durable per-day counter (survives worker restarts, unlike
    the in-process idempotency store). At/over
    ``settings.samus_max_sends_per_day`` -> record + escalate + raise
    :class:`SendCapExceeded`. Fail-OPEN on any unexpected error in the check
    itself (a counter hiccup must not wedge every send), but the cap read is the
    load-bearing floor.
    """
    # Call/voicemail volume is governed by the call cap, not the send cap.
    if req.channel not in ("email",):
        return
    try:
        from backend.common import daily_counter

        cap = int(getattr(get_settings(), "samus_max_sends_per_day", 15))
        if cap <= 0:
            return
        already = daily_counter.count_today(_SEND_CAP_COUNTER_KEY)
        if already >= cap:
            _block_capped_send(req, sent_today=already, cap=cap)
    except SendCapExceeded:
        raise
    except Exception as exc:  # noqa: BLE001 — a counter fault never wedges sends
        _LOG.warning("send-cap check failed (allowing send): %s", exc)


def _block_capped_send(req: OutreachMessageRequest, *, sent_today: int, cap: int) -> None:
    """Record + escalate a cap-blocked send, then raise. Never sends."""
    why = (
        f"daily send cap reached: {sent_today} sends today >= cap {cap}; "
        f"{req.channel} send to prospect {req.prospect_id} blocked"
    )
    # 1. decision.made on the unified stream (fail-soft by contract).
    from backend.common.business_events import DECISION_MADE, emit_business_event
    emit_business_event(
        DECISION_MADE,
        workcell="outreach",
        prospect_id=req.prospect_id,
        campaign_id=(getattr(req, "campaign_id", "") or None),
        metadata={
            "decision": "send_cap_blocked",
            "why": why,
            "sent_today": sent_today,
            "cap": cap,
        },
    )
    # 2. Operator task via the existing CRM path (best-effort).
    try:
        from backend.crm import service as crm
        from backend.crm.models import CreateOperatorTaskRequest

        crm.create_operator_task(CreateOperatorTaskRequest(
            kind="review",
            title=f"Daily send cap reached ({sent_today}/{cap})",
            description=why,
            source="outreach_send_cap",
            source_ref=req.prospect_id,
        ))
    except Exception as exc:  # noqa: BLE001 — the block itself is the deliverable
        _LOG.warning("send-cap operator-task creation failed: %s", exc)
    _LOG.warning("%s", why)
    raise SendCapExceeded(why, sent_today=sent_today, cap=cap)


def _check_harm_suppression(req: OutreachMessageRequest) -> dict | None:
    """HOTL Tranche 1 real-time harm gate — runs before EVERY send.

    Reads :mod:`backend.strategy.harm_signals` for the target's opportunity:
    >=1 SES complaint or unsubscribe -> return a block dict. The signal
    lookups themselves fail-OPEN (a store hiccup never blocks legitimate
    sends), matching harm_signals' own contract.
    """
    try:
        from backend.crm import service as crm
        from backend.strategy import harm_signals

        opp = crm.get_opportunity_for_prospect(req.prospect_id)
        if opp is None:
            return None
        opportunity_id = str(getattr(opp, "opportunity_id", "") or "")
        if not opportunity_id:
            return None
        complaints = harm_signals.complaints_for(opportunity_id)
        unsubscribes = harm_signals.unsubscribes_for(opportunity_id)
        if complaints or unsubscribes:
            return {
                "opportunity_id": opportunity_id,
                "complaints": int(complaints),
                "unsubscribes": int(unsubscribes),
            }
        return None
    except Exception as exc:  # noqa: BLE001 — harm check must not break sends
        _LOG.warning("harm suppression check failed (allowing send): %s", exc)
        return None


def _block_harmful_send(req: OutreachMessageRequest, signals: dict) -> None:
    """Record + escalate a harm-suppressed send, then raise. Never sends."""
    why = (
        f"harm suppression: prospect {req.prospect_id} has "
        f"{signals['complaints']} complaint(s) + "
        f"{signals['unsubscribes']} unsubscribe(s); {req.channel} send blocked"
    )
    # 1. decision.made on the unified stream (fail-soft by contract).
    from backend.common.business_events import DECISION_MADE, emit_business_event
    emit_business_event(
        DECISION_MADE,
        workcell="outreach",
        prospect_id=req.prospect_id,
        opportunity_id=signals.get("opportunity_id"),
        campaign_id=(getattr(req, "campaign_id", "") or None),
        metadata={"decision": "harm_suppressed_send", "why": why, **signals},
    )
    # 2. Operator task via the existing CRM path (best-effort).
    try:
        from backend.crm import service as crm
        from backend.crm.models import CreateOperatorTaskRequest

        crm.create_operator_task(CreateOperatorTaskRequest(
            kind="review",
            title=f"Harm-suppressed outreach to prospect {req.prospect_id}",
            description=why,
            related_entity_kind="opportunity",
            related_entity_id=str(signals.get("opportunity_id") or ""),
            source="outreach_harm_suppression",
            source_ref=req.prospect_id,
        ))
    except Exception as exc:  # noqa: BLE001 — the block itself is the deliverable
        _LOG.warning("harm suppression operator-task creation failed: %s", exc)
    # 3. Audit row on the outreach ledger (best-effort).
    ev = events.build_audit_event(
        service="outreach",
        task_id=req.prospect_id,
        action="send_message",
        input_payload=req.model_dump(exclude={"body"}),
        output_payload={"blocked": True, "why": why, **signals},
        status="failed",
    )
    try:
        _audit_ledger().append(ev)
    except OSError as exc:
        _LOG.warning("outreach audit append failed: %s", exc)
    _LOG.warning("%s", why)
    raise HarmSuppressedSend(why, signals)


def send_message(req: OutreachMessageRequest) -> dict:
    """Dispatch an outbound message. Returns {message_id, channel, to, ts}.

    email -> routed through ``backend.common.email_backend.send_email`` which
    selects SES or SendGrid via ``settings.email_backend``. The cash-engine
    cold tripwire + the live retainer pitches both run through this seam.
    call/voicemail -> backend.voice workcell (Vapi). sms -> deferred. Appends
    an audit event on both success and failure.

    HOTL Tranche 1: every send is preceded by the real-time harm gate — a
    prospect with >=1 complaint or unsubscribe raises
    :class:`HarmSuppressedSend` before any channel dispatch.
    """
    harm = _check_harm_suppression(req)
    if harm is not None:
        _block_harmful_send(req, harm)

    # Constitutional daily send cap (HOTL T5) — hard block once today's count
    # is at/over the ceiling. Runs before any channel dispatch.
    _check_send_cap(req)

    if req.channel == "email":
        from backend.common.email_backend import EmailBackendError, send_email
        # custom_args are echoed back on every SendGrid event so the Heat Field
        # / attribution can correlate a delivery/bounce/complaint to this
        # prospect + template (sg_message_id -> deal).
        _custom_args = {
            "prospect_id": req.prospect_id,
            "template_id": req.template_id,
        }
        try:
            result = send_email(
                req.to, req.subject, req.body, custom_args=_custom_args,
                # Forward the rich HTML flyer as the text/html multipart part
                # when the caller supplied one (cash_engine + morning_batch both
                # now attach the vibrant flyer). Empty -> text-only, unchanged.
                html_body=(req.html_body or None),
            )
        except EmailBackendError as exc:
            ev = events.build_audit_event(
                service="outreach",
                task_id=req.prospect_id,
                action="send_message",
                input_payload=req.model_dump(exclude={"body"}),
                output_payload={"error": str(exc)},
                status="failed",
            )
            try:
                _audit_ledger().append(ev)
            except OSError as oerr:
                _LOG.warning("outreach audit append failed: %s", oerr)
            raise

        ev = events.build_audit_event(
            service="outreach",
            task_id=req.prospect_id,
            action="send_message",
            input_payload=req.model_dump(exclude={"body"}),
            output_payload=result,
            status="completed",
        )
        try:
            _audit_ledger().append(ev)
        except OSError as exc:
            _LOG.warning("outreach audit append failed: %s", exc)
        # Record the sent message in the CRM so the prospect enters the
        # daily-calls follow-up cadence. Best-effort — never fails the send.
        _dispatch_outreach_to_crm(req, str(result.get("ts") or iso_now()))
        # Tick the Heat Field "emails sent today" counter — the outbound-volume
        # ground truth nothing tracked before. Best-effort, never fails a send.
        try:
            from backend.heat import service as heat_service
            heat_service.record_send(1)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("heat record_send failed: %s", exc)
        # Append the send-ramp ledger row — the warmup-volume accounting the
        # ramp reads (B-6). Record-only: this reflects a send that already
        # succeeded, it does NOT gate/throttle. Best-effort, never fails a send.
        try:
            from backend.common import send_ramp
            send_ramp.record_send(to=req.to or "")
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("send_ramp record_send failed: %s", exc)
        # Tick the durable constitutional send-cap counter for this real send
        # (HOTL T5). Best-effort — a counter fault never fails a completed send.
        try:
            from backend.common import daily_counter
            daily_counter.increment(_SEND_CAP_COUNTER_KEY)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("send-cap counter increment failed: %s", exc)
        # Unified business-event ledger (HOTL Tranche 1) — one email.sent row
        # per successful send, with campaign attribution when the request
        # carries it. Fail-soft by contract: emit_business_event never raises.
        from backend.common.business_events import EMAIL_SENT, emit_business_event
        emit_business_event(
            EMAIL_SENT,
            workcell="outreach",
            prospect_id=req.prospect_id,
            campaign_id=(getattr(req, "campaign_id", "") or None),
            variant_arm_id=(getattr(req, "variant_arm_id", "") or None),
            metadata={
                "template_id": req.template_id,
                "to_tail": (req.to or "")[-12:],
                "message_id": str(result.get("message_id") or ""),
            },
        )
        # Reward Samus's karma for a clean send (builds the autonomy track
        # record; accrues even while the karma gate is dormant). Best-effort.
        try:
            from backend.governance.karma import engine as karma
            karma.apply_outcome("send_success", ref=req.prospect_id)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("karma send_success failed: %s", exc)
        return result

    if req.channel in ("call", "voicemail"):
        return _send_voice_message(req)

    raise NotImplementedError(
        f"outreach.send_message channel='{req.channel}' not yet wired."
    )


def _send_voice_message(req: OutreachMessageRequest) -> dict:
    """Place an outbound call/voicemail through the real Vapi provider.

    The voice workcell (:mod:`backend.voice`) owns the Vapi REST client; this
    delegates to its ``initiate_call`` rather than re-implementing a second Vapi
    path. Config-gated and fail-closed:

      * ``outreach_voice_send_enabled`` must be truthy (default OFF) — without it
        an outbound dial never fires from the outreach path.
      * a destination number (``req.phone``) and the configured default
        ``vapi_assistant_id`` + ``vapi_phone_number_id`` must all be present.
      * if any of those (or the Vapi API key) is unset, returns a structured
        ``status="degraded"`` receipt — never a silent success, never a raise
        that masks an unconfigured provider as a code defect.

    Audits both the attempt and the outcome, mirroring the email branch.
    """
    settings = get_settings()
    enabled = bool(getattr(settings, "outreach_voice_send_enabled", False))
    to_number = (req.to or req.phone or "").strip()
    assistant_id = (getattr(settings, "vapi_assistant_id", "") or "").strip()
    phone_number_id = (getattr(settings, "vapi_phone_number_id", "") or "").strip()

    def _degraded(reason: str) -> dict:
        ev = events.build_audit_event(
            service="outreach",
            task_id=req.prospect_id,
            action="send_message",
            input_payload=req.model_dump(exclude={"body"}),
            output_payload={"voice_skip_reason": reason},
            status="degraded",
        )
        try:
            _audit_ledger().append(ev)
        except OSError as oerr:
            _LOG.warning("outreach audit append failed: %s", oerr)
        _LOG.info("outreach voice send skipped prospect=%s reason=%s",
                  req.prospect_id, reason)
        return {
            "message_id": "",
            "channel": req.channel,
            "to": to_number,
            "ts": iso_now(),
            "status": "degraded",
            "voice_skip_reason": reason,
        }

    if not enabled:
        return _degraded("outreach_voice_send_disabled")
    if not to_number:
        return _degraded("no_destination_number")
    if not (assistant_id and phone_number_id):
        return _degraded("vapi_assistant_or_phone_unset")

    # Real provider call through the voice workcell (single Vapi path).
    from backend.voice import service as voice_service
    from backend.voice.models import InitiateCallRequest

    result = voice_service.initiate_call(InitiateCallRequest(
        assistant_id=assistant_id,
        phone_number_id=phone_number_id,
        customer_number=to_number,
        customer_name=(req.company or None),
        metadata={
            "source": "outreach.send_message",
            "prospect_id": req.prospect_id,
            "template_id": req.template_id,
            "channel": req.channel,
        },
    ))

    if result.vapi_error:
        ev = events.build_audit_event(
            service="outreach",
            task_id=req.prospect_id,
            action="send_message",
            input_payload=req.model_dump(exclude={"body"}),
            output_payload={"vapi_error": result.vapi_error},
            status="failed",
        )
        try:
            _audit_ledger().append(ev)
        except OSError as oerr:
            _LOG.warning("outreach audit append failed: %s", oerr)
        return {
            "message_id": "",
            "channel": req.channel,
            "to": to_number,
            "ts": iso_now(),
            "status": "failed",
            "vapi_error": result.vapi_error,
        }

    out = {
        "message_id": result.call_id,
        "channel": req.channel,
        "to": to_number,
        "ts": iso_now(),
        "status": result.status or "queued",
    }
    ev = events.build_audit_event(
        service="outreach",
        task_id=req.prospect_id,
        action="send_message",
        input_payload=req.model_dump(exclude={"body"}),
        output_payload=out,
        status="completed",
    )
    try:
        _audit_ledger().append(ev)
    except OSError as oerr:
        _LOG.warning("outreach audit append failed: %s", oerr)
    return out
