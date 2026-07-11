"""Verify VapiClient.create_call forwards variable_values under assistantOverrides."""

from __future__ import annotations

import httpx


class _FakeHttpx:
    def __init__(self, client_cls):
        self.Client = client_cls

    def __getattr__(self, name):
        return getattr(httpx, name)


def _build_client(monkeypatch, *, capture: dict):
    class _Resp:
        status_code = 200
        text = '{"id":"call_1","status":"queued"}'
        content = text.encode("utf-8")

        def json(self):
            return {"id": "call_1", "status": "queued"}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, headers=None, params=None, json=None):
            capture["body"] = json
            return _Resp()

    import backend.voice.client as mod

    monkeypatch.setattr(mod, "httpx", _FakeHttpx(_Client))
    return mod.VapiClient(api_key="k")


def test_create_call_forwards_variable_values_under_assistant_overrides(monkeypatch):
    cap: dict = {}
    client = _build_client(monkeypatch, capture=cap)
    client.create_call(
        assistant_id="ast_x",
        phone_number_id="pn_y",
        customer_number="+15305551234",
        customer_name="Acme",
        metadata={"prospect_id": "p1"},
        variable_values={"company_name": "Acme", "lead_score": 80, "pitch": "hi"},
    )
    body = cap["body"]
    assert body["assistantId"] == "ast_x"
    assert body["phoneNumberId"] == "pn_y"
    assert body["metadata"] == {"prospect_id": "p1"}
    overrides = body["assistantOverrides"]
    assert overrides["variableValues"]["company_name"] == "Acme"
    # All values coerced to str.
    assert overrides["variableValues"]["lead_score"] == "80"


def test_create_call_omits_assistant_overrides_when_no_variables(monkeypatch):
    cap: dict = {}
    client = _build_client(monkeypatch, capture=cap)
    client.create_call(
        assistant_id="ast_x",
        phone_number_id="pn_y",
        customer_number="+15305551234",
    )
    assert "assistantOverrides" not in cap["body"]


def test_create_call_forwards_voicemail_message(monkeypatch):
    """The per-prospect voicemail rides under assistantOverrides.voicemailMessage."""
    cap: dict = {}
    client = _build_client(monkeypatch, capture=cap)
    client.create_call(
        assistant_id="ast_x",
        phone_number_id="pn_y",
        customer_number="+15305551234",
        voicemail_message="Hey, it's Morgan from HustleForge — sorry I missed you.",
    )
    overrides = cap["body"]["assistantOverrides"]
    assert overrides["voicemailMessage"].startswith("Hey, it's Morgan")
    # voicemail-only call: no variableValues key when none were passed.
    assert "variableValues" not in overrides


def test_create_call_forwards_variables_and_voicemail_together(monkeypatch):
    """variable_values + voicemail_message coexist under one assistantOverrides."""
    cap: dict = {}
    client = _build_client(monkeypatch, capture=cap)
    client.create_call(
        assistant_id="ast_x",
        phone_number_id="pn_y",
        customer_number="+15305551234",
        variable_values={"company_name": "Acme"},
        voicemail_message="prospect-specific voicemail",
    )
    overrides = cap["body"]["assistantOverrides"]
    assert overrides["variableValues"]["company_name"] == "Acme"
    assert overrides["voicemailMessage"] == "prospect-specific voicemail"


def test_create_call_forwards_first_message(monkeypatch):
    """The per-prospect spoken opener rides under assistantOverrides.firstMessage."""
    cap: dict = {}
    client = _build_client(monkeypatch, capture=cap)
    client.create_call(
        assistant_id="ast_x",
        phone_number_id="pn_y",
        customer_number="+15305551234",
        first_message="Hey, is this Acme? Honestly, this is a cold call...",
    )
    overrides = cap["body"]["assistantOverrides"]
    assert overrides["firstMessage"].startswith("Hey, is this Acme?")
    # opener-only call: no variableValues / voicemail keys when none passed.
    assert "variableValues" not in overrides
    assert "voicemailMessage" not in overrides


def test_create_call_omits_first_message_when_empty(monkeypatch):
    """No firstMessage override when opener is empty -> assistant default stands."""
    cap: dict = {}
    client = _build_client(monkeypatch, capture=cap)
    client.create_call(
        assistant_id="ast_x",
        phone_number_id="pn_y",
        customer_number="+15305551234",
        variable_values={"company_name": "Acme"},
        first_message=None,
    )
    assert "firstMessage" not in cap["body"]["assistantOverrides"]
