"""AI Digital Receptionist — inbound end-of-call handling + webhook routing."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.voice import inbound, service
from backend.voice.inbound import (
    _classify_outcome,
    _duration_seconds,
    _extract_inbound_summary,
    _was_answered,
)
from backend.voice.models import InboundSummary, ReceptionistConfig, VapiWebhookEvent


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_extract_inbound_summary_nested_and_root():
    nested = _extract_inbound_summary({"inbound_summary": {"caller_name": "Jane"}})
    assert nested.caller_name == "Jane"
    root = _extract_inbound_summary({"appointment_requested": True})
    assert root.appointment_requested is True
    assert isinstance(_extract_inbound_summary(None), InboundSummary)


@pytest.mark.parametrize("reason,answered", [
    ("customer-ended-call", True),
    ("assistant-ended-call", True),
    ("", True),
    ("no-answer", False),
    ("twilio-failed-to-connect", False),
    ("pipeline-error", False),
])
def test_was_answered(reason, answered):
    assert _was_answered(reason) is answered


def test_duration_seconds_prefers_explicit_then_falls_back():
    assert _duration_seconds(190.4, {}) == 190
    assert _duration_seconds(None, {
        "startedAt": "2026-05-20T10:00:00Z",
        "endedAt": "2026-05-20T10:02:30Z",
    }) == 150
    assert _duration_seconds(None, {}) == 0


def test_classify_outcome():
    appt = InboundSummary(appointment_requested=True)
    assert _classify_outcome(appt, answered=True, voicemail_left=False) == "appointment_requested"
    cb = InboundSummary(callback_requested=True)
    assert _classify_outcome(cb, answered=True, voicemail_left=False) == "callback_requested"
    assert _classify_outcome(InboundSummary(), answered=False, voicemail_left=False) == "missed"
    assert _classify_outcome(InboundSummary(), answered=True, voicemail_left=True) == "voicemail"
    assert _classify_outcome(InboundSummary(), answered=True, voicemail_left=False) == "handled"


# ---------------------------------------------------------------------------
# handle_inbound_end_of_call
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class _SettingsStub:
    gateway_urls = {"crm": "http://crm:8080", "finance": "http://finance:8080"}
    shared_hmac_key = "test-hmac-key"


def _inbound_event(*, call_id="call_in_1", structured=None,
                   duration=190.0, ended="customer-ended-call") -> VapiWebhookEvent:
    return VapiWebhookEvent.model_validate({
        "message": {
            "type": "end-of-call-report",
            "call": {
                "id": call_id,
                "type": "inboundPhoneCall",
                "phoneNumberId": "pn_acme",
                "customer": {"number": "+14155550100"},
                "startedAt": "2026-05-20T10:00:00.000Z",
                "endedAt": "2026-05-20T10:03:10.000Z",
            },
            "transcript": "Caller: I'd like to book a cleaning.",
            "summary": "Caller asked to book an appointment.",
            "endedReason": ended,
            "durationSeconds": duration,
            "structuredData": {"inbound_summary": structured or {}},
        },
    })


@pytest.fixture
def patched_dispatch(tmp_path, monkeypatch):
    """Route storage to tmp, capture every signed CRM/finance POST."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    posts: list[dict] = []

    async def _fake_signed_post(url, path, payload, retries=2):
        posts.append({"url": url, "path": path, "payload": payload})
        return _FakeResp(200)

    monkeypatch.setattr(inbound, "signed_post_json", _fake_signed_post)
    monkeypatch.setattr(inbound, "get_settings", lambda: _SettingsStub())
    return posts, tmp_path


def test_handle_inbound_appointment_request(patched_dispatch):
    posts, tmp_path = patched_dispatch
    cfg = ReceptionistConfig(
        customer_slug="acme", business_name="Acme Plumbing",
        phone_numbers=["+15305551212"], vapi_phone_number_id="pn_acme",
    )
    event = _inbound_event(structured={
        "caller_name": "Jane", "reason_for_call": "book a cleaning",
        "appointment_requested": True, "appointment_details": "Tuesday morning",
    })

    outcome = asyncio.run(inbound.handle_inbound_end_of_call(event, cfg))

    assert outcome.outcome == "appointment_requested"
    assert outcome.crm_ok is True
    assert outcome.tasks_created == 1

    # An inbound Conversation row went to CRM.
    conv = next(p for p in posts if p["path"] == "/crm/conversations")
    assert conv["payload"]["direction"] == "inbound"
    assert conv["payload"]["customer_id"] == "acme"
    assert conv["payload"]["conversation_id"] == "cv_vapi_call_in_1"
    assert conv["payload"]["caller_number"] == "+14155550100"

    # An appointment OperatorTask was opened.
    task = next(p for p in posts if p["path"] == "/crm/operator-tasks")
    assert task["payload"]["kind"] == "schedule"

    # call.json was persisted to the per-client artifact tree.
    call_json = Path(tmp_path) / "customers" / "acme" / "calls" / "call_in_1" / "call.json"
    assert call_json.is_file()
    rec = json.loads(call_json.read_text(encoding="utf-8"))
    assert rec["direction"] == "inbound" and rec["duration_sec"] == 190


def test_handle_inbound_reports_usage_when_stripe_customer_set(patched_dispatch):
    posts, _ = patched_dispatch
    cfg = ReceptionistConfig(
        customer_slug="acme", vapi_phone_number_id="pn_acme",
        stripe_customer_id="cus_acme",
    )
    outcome = asyncio.run(
        inbound.handle_inbound_end_of_call(_inbound_event(), cfg),
    )
    meter = next(p for p in posts if p["path"] == "/meter-event")
    assert meter["payload"]["stripe_customer_id"] == "cus_acme"
    assert meter["payload"]["call_id"] == "call_in_1"
    assert outcome.metered_ok is True


def test_handle_inbound_skips_metering_without_stripe_customer(patched_dispatch):
    posts, _ = patched_dispatch
    cfg = ReceptionistConfig(customer_slug="acme", vapi_phone_number_id="pn_acme")
    outcome = asyncio.run(
        inbound.handle_inbound_end_of_call(_inbound_event(), cfg),
    )
    assert not any(p["path"] == "/meter-event" for p in posts)
    # Deliberate no-op, not a failure.
    assert outcome.metered_ok is True


def test_handle_inbound_disabled_metering_dispatches_no_meter_event(patched_dispatch):
    """FIN-08: a config the loader flagged with an unconfirmed stripe_customer_id
    must NOT dispatch a meter event — fail-closed, never bill the wrong customer.
    The call is still logged + CRM-written; only the billing write is refused.
    """
    posts, tmp_path = patched_dispatch
    cfg = ReceptionistConfig(
        customer_slug="acme", vapi_phone_number_id="pn_acme",
        stripe_customer_id="cus_TYPO",          # would have billed this id
        metering_disabled=True,                  # loader couldn't confirm it
        metering_disabled_reason="stripe_customer_not_found",
    )
    outcome = asyncio.run(
        inbound.handle_inbound_end_of_call(_inbound_event(), cfg),
    )
    # NO meter event was dispatched (assert via the mock finance dispatcher).
    assert not any(p["path"] == "/meter-event" for p in posts)
    # Fail-closed tuple surfaced on the outcome.
    assert outcome.metered_ok is False
    assert outcome.metering_note == "metering_disabled_invalid_customer"
    # The call was still logged + persisted (CRM Conversation row went out).
    assert any(p["path"] == "/crm/conversations" for p in posts)
    call_json = (Path(tmp_path) / "customers" / "acme" / "calls"
                 / "call_in_1" / "call.json")
    assert call_json.is_file()


def test_report_usage_disabled_returns_failclosed_tuple():
    """Unit: _report_usage returns the fail-closed tuple and reads no settings."""
    cfg = ReceptionistConfig(
        customer_slug="acme", stripe_customer_id="cus_TYPO",
        metering_disabled=True,
        metering_disabled_reason="stripe_customer_not_found",
    )
    from backend.voice.models import InboundCallRecord
    rec = InboundCallRecord(call_id="c1", customer_slug="acme", duration_sec=190)
    ok, note = asyncio.run(inbound._report_usage(cfg, rec))
    assert ok is False
    assert note == "metering_disabled_invalid_customer"


# ---------------------------------------------------------------------------
# Webhook routing — service.handle_webhook_event inbound fork
# ---------------------------------------------------------------------------

def test_is_inbound_call_detection():
    class _S:
        vapi_inbound_phone_number_id = "pn_inbound"
    assert service._is_inbound_call({"type": "inboundPhoneCall"}, _S())
    assert service._is_inbound_call({"phoneNumberId": "pn_inbound"}, _S())
    assert not service._is_inbound_call({"type": "outboundPhoneCall"}, _S())
    assert not service._is_inbound_call({"phoneNumberId": "pn_other"}, _S())


def test_webhook_routes_inbound_to_receptionist(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_VOICE_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH", str(tmp_path / "events.jsonl"))

    cfg = ReceptionistConfig(customer_slug="acme", vapi_phone_number_id="pn_acme")
    monkeypatch.setattr(service, "resolve_customer_for_vapi_number",
                        lambda pid: cfg if pid == "pn_acme" else None)

    captured = {}

    async def _fake_inbound(event, config):
        captured["called"] = True
        return inbound.InboundCallOutcome(
            call_id="call_in_1", customer_slug=config.customer_slug,
            outcome="handled", crm_ok=True,
        )

    monkeypatch.setattr(service.inbound, "handle_inbound_end_of_call", _fake_inbound)

    result = asyncio.run(service.handle_webhook_event(_inbound_event()))
    assert captured.get("called") is True
    assert result.crm_dispatch_ok is True
    assert result.lead_summary is None      # inbound -> no outbound lead summary


def test_webhook_inbound_unmatched_did_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_VOICE_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(service, "resolve_customer_for_vapi_number", lambda pid: None)
    monkeypatch.setattr(service, "resolve_customer_for_number", lambda n: None)

    result = asyncio.run(service.handle_webhook_event(_inbound_event()))
    assert result.received is True
    assert result.memory_dispatch_error == "inbound_no_matching_receptionist_client"
