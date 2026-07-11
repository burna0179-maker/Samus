"""Voice workcell FastAPI endpoint tests."""
from __future__ import annotations

import hmac
from hashlib import sha256
from typing import Any

import pytest


def _override_settings(monkeypatch, *, vapi_api_key: str = "",
                       vapi_webhook_secret: str = "",
                       memory_url: str = ""):
    class _S:
        pass

    settings = _S()
    settings.vapi_api_key = vapi_api_key
    settings.vapi_webhook_secret = vapi_webhook_secret
    settings.shared_hmac_key = "test-hmac-32"
    # L3 hardening: voice/app._signature_verification_enabled() reads
    # is_production to keep the Vapi sig check always-on in production. Tests
    # run as a non-production env, so the disable flag still governs here.
    settings.is_production = False
    # AI Digital Receptionist inbound fields — empty so the inbound fork
    # stays inert for these outbound webhook tests.
    settings.vapi_inbound_assistant_id = ""
    settings.vapi_inbound_phone_number_id = ""
    settings.gateway_urls = {"memory": memory_url} if memory_url else {}
    import backend.voice.service as svc_mod
    import backend.voice.app as app_mod
    monkeypatch.setattr(svc_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)


def _audit_to_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_VOICE_AUDIT_PATH", str(tmp_path / "voice_audit.jsonl"))


def _client():
    from fastapi.testclient import TestClient
    from backend.voice.app import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# Outbound endpoints
# ---------------------------------------------------------------------------

def test_post_voice_call_degrades_without_key(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(monkeypatch, vapi_api_key="")
    r = _client().post("/voice/call", json={
        "assistant_id": "asst", "phone_number_id": "phn",
        "customer_number": "+15555550100",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["vapi_error"] == "vapi_api_key_unset"
    assert body["call_id"] == ""


def test_post_voice_call_success(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(monkeypatch, vapi_api_key="vapi_x")

    class _FakeClient:
        def __init__(self, **_):
            pass

        def create_call(self, **kwargs):
            from backend.voice.models import VapiCall
            return VapiCall(id="call_xyz", status="queued")

    import backend.voice.service as svc_mod
    monkeypatch.setattr(svc_mod, "VapiClient", _FakeClient)
    r = _client().post("/voice/call", json={
        "assistant_id": "asst", "phone_number_id": "phn",
        "customer_number": "+15555550100",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["call_id"] == "call_xyz"
    assert body["status"] == "queued"


def test_post_voice_call_rejects_invalid_body(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(monkeypatch, vapi_api_key="vapi_x")
    r = _client().post("/voice/call", json={"assistant_id": "asst"})  # missing fields
    assert r.status_code == 422


def test_get_voice_calls_degrades(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(monkeypatch, vapi_api_key="")
    r = _client().get("/voice/calls")
    assert r.status_code == 200
    body = r.json()
    assert body["calls"] == []
    assert body["vapi_error"] == "vapi_api_key_unset"


def test_get_voice_call_by_id(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(monkeypatch, vapi_api_key="vapi_x")

    class _FakeClient:
        def __init__(self, **_):
            pass

        def get_call(self, call_id):
            from backend.voice.models import VapiCall
            return VapiCall(id=call_id, status="ended", endedReason="completed")

    import backend.voice.service as svc_mod
    monkeypatch.setattr(svc_mod, "VapiClient", _FakeClient)
    r = _client().get("/voice/call/call_abc")
    assert r.status_code == 200
    body = r.json()
    assert body["call"]["id"] == "call_abc"
    assert body["call"]["endedReason"] == "completed"


# ---------------------------------------------------------------------------
# Webhook — signature gate
# ---------------------------------------------------------------------------

def _eo_payload() -> dict[str, Any]:
    return {
        "message": {
            "type": "end-of-call-report",
            "call": {"id": "call_eo_1", "status": "ended"},
            "endedReason": "customer-ended-call",
            "structuredData": {
                "lead_summary": {
                    "company": "Acme",
                    "tier": "high",
                    "intent_score": 80,
                    "recommended_action": "book_call",
                },
            },
        },
    }


def test_webhook_dispatches_when_verify_off(tmp_path, monkeypatch):
    """Default conftest sets SAMUS_VOICE_VERIFY_WEBHOOK=0 so this path runs."""
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(monkeypatch, vapi_webhook_secret="",
                       memory_url="http://samus-memory:8080")

    async def _fake_signed_post_json(*a, **kw):
        class _R:
            status_code = 200
            text = '{"status":"ok"}'
        return _R()

    import backend.voice.service as svc_mod
    monkeypatch.setattr(svc_mod, "signed_post_json", _fake_signed_post_json)
    r = _client().post("/vapi/webhook", json=_eo_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["received"] is True
    assert body["memory_dispatch_ok"] is True


def test_webhook_returns_503_when_verify_on_but_secret_unset(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(monkeypatch, vapi_webhook_secret="")
    monkeypatch.setenv("SAMUS_VOICE_VERIFY_WEBHOOK", "1")
    r = _client().post("/vapi/webhook", json=_eo_payload())
    assert r.status_code == 503
    assert "vapi_webhook_secret_unset" in r.text


def test_webhook_accepts_valid_signature(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    secret = "test-secret-32-bytes-of-entropy!"
    _override_settings(monkeypatch, vapi_webhook_secret=secret,
                       memory_url="http://samus-memory:8080")
    monkeypatch.setenv("SAMUS_VOICE_VERIFY_WEBHOOK", "1")

    async def _fake_signed_post_json(*a, **kw):
        class _R:
            status_code = 200
            text = "{}"
        return _R()

    import backend.voice.service as svc_mod
    monkeypatch.setattr(svc_mod, "signed_post_json", _fake_signed_post_json)

    import json as _json
    body_bytes = _json.dumps(_eo_payload()).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), body_bytes, sha256).hexdigest()
    r = _client().post(
        "/vapi/webhook",
        content=body_bytes,
        headers={
            "x-vapi-signature": sig,
            "content-type": "application/json",
        },
    )
    assert r.status_code == 200, r.text


def test_webhook_rejects_bad_signature(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    secret = "test-secret-32-bytes-of-entropy!"
    _override_settings(monkeypatch, vapi_webhook_secret=secret)
    monkeypatch.setenv("SAMUS_VOICE_VERIFY_WEBHOOK", "1")

    import json as _json
    body_bytes = _json.dumps(_eo_payload()).encode("utf-8")
    bad_sig = hmac.new(b"wrong-secret", body_bytes, sha256).hexdigest()
    r = _client().post(
        "/vapi/webhook",
        content=body_bytes,
        headers={
            "x-vapi-signature": bad_sig,
            "content-type": "application/json",
        },
    )
    assert r.status_code == 403
    assert "vapi_signature_invalid" in r.text


def test_webhook_rejects_missing_signature(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(monkeypatch, vapi_webhook_secret="test-secret-32-bytes-of-entropy!")
    monkeypatch.setenv("SAMUS_VOICE_VERIFY_WEBHOOK", "1")
    r = _client().post("/vapi/webhook", json=_eo_payload())
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# /work TaskEnvelope route
# ---------------------------------------------------------------------------

def test_work_envelope_routes_initiate_call(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(monkeypatch, vapi_api_key="")
    envelope = {
        "task_id": "t1",
        "payload": {
            "assistant_id": "a", "phone_number_id": "p",
            "customer_number": "+15555550100",
        },
        "metadata": {"action": "initiate_call"},
    }
    r = _client().post("/work", json=envelope)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["vapi_error"] == "vapi_api_key_unset"


def test_work_envelope_rejects_unknown_action(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    envelope = {
        "task_id": "t1",
        "payload": {},
        "metadata": {"action": "rocket_launch"},
    }
    r = _client().post("/work", json=envelope)
    assert r.status_code == 400
    assert "unknown_action" in r.text