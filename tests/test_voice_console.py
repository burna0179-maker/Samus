"""Operator voice console tests — backend.voice.console.

Covers the token gate (fail-closed when unset, 401 on a wrong token), the
config-check booleans, server-side phone validation, the degraded-Vapi path,
and the call / list / status routes.
"""

from __future__ import annotations


def _override_settings(
    monkeypatch,
    *,
    vapi_api_key: str = "",
    vapi_assistant_id: str = "",
    vapi_phone_number_id: str = "",
):
    """Pin a fake settings object onto every module the console touches."""

    class _S:
        pass

    settings = _S()
    settings.vapi_api_key = vapi_api_key
    settings.vapi_assistant_id = vapi_assistant_id
    settings.vapi_phone_number_id = vapi_phone_number_id
    settings.vapi_webhook_secret = ""
    settings.shared_hmac_key = "test-hmac-32"
    settings.is_production = False
    settings.vapi_inbound_assistant_id = ""
    settings.vapi_inbound_phone_number_id = ""
    settings.gateway_urls = {}

    import backend.voice.app as app_mod
    import backend.voice.console as console_mod
    import backend.voice.service as svc_mod

    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(console_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(svc_mod, "get_settings", lambda: settings)


def _audit_to_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_VOICE_AUDIT_PATH", str(tmp_path / "voice_audit.jsonl"))
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH", str(tmp_path / "voice_events.jsonl"))


def _client():
    from fastapi.testclient import TestClient
    from backend.voice.app import app

    return TestClient(app)


# ---------------------------------------------------------------------------
# Page + token gate
# ---------------------------------------------------------------------------


def test_console_page_is_served(monkeypatch):
    monkeypatch.delenv("SAMUS_VOICE_CONSOLE_TOKEN", raising=False)
    r = _client().get("/console")
    assert r.status_code == 200, r.text
    assert "Samus Voice Console" in r.text


def test_console_api_503_when_token_unset(monkeypatch):
    """Fail-closed: no SAMUS_VOICE_CONSOLE_TOKEN -> the API is off."""
    monkeypatch.delenv("SAMUS_VOICE_CONSOLE_TOKEN", raising=False)
    r = _client().get("/console/api/config-check")
    assert r.status_code == 503
    assert "console_token_unset" in r.text


def test_console_api_401_with_wrong_token(monkeypatch):
    monkeypatch.setenv("SAMUS_VOICE_CONSOLE_TOKEN", "right-token")
    r = _client().get("/console/api/config-check", headers={"X-Console-Token": "wrong-token"})
    assert r.status_code == 401
    assert "console_unauthorized" in r.text


def test_console_api_401_with_missing_header(monkeypatch):
    monkeypatch.setenv("SAMUS_VOICE_CONSOLE_TOKEN", "right-token")
    r = _client().get("/console/api/config-check")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# config-check
# ---------------------------------------------------------------------------


def test_config_check_reports_unset(monkeypatch):
    monkeypatch.setenv("SAMUS_VOICE_CONSOLE_TOKEN", "tok")
    _override_settings(monkeypatch)  # everything empty
    r = _client().get("/console/api/config-check", headers={"X-Console-Token": "tok"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "vapi_api_key_set": False,
        "vapi_assistant_id_set": False,
        "vapi_phone_number_id_set": False,
    }


def test_config_check_reports_set(monkeypatch):
    monkeypatch.setenv("SAMUS_VOICE_CONSOLE_TOKEN", "tok")
    _override_settings(
        monkeypatch, vapi_api_key="k", vapi_assistant_id="a", vapi_phone_number_id="p"
    )
    r = _client().get("/console/api/config-check", headers={"X-Console-Token": "tok"})
    assert r.status_code == 200
    assert all(r.json().values())


# ---------------------------------------------------------------------------
# Place a call
# ---------------------------------------------------------------------------


def test_console_call_rejects_bad_phone(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    monkeypatch.setenv("SAMUS_VOICE_CONSOLE_TOKEN", "tok")
    _override_settings(
        monkeypatch, vapi_api_key="k", vapi_assistant_id="a", vapi_phone_number_id="p"
    )
    r = _client().post(
        "/console/api/call", headers={"X-Console-Token": "tok"}, json={"customer_number": "12345"}
    )  # too few digits
    assert r.status_code == 422
    assert "invalid_phone_number" in r.text


def test_console_call_503_when_assistant_unset(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    monkeypatch.setenv("SAMUS_VOICE_CONSOLE_TOKEN", "tok")
    _override_settings(monkeypatch, vapi_api_key="k")  # assistant/phone empty
    r = _client().post(
        "/console/api/call",
        headers={"X-Console-Token": "tok"},
        json={"customer_number": "5555555555"},
    )
    assert r.status_code == 503
    assert "vapi_assistant_or_phone_unset" in r.text


def test_console_call_degrades_without_key(tmp_path, monkeypatch):
    """No VAPI_API_KEY -> HTTP 200 with vapi_error, never a crash."""
    _audit_to_tmp(monkeypatch, tmp_path)
    monkeypatch.setenv("SAMUS_VOICE_CONSOLE_TOKEN", "tok")
    _override_settings(
        monkeypatch, vapi_api_key="", vapi_assistant_id="a", vapi_phone_number_id="p"
    )
    r = _client().post(
        "/console/api/call",
        headers={"X-Console-Token": "tok"},
        json={"customer_number": "5555555555"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["vapi_error"] == "vapi_api_key_unset"
    assert body["call_id"] == ""


def test_console_call_success(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    monkeypatch.setenv("SAMUS_VOICE_CONSOLE_TOKEN", "tok")
    _override_settings(
        monkeypatch, vapi_api_key="vapi_x", vapi_assistant_id="a", vapi_phone_number_id="p"
    )

    captured = {}

    class _FakeClient:
        def __init__(self, **_):
            pass

        def create_call(self, **kwargs):
            captured.update(kwargs)
            from backend.voice.models import VapiCall

            return VapiCall(id="call_abc", status="queued")

    import backend.voice.service as svc_mod

    monkeypatch.setattr(svc_mod, "VapiClient", _FakeClient)

    r = _client().post(
        "/console/api/call",
        headers={"X-Console-Token": "tok"},
        json={"customer_number": "555-555-5555", "customer_name": "Acme HVAC"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["call_id"] == "call_abc"
    assert body["status"] == "queued"
    # Server normalized the operator-typed number to E.164.
    assert captured["customer_number"] == "+15555555555"
    assert captured["customer_name"] == "Acme HVAC"


def test_console_call_rejects_unknown_fields(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    monkeypatch.setenv("SAMUS_VOICE_CONSOLE_TOKEN", "tok")
    _override_settings(
        monkeypatch, vapi_api_key="k", vapi_assistant_id="a", vapi_phone_number_id="p"
    )
    # assistant_id is server-chosen; the browser must not be able to pass one.
    r = _client().post(
        "/console/api/call",
        headers={"X-Console-Token": "tok"},
        json={"customer_number": "5555555555", "assistant_id": "attacker-asst"},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Read routes
# ---------------------------------------------------------------------------


def test_console_calls_list_degrades(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    monkeypatch.setenv("SAMUS_VOICE_CONSOLE_TOKEN", "tok")
    _override_settings(monkeypatch, vapi_api_key="")
    r = _client().get("/console/api/calls", headers={"X-Console-Token": "tok"})
    assert r.status_code == 200
    body = r.json()
    assert body["calls"] == []
    assert body["vapi_error"] == "vapi_api_key_unset"


def test_console_call_status_requires_call_id(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    monkeypatch.setenv("SAMUS_VOICE_CONSOLE_TOKEN", "tok")
    _override_settings(monkeypatch, vapi_api_key="k")
    r = _client().get("/console/api/call-status", headers={"X-Console-Token": "tok"})
    assert r.status_code == 422
    assert "call_id" in r.text


def test_console_summary(tmp_path, monkeypatch):
    _audit_to_tmp(monkeypatch, tmp_path)
    monkeypatch.setenv("SAMUS_VOICE_CONSOLE_TOKEN", "tok")
    _override_settings(monkeypatch, vapi_api_key="k")
    r = _client().get("/console/api/summary", headers={"X-Console-Token": "tok"})
    assert r.status_code == 200, r.text
    body = r.json()
    # No events ledger yet -> a zeroed, log_loaded=False summary.
    assert body["total_calls"] == 0
    assert body["log_loaded"] is False
