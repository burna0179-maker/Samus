"""Voice service — outbound initiate + inbound webhook routing."""

from __future__ import annotations

import asyncio
import json
from typing import Any


def _override_settings(
    monkeypatch,
    *,
    vapi_api_key: str = "",
    memory_url: str = "",
    crm_url: str = "",
    shared_hmac_key: str = "test-hmac-32",
):
    """Configure voice service settings stub.

    HEAD branch extended this helper with ``crm_url`` to support Phase 2
    CRM dispatch tests. Origin had only memory_url. Union keeps both.
    """

    class _S:
        pass

    settings = _S()
    settings.vapi_api_key = vapi_api_key
    settings.shared_hmac_key = shared_hmac_key
    # AI Digital Receptionist inbound fields — empty so the inbound fork in
    # handle_webhook_event evaluates False and these outbound tests are
    # unaffected.
    settings.vapi_inbound_assistant_id = ""
    settings.vapi_inbound_phone_number_id = ""
    gw: dict[str, str] = {}
    if memory_url:
        gw["memory"] = memory_url
    if crm_url:
        gw["crm"] = crm_url
    settings.gateway_urls = gw
    import backend.voice.service as svc_mod

    monkeypatch.setattr(svc_mod, "get_settings", lambda: settings)


def _audit_to_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_VOICE_AUDIT_PATH", str(tmp_path / "voice_audit.jsonl"))


# ---------------------------------------------------------------------------
# initiate_call
# ---------------------------------------------------------------------------


def test_initiate_call_returns_degraded_without_key(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(monkeypatch, vapi_api_key="")
    from backend.voice.service import initiate_call
    from backend.voice.models import InitiateCallRequest

    result = initiate_call(
        InitiateCallRequest(
            assistant_id="asst",
            phone_number_id="phn",
            customer_number="+15555550100",
        )
    )
    assert result.vapi_error == "vapi_api_key_unset"
    assert result.call_id == ""


def test_initiate_call_success(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(monkeypatch, vapi_api_key="vapi_x")

    class _FakeClient:
        def __init__(self, **_):
            pass

        def create_call(self, **kwargs):
            from backend.voice.models import VapiCall

            return VapiCall(
                id="call_new",
                status="queued",
                assistantId=kwargs["assistant_id"],
                phoneNumberId=kwargs["phone_number_id"],
            )

    import backend.voice.service as svc_mod

    monkeypatch.setattr(svc_mod, "VapiClient", _FakeClient)
    from backend.voice.service import initiate_call
    from backend.voice.models import InitiateCallRequest

    result = initiate_call(
        InitiateCallRequest(
            assistant_id="asst",
            phone_number_id="phn",
            customer_number="+15555550100",
        )
    )
    assert result.call_id == "call_new"
    assert result.status == "queued"
    assert result.vapi_error is None


def test_initiate_call_records_vapi_error(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(monkeypatch, vapi_api_key="vapi_x")

    from backend.voice.client import VapiError

    class _FakeClient:
        def __init__(self, **_):
            pass

        def create_call(self, **_):
            raise VapiError("vapi_http_401: bad key")

    import backend.voice.service as svc_mod

    monkeypatch.setattr(svc_mod, "VapiClient", _FakeClient)
    from backend.voice.service import initiate_call
    from backend.voice.models import InitiateCallRequest

    result = initiate_call(
        InitiateCallRequest(
            assistant_id="asst",
            phone_number_id="phn",
            customer_number="+15555550100",
        )
    )
    assert result.call_id == ""
    assert "vapi_http_401" in (result.vapi_error or "")


# ---------------------------------------------------------------------------
# handle_webhook_event
# ---------------------------------------------------------------------------


def _end_of_call_event(
    *,
    lead_summary: dict | None = None,
    call_id: str = "call_end_1",
    metadata: dict | None = None,
    transcript: str | None = None,
    ended_reason: str = "customer-ended-call",
):
    """Build a synthetic end-of-call webhook payload.

    HEAD branch extended this helper with ``metadata`` and ``transcript``
    parameters to support Phase 2 CRM dispatch tests. Origin had only
    ``lead_summary`` and ``call_id``. Union keeps all parameters.
    """
    structured: dict[str, Any] = {}
    if lead_summary is not None:
        structured["lead_summary"] = lead_summary
    call_block: dict[str, Any] = {"id": call_id, "status": "ended"}
    if metadata is not None:
        call_block["metadata"] = metadata
    msg: dict[str, Any] = {
        "type": "end-of-call-report",
        "call": call_block,
        "endedReason": ended_reason,
        "summary": "they liked the pitch",
        "recordingUrl": "https://vapi.example/recordings/1.mp3",
        "structuredData": structured,
    }
    if transcript is not None:
        msg["transcript"] = transcript
    return {"message": msg}


def test_webhook_non_terminal_event_skips_dispatch(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(monkeypatch, vapi_api_key="vapi_x", memory_url="")
    from backend.voice.service import handle_webhook_event
    from backend.voice.models import VapiWebhookEvent

    event = VapiWebhookEvent.model_validate(
        {"message": {"type": "transcript", "call": {"id": "c1"}}}
    )
    result = asyncio.run(handle_webhook_event(event))
    assert result.received is True
    assert result.message_type == "transcript"
    assert result.memory_dispatch_ok is False
    assert result.memory_dispatch_error is None  # not an error, just no dispatch
    assert result.lead_summary is None


def test_webhook_end_of_call_without_lead_summary(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(monkeypatch, vapi_api_key="vapi_x", memory_url="http://samus-memory:8080")
    from backend.voice.service import handle_webhook_event
    from backend.voice.models import VapiWebhookEvent

    event = VapiWebhookEvent.model_validate(_end_of_call_event(lead_summary=None))
    result = asyncio.run(handle_webhook_event(event))
    assert result.received is True
    assert result.lead_summary is None
    assert result.memory_dispatch_ok is False
    assert result.memory_dispatch_error == "no_lead_summary_in_structured_data"


def test_webhook_end_of_call_dispatches_to_memory(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(monkeypatch, vapi_api_key="vapi_x", memory_url="http://samus-memory:8080")

    captured: dict = {}

    class _FakeResp:
        status_code = 200
        text = '{"status": "ok"}'

    async def _fake_signed_post_json(base_url, path, payload, **kwargs):
        captured["base_url"] = base_url
        captured["path"] = path
        captured["payload"] = payload
        return _FakeResp()

    import backend.voice.service as svc_mod

    monkeypatch.setattr(svc_mod, "signed_post_json", _fake_signed_post_json)

    from backend.voice.service import handle_webhook_event
    from backend.voice.models import VapiWebhookEvent

    event = VapiWebhookEvent.model_validate(
        _end_of_call_event(
            lead_summary={
                "company": "Acme",
                "lead_volume": "10/week",
                "automation_level": "manual",
                "pain_points": ["missed follow-ups"],
                "intent_score": 82,
                "tier": "high",
                "recommended_action": "book_call",
            }
        )
    )
    result = asyncio.run(handle_webhook_event(event))
    assert result.memory_dispatch_ok is True
    assert result.memory_dispatch_error is None
    assert result.lead_summary is not None
    assert result.lead_summary.tier == "high"
    assert result.lead_summary.intent_score == 82
    assert captured["base_url"] == "http://samus-memory:8080"
    assert captured["path"] == "/write"
    assert captured["payload"]["namespace"] == "voice.calls"
    assert captured["payload"]["key"] == "call_end_1"
    assert captured["payload"]["value"]["lead_summary"]["company"] == "Acme"


def test_webhook_end_of_call_handles_memory_url_unset(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(monkeypatch, vapi_api_key="vapi_x", memory_url="")
    from backend.voice.service import handle_webhook_event
    from backend.voice.models import VapiWebhookEvent

    event = VapiWebhookEvent.model_validate(
        _end_of_call_event(
            lead_summary={
                "tier": "low",
                "recommended_action": "disqualify",
            }
        )
    )
    result = asyncio.run(handle_webhook_event(event))
    assert result.memory_dispatch_ok is False
    assert result.memory_dispatch_error == "memory_url_unset"


def test_audit_ledger_is_written(tmp_path, monkeypatch):
    """End-of-call event must land in the JSONL audit ledger regardless of dispatch outcome."""
    audit_path = tmp_path / "voice_audit.jsonl"
    monkeypatch.setenv("SAMUS_VOICE_AUDIT_PATH", str(audit_path))
    _override_settings(monkeypatch, vapi_api_key="vapi_x", memory_url="")
    from backend.voice.service import handle_webhook_event
    from backend.voice.models import VapiWebhookEvent

    event = VapiWebhookEvent.model_validate(
        _end_of_call_event(
            lead_summary={
                "tier": "medium",
                "intent_score": 55,
            }
        )
    )
    asyncio.run(handle_webhook_event(event))
    assert audit_path.exists()
    lines = [
        json.loads(l) for l in audit_path.read_text(encoding="utf-8").splitlines() if l.strip()
    ]
    assert any(
        rec.get("service") == "voice" and rec.get("action") == "webhook_end_of_call"
        for rec in lines
    )


# ---------------------------------------------------------------------------
# Phase 2 — voice end-of-call also dispatches to samus-crm
# (Conversation row + per-prospect CallState upsert)
# ---------------------------------------------------------------------------


def _capture_signed_posts(monkeypatch):
    """Stub signed_post_json to record every (base_url, path, payload) call.

    Each call returns a 200 response with an empty body. Returns the captured
    list so tests can assert the dispatch fanout.
    """
    captured: list[dict] = []

    class _FakeResp:
        status_code = 200
        text = "{}"

    async def _fake(base_url, path, payload, **kwargs):
        captured.append({"base_url": base_url, "path": path, "payload": payload})
        return _FakeResp()

    import backend.voice.service as svc_mod

    monkeypatch.setattr(svc_mod, "signed_post_json", _fake)
    return captured


def test_webhook_end_of_call_dispatches_to_crm_alongside_memory(
    tmp_path,
    monkeypatch,
):
    """Both memory and CRM receive the end-of-call payload."""
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(
        monkeypatch,
        vapi_api_key="vapi_x",
        memory_url="http://samus-memory:8080",
        crm_url="http://samus-crm:8080",
    )
    captured = _capture_signed_posts(monkeypatch)

    from backend.voice.service import handle_webhook_event
    from backend.voice.models import VapiWebhookEvent

    event = VapiWebhookEvent.model_validate(
        _end_of_call_event(
            lead_summary={
                "company": "Acme",
                "intent_score": 82,
                "tier": "high",
                "recommended_action": "book_call",
            },
            metadata={"prospect_id": "pr_acme"},
            transcript="Hi, this is Morgan...",
        )
    )
    result = asyncio.run(handle_webhook_event(event))

    assert result.memory_dispatch_ok is True
    assert result.crm_dispatch_ok is True
    assert result.crm_dispatch_error is None

    # Three dispatches expected: memory + crm conversation + crm call-state
    paths = [c["path"] for c in captured]
    assert "/write" in paths
    assert "/crm/conversations" in paths
    assert "/crm/call-state/pr_acme" in paths
    assert len(captured) == 3

    crm_conv = next(c for c in captured if c["path"] == "/crm/conversations")
    assert crm_conv["base_url"] == "http://samus-crm:8080"
    assert crm_conv["payload"]["conversation_id"] == "cv_vapi_call_end_1"
    assert crm_conv["payload"]["prospect_id"] == "pr_acme"
    assert crm_conv["payload"]["channel"] == "call"
    assert crm_conv["payload"]["status"] == "completed"
    assert crm_conv["payload"]["source"] == "vapi"
    assert crm_conv["payload"]["source_ref"] == "call_end_1"
    # recommended_action "book_call" is translated to the canonical "booked"
    # outcome — the same value the operator hand-call path records.
    assert crm_conv["payload"]["outcome"] == "booked"
    assert crm_conv["payload"]["transcript"] == "Hi, this is Morgan..."
    assert crm_conv["payload"]["structured_data"]["lead_summary"]["company"] == "Acme"

    crm_state = next(c for c in captured if c["path"] == "/crm/call-state/pr_acme")
    assert crm_state["payload"]["prospect_id"] == "pr_acme"
    assert crm_state["payload"]["state"] == "completed"  # booked -> completed
    assert crm_state["payload"]["last_call_id"] == "call_end_1"
    assert crm_state["payload"]["last_outcome"] == "booked"


def test_webhook_no_answer_records_no_answer_outcome(tmp_path, monkeypatch):
    """A Vapi call that rang out (no lead summary) records the canonical
    no_answer outcome + state — not the old hardcoded 'completed'."""
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(
        monkeypatch,
        vapi_api_key="vapi_x",
        memory_url="http://samus-memory:8080",
        crm_url="http://samus-crm:8080",
    )
    captured = _capture_signed_posts(monkeypatch)

    from backend.voice.service import handle_webhook_event
    from backend.voice.models import VapiWebhookEvent

    event = VapiWebhookEvent.model_validate(
        _end_of_call_event(
            lead_summary=None,
            ended_reason="customer-did-not-answer",
            metadata={"prospect_id": "pr_acme"},
        )
    )
    asyncio.run(handle_webhook_event(event))

    crm_conv = next(c for c in captured if c["path"] == "/crm/conversations")
    assert crm_conv["payload"]["outcome"] == "no_answer"
    crm_state = next(c for c in captured if c["path"] == "/crm/call-state/pr_acme")
    assert crm_state["payload"]["state"] == "no_answer"
    assert crm_state["payload"]["last_outcome"] == "no_answer"


def test_webhook_voicemail_records_voicemail_outcome(tmp_path, monkeypatch):
    """A Vapi call that hit voicemail records the canonical voicemail state."""
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(
        monkeypatch,
        vapi_api_key="vapi_x",
        memory_url="http://samus-memory:8080",
        crm_url="http://samus-crm:8080",
    )
    captured = _capture_signed_posts(monkeypatch)

    from backend.voice.service import handle_webhook_event
    from backend.voice.models import VapiWebhookEvent

    event = VapiWebhookEvent.model_validate(
        _end_of_call_event(
            lead_summary=None,
            ended_reason="voicemail",
            metadata={"prospect_id": "pr_acme"},
        )
    )
    asyncio.run(handle_webhook_event(event))

    crm_state = next(c for c in captured if c["path"] == "/crm/call-state/pr_acme")
    assert crm_state["payload"]["state"] == "voicemail"
    assert crm_state["payload"]["last_outcome"] == "voicemail"


def test_webhook_gatekeeper_keeps_prospect_callable(tmp_path, monkeypatch):
    """A Vapi call that hit a gatekeeper records the non-terminal gatekeeper
    state — the prospect stays callable, not marked completed."""
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(
        monkeypatch,
        vapi_api_key="vapi_x",
        memory_url="http://samus-memory:8080",
        crm_url="http://samus-crm:8080",
    )
    captured = _capture_signed_posts(monkeypatch)

    from backend.voice.service import handle_webhook_event
    from backend.voice.models import VapiWebhookEvent

    event = VapiWebhookEvent.model_validate(
        _end_of_call_event(
            lead_summary={"company": "Acme", "recommended_action": "gatekeeper"},
            metadata={"prospect_id": "pr_acme"},
        )
    )
    asyncio.run(handle_webhook_event(event))

    crm_conv = next(c for c in captured if c["path"] == "/crm/conversations")
    assert crm_conv["payload"]["outcome"] == "gatekeeper"
    crm_state = next(c for c in captured if c["path"] == "/crm/call-state/pr_acme")
    assert crm_state["payload"]["state"] == "gatekeeper"
    assert crm_state["payload"]["last_outcome"] == "gatekeeper"


def test_webhook_validates_a_contact_offered_on_the_call(tmp_path, monkeypatch):
    """A contact Morgan was handed is run through contact_validation; the
    verdict rides on the Conversation's structured_data."""
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(
        monkeypatch,
        vapi_api_key="vapi_x",
        memory_url="http://samus-memory:8080",
        crm_url="http://samus-crm:8080",
    )
    captured = _capture_signed_posts(monkeypatch)

    from backend.voice.service import handle_webhook_event
    from backend.voice.models import VapiWebhookEvent

    event = VapiWebhookEvent.model_validate(
        _end_of_call_event(
            lead_summary={
                "company": "Magnolia Modern Dentistry",
                "recommended_action": "gatekeeper",
                "contact_offered": "info@magnolia-.com",
            },
            metadata={"prospect_id": "pr_mag"},
        )
    )
    asyncio.run(handle_webhook_event(event))

    crm_conv = next(c for c in captured if c["path"] == "/crm/conversations")
    assessment = crm_conv["payload"]["structured_data"]["contact_assessment"]
    assert assessment["email"] == "info@magnolia-.com"
    assert assessment["verdict"] == "malformed"
    assert assessment["valid_syntax"] is False
    assert assessment["reasons"]


def test_webhook_prefers_text_rides_on_structured_data(tmp_path, monkeypatch):
    """prefers_text captured by the agent survives onto the Conversation."""
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(
        monkeypatch,
        vapi_api_key="vapi_x",
        memory_url="http://samus-memory:8080",
        crm_url="http://samus-crm:8080",
    )
    captured = _capture_signed_posts(monkeypatch)

    from backend.voice.service import handle_webhook_event
    from backend.voice.models import VapiWebhookEvent

    event = VapiWebhookEvent.model_validate(
        _end_of_call_event(
            lead_summary={
                "company": "Acme",
                "recommended_action": "follow_up",
                "prefers_text": True,
            },
            metadata={"prospect_id": "pr_acme"},
        )
    )
    asyncio.run(handle_webhook_event(event))

    crm_conv = next(c for c in captured if c["path"] == "/crm/conversations")
    lead = crm_conv["payload"]["structured_data"]["lead_summary"]
    assert lead["prefers_text"] is True


def test_webhook_end_of_call_crm_skips_callstate_when_no_prospect_id(
    tmp_path,
    monkeypatch,
):
    """No prospect_id in metadata -> Conversation still written, CallState
    skipped without flagging as an error (data gap upstream, not CRM failure)."""
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(
        monkeypatch,
        vapi_api_key="vapi_x",
        memory_url="http://samus-memory:8080",
        crm_url="http://samus-crm:8080",
    )
    captured = _capture_signed_posts(monkeypatch)

    from backend.voice.service import handle_webhook_event
    from backend.voice.models import VapiWebhookEvent

    event = VapiWebhookEvent.model_validate(
        _end_of_call_event(
            lead_summary={"tier": "medium", "recommended_action": "follow_up"},
            # no metadata
        )
    )
    result = asyncio.run(handle_webhook_event(event))

    assert result.crm_dispatch_ok is True  # treated as full success
    paths = [c["path"] for c in captured]
    assert "/crm/conversations" in paths
    assert not any(p.startswith("/crm/call-state/") for p in paths)


def test_webhook_end_of_call_crm_failure_does_not_block_memory(
    tmp_path,
    monkeypatch,
):
    """A 5xx from CRM is recorded as crm_dispatch_error but memory_dispatch
    still succeeds and the webhook returns 200 (best-effort contract)."""
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(
        monkeypatch,
        vapi_api_key="vapi_x",
        memory_url="http://samus-memory:8080",
        crm_url="http://samus-crm:8080",
    )

    class _OkResp:
        status_code = 200
        text = "{}"

    class _ErrResp:
        status_code = 502
        text = "bad gateway"

    async def _fake(base_url, path, payload, **kwargs):
        if path.startswith("/crm/"):
            return _ErrResp()
        return _OkResp()

    import backend.voice.service as svc_mod

    monkeypatch.setattr(svc_mod, "signed_post_json", _fake)

    from backend.voice.service import handle_webhook_event
    from backend.voice.models import VapiWebhookEvent

    event = VapiWebhookEvent.model_validate(
        _end_of_call_event(
            lead_summary={"tier": "high", "recommended_action": "book_call"},
            metadata={"prospect_id": "pr_acme"},
        )
    )
    result = asyncio.run(handle_webhook_event(event))

    assert result.memory_dispatch_ok is True
    assert result.crm_dispatch_ok is False
    assert result.crm_dispatch_error is not None
    assert "crm_http_502" in result.crm_dispatch_error


def test_webhook_end_of_call_crm_url_unset_is_explicit_error(
    tmp_path,
    monkeypatch,
):
    """When CRM URL isn't configured, the dispatch returns crm_url_unset —
    explicit (not silently swallowed) so the operator can wire it up."""
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(
        monkeypatch,
        vapi_api_key="vapi_x",
        memory_url="http://samus-memory:8080",
        # crm_url omitted
    )
    captured = _capture_signed_posts(monkeypatch)
    from backend.voice.service import handle_webhook_event
    from backend.voice.models import VapiWebhookEvent

    event = VapiWebhookEvent.model_validate(
        _end_of_call_event(
            lead_summary={"tier": "high", "recommended_action": "book_call"},
            metadata={"prospect_id": "pr_acme"},
        )
    )
    result = asyncio.run(handle_webhook_event(event))

    assert result.crm_dispatch_ok is False
    assert result.crm_dispatch_error is not None
    assert "crm_url_unset" in result.crm_dispatch_error
    # Memory still fires.
    assert any(c["path"] == "/write" for c in captured)
    # No CRM dispatches escaped.
    assert not any(c["path"].startswith("/crm/") for c in captured)


def test_webhook_end_of_call_audit_records_crm_status(tmp_path, monkeypatch):
    """Audit entry for an end-of-call webhook is written with a hashed payload
    (input/output are stored as deterministic hashes per audit-ledger contract).
    Verify the row exists with the right action + a status that reflects
    aggregate dispatch outcome."""
    audit_path = tmp_path / "voice_audit.jsonl"
    monkeypatch.setenv("SAMUS_VOICE_AUDIT_PATH", str(audit_path))
    _override_settings(
        monkeypatch,
        vapi_api_key="vapi_x",
        memory_url="http://samus-memory:8080",
        crm_url="http://samus-crm:8080",
    )
    _capture_signed_posts(monkeypatch)

    from backend.voice.service import handle_webhook_event
    from backend.voice.models import VapiWebhookEvent

    event = VapiWebhookEvent.model_validate(
        _end_of_call_event(
            lead_summary={"tier": "low", "recommended_action": "disqualify"},
            metadata={"prospect_id": "pr_x"},
        )
    )
    asyncio.run(handle_webhook_event(event))
    assert audit_path.exists()
    lines = [
        json.loads(l) for l in audit_path.read_text(encoding="utf-8").splitlines() if l.strip()
    ]
    eoc = next((r for r in lines if r.get("action") == "webhook_end_of_call"), None)
    assert eoc is not None
    # With both dispatches succeeding (memory + CRM 200s), status is completed.
    assert eoc.get("status") == "completed"
