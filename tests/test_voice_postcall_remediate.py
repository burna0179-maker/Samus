"""Post-call remediate -> next-touch loop (wired-DORMANT).

When SAMUS_POSTCALL_REMEDIATE_ENABLED is armed AND a completed outbound call is
a positive/interested outcome, the end-of-call handler must:
  (a) deliver the SEO remediation deliverable via the backend.fulfill seam, and
  (b) enqueue a next-touch via the buying_signal enroll seam.

Both are best-effort + fail-soft. This loop NEVER initiates a dial.

These tests exercise the loop directly through the SYNCHRONOUS webhook path
(SAMUS_VOICE_WEBHOOK_FAST_ACK=0) so the assertions run before the loop closes,
and mock the two seams so nothing touches the network / SES / Neo4j.
"""

from __future__ import annotations

import asyncio

import pytest


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _settings(*, postcall_enabled: bool):
    class _S:
        pass

    s = _S()
    s.postcall_remediate_enabled = postcall_enabled
    # The pre-existing buying-signal route stays OFF so the ONLY enrollment
    # under test is this loop's (source="postcall_remediate").
    s.outreach_buying_signal_route_enabled = False
    s.outreach_buying_signal_intent_threshold = 70
    # Fields the rest of _process_outbound_end_of_call touches.
    s.vapi_api_key = "vapi_x"
    s.shared_hmac_key = ""  # -> memory/crm dispatch cleanly skipped
    s.gateway_urls = {}
    s.vapi_inbound_assistant_id = ""
    s.vapi_inbound_phone_number_id = ""
    return s


def _event(*, action="book_call", tier="high", intent=82, contact="owner@acme.example"):
    from backend.voice.models import VapiWebhookEvent

    return VapiWebhookEvent.model_validate(
        {
            "message": {
                "type": "end-of-call-report",
                "call": {
                    "id": "call_pcr_1",
                    "status": "ended",
                    "metadata": {
                        "prospect_id": "p_pcr_1",
                        "prospect_phone": "+15305551212",
                        "owner_email": "meta-owner@acme.example",
                        "owner_name": "Pat Owner",
                        "website_url": "https://acme.example",
                        "company_name": "Acme LLC",
                    },
                },
                "endedReason": "customer-ended-call",
                "summary": "they liked the pitch",
                "structuredData": {
                    "lead_summary": {
                        "company": "Acme LLC",
                        "intent_score": intent,
                        "tier": tier,
                        "recommended_action": action,
                        "contact_offered": contact,
                    }
                },
            }
        }
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    # Synchronous webhook path so the loop runs inline before we assert.
    monkeypatch.setenv("SAMUS_VOICE_WEBHOOK_FAST_ACK", "0")
    monkeypatch.setenv("SAMUS_VOICE_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    yield


def _wire(monkeypatch, settings, *, deliver=None, enroll=None):
    """Patch settings + the two seams; return (deliver_calls, enroll_calls).

    ``enroll_calls`` records ONLY this loop's enrollment (source ==
    "postcall_remediate"). The pre-existing buying-signal route block in
    _process_outbound_end_of_call ALSO calls maybe_enroll_buying_signal
    (source="voice"), gated by its OWN route flag — we mock the real gate so a
    route-disabled call is a genuine no-op and never counts as our invocation.
    """
    import backend.voice.service as svc

    monkeypatch.setattr(svc, "get_settings", lambda: settings)
    # Neutralise unrelated best-effort side effects.
    monkeypatch.setattr(svc, "index_outbound_call", lambda **k: None)
    monkeypatch.setattr(svc, "submit_product_page", lambda **k: {"status": "skipped_existing_sku"})

    deliver_calls: list[dict] = []
    enroll_calls: list[dict] = []

    def _deliver(**kw):
        deliver_calls.append(kw)
        if deliver is not None:
            return deliver(**kw)
        return {"ok": True}

    route_enabled = bool(getattr(settings, "outreach_buying_signal_route_enabled", False))

    def _enroll(**kw):
        # Mirror the real gate: the pre-existing route block (no override) is a
        # no-op unless the route flag is armed. Only OUR loop passes
        # enabled_override=True, so it always records.
        if not kw.get("enabled_override") and not route_enabled:
            return {"enrolled": False, "reason": "route_disabled"}
        enroll_calls.append(kw)
        if enroll is not None:
            return enroll(**kw)
        return {"enrolled": True}

    import backend.fulfill as fulfill_mod
    import backend.outreach.buying_signal_route as bsr_mod

    monkeypatch.setattr(fulfill_mod, "fulfill_customer", _deliver)
    monkeypatch.setattr(bsr_mod, "maybe_enroll_buying_signal", _enroll)
    return deliver_calls, enroll_calls


def _run(event):
    import backend.voice.service as svc

    return asyncio.run(svc.handle_webhook_event(event))


# ---------------------------------------------------------------------------
# (1) Flag OFF => unchanged: no delivery / enroll call made.
# ---------------------------------------------------------------------------


def test_flag_off_makes_no_delivery_or_enroll(monkeypatch):
    settings = _settings(postcall_enabled=False)
    deliver_calls, enroll_calls = _wire(monkeypatch, settings)

    _run(_event())

    assert deliver_calls == []
    assert enroll_calls == []


# ---------------------------------------------------------------------------
# (2) Flag ON + interested outcome => delivery + enroll each invoked once.
# ---------------------------------------------------------------------------


def test_flag_on_interested_invokes_delivery_and_enroll_once(monkeypatch):
    settings = _settings(postcall_enabled=True)
    deliver_calls, enroll_calls = _wire(monkeypatch, settings)

    _run(_event(action="book_call", tier="high", intent=82))

    assert len(deliver_calls) == 1
    assert len(enroll_calls) == 1
    # Delivery targets the offered contact email + the metadata site URL.
    assert deliver_calls[0]["email"] == "owner@acme.example"
    assert deliver_calls[0]["audit_url"] == "https://acme.example"
    # Enroll uses this loop's own gate (route flag not required).
    assert enroll_calls[0]["enabled_override"] is True
    assert enroll_calls[0]["prospect_id"] == "p_pcr_1"


def test_email_falls_back_to_metadata_owner_email(monkeypatch):
    settings = _settings(postcall_enabled=True)
    deliver_calls, _ = _wire(monkeypatch, settings)

    # No contact offered on the call -> fall back to enriched owner_email.
    _run(_event(contact=""))

    assert len(deliver_calls) == 1
    assert deliver_calls[0]["email"] == "meta-owner@acme.example"


# ---------------------------------------------------------------------------
# (3) Flag ON + non-interested outcome => neither invoked.
# ---------------------------------------------------------------------------


def test_flag_on_disqualify_invokes_neither(monkeypatch):
    settings = _settings(postcall_enabled=True)
    deliver_calls, enroll_calls = _wire(monkeypatch, settings)

    # disqualify is a hard-no: never a buying signal.
    _run(_event(action="disqualify", tier="low", intent=10))

    assert deliver_calls == []
    assert enroll_calls == []


def test_flag_on_low_intent_follow_up_invokes_neither(monkeypatch):
    settings = _settings(postcall_enabled=True)
    deliver_calls, enroll_calls = _wire(monkeypatch, settings)

    # follow_up, low tier, sub-threshold score -> not a buying signal.
    _run(_event(action="follow_up", tier="low", intent=40))

    assert deliver_calls == []
    assert enroll_calls == []


# ---------------------------------------------------------------------------
# (4) A raised exception in delivery / enroll does NOT propagate.
# ---------------------------------------------------------------------------


def test_delivery_exception_does_not_break_webhook(monkeypatch):
    settings = _settings(postcall_enabled=True)

    def _boom(**kw):
        raise RuntimeError("SES exploded")

    deliver_calls, enroll_calls = _wire(monkeypatch, settings, deliver=_boom)

    # Must not raise, and enroll still runs (delivery failure is isolated).
    result = _run(_event())

    assert result.received is True
    assert len(deliver_calls) == 1
    assert len(enroll_calls) == 1


def test_enroll_exception_does_not_break_webhook(monkeypatch):
    settings = _settings(postcall_enabled=True)

    def _boom(**kw):
        raise RuntimeError("enroll store exploded")

    deliver_calls, enroll_calls = _wire(monkeypatch, settings, enroll=_boom)

    result = _run(_event())

    assert result.received is True
    assert len(deliver_calls) == 1
    assert len(enroll_calls) == 1


# ---------------------------------------------------------------------------
# Guardrail: the loop never calls anything that initiates a dial.
# ---------------------------------------------------------------------------


def test_loop_never_initiates_a_dial(monkeypatch):
    settings = _settings(postcall_enabled=True)
    _wire(monkeypatch, settings)

    import backend.voice.service as svc

    called = {"initiate": 0}

    def _spy_initiate(*a, **k):  # pragma: no cover — asserted not called
        called["initiate"] += 1
        raise AssertionError("post-call loop must never initiate a call")

    # If the loop ever reached the dial path it would go through initiate_call.
    monkeypatch.setattr(svc, "initiate_call", _spy_initiate)

    _run(_event())

    assert called["initiate"] == 0
