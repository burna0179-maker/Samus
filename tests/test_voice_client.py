"""VapiClient — httpx mocked at the module level."""
from __future__ import annotations

import json

import httpx
import pytest


class _FakeHttpx:
    """Per-module httpx stub. Falls through to real httpx for exception classes."""
    def __init__(self, client_cls):
        self.Client = client_cls

    def __getattr__(self, name):
        return getattr(httpx, name)


def _build_client(monkeypatch, *, status: int = 200,
                  body: dict | list | None = None,
                  raise_exc: Exception | None = None,
                  capture: dict | None = None):
    """Patch backend.voice.client.httpx with a controllable fake."""

    class _Resp:
        def __init__(self):
            self.status_code = status
            self._body = body if body is not None else {}
            self.text = json.dumps(self._body) if self._body != {} else ""
            self.content = self.text.encode("utf-8")

        def json(self):
            return self._body

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, headers=None, params=None, json=None):
            if capture is not None:
                capture["method"] = method
                capture["url"] = url
                capture["headers"] = headers or {}
                capture["params"] = params or {}
                capture["json"] = json
            if raise_exc is not None:
                raise raise_exc
            return _Resp()

    import backend.voice.client as mod
    monkeypatch.setattr(mod, "httpx", _FakeHttpx(_Client))
    return mod.VapiClient(api_key="vapi_unit_key")


def test_client_rejects_empty_key():
    from backend.voice.client import VapiClient
    with pytest.raises(ValueError):
        VapiClient(api_key="")


def test_create_call_posts_expected_body(monkeypatch):
    capture: dict = {}
    body = {"id": "call_123", "status": "queued",
            "assistantId": "asst_x", "phoneNumberId": "phn_y"}
    client = _build_client(monkeypatch, body=body, capture=capture)
    call = client.create_call(
        assistant_id="asst_x",
        phone_number_id="phn_y",
        customer_number="+15555550100",
        customer_name="Test Co",
        metadata={"campaign": "morgan_phase1"},
    )
    assert call.id == "call_123"
    assert call.status == "queued"
    assert capture["method"] == "POST"
    assert capture["url"].endswith("/call")
    payload = capture["json"]
    assert payload["assistantId"] == "asst_x"
    assert payload["phoneNumberId"] == "phn_y"
    assert payload["customer"] == {"number": "+15555550100", "name": "Test Co"}
    assert payload["metadata"] == {"campaign": "morgan_phase1"}
    assert capture["headers"]["Authorization"] == "Bearer vapi_unit_key"


def test_create_call_omits_optional_fields(monkeypatch):
    capture: dict = {}
    body = {"id": "call_abc"}
    client = _build_client(monkeypatch, body=body, capture=capture)
    client.create_call(
        assistant_id="asst_x",
        phone_number_id="phn_y",
        customer_number="+15555550100",
    )
    payload = capture["json"]
    assert "name" not in payload["customer"]
    assert "metadata" not in payload


def test_get_call_parses_typed(monkeypatch):
    body = {"id": "call_xyz", "status": "ended",
            "endedReason": "customer-ended-call",
            "transcript": "...", "cost": 0.12}
    client = _build_client(monkeypatch, body=body)
    call = client.get_call("call_xyz")
    assert call.id == "call_xyz"
    assert call.endedReason == "customer-ended-call"
    assert call.cost == pytest.approx(0.12)


def test_get_call_requires_id():
    from backend.voice.client import VapiClient
    c = VapiClient(api_key="vapi_unit_key")
    with pytest.raises(ValueError):
        c.get_call("")


def test_list_calls_handles_bare_list(monkeypatch):
    """Vapi returns a bare JSON array; the client wraps it as {data: [...]}.

    Verified by feeding a list body and asserting both rows materialize.
    """
    body = [
        {"id": "c1", "status": "queued"},
        {"id": "c2", "status": "in-progress"},
    ]
    client = _build_client(monkeypatch, body=body)
    calls = client.list_calls(limit=2)
    assert [c.id for c in calls] == ["c1", "c2"]


def test_list_calls_clamps_limit(monkeypatch):
    capture: dict = {}
    client = _build_client(monkeypatch, body={"data": []}, capture=capture)
    client.list_calls(limit=500)
    assert capture["params"]["limit"] == 100
    client.list_calls(limit=0)
    assert capture["params"]["limit"] == 1


def test_list_calls_skips_malformed_row(monkeypatch):
    body = [
        {"id": "ok"},
        {"no_id_field": True},
    ]
    client = _build_client(monkeypatch, body=body)
    calls = client.list_calls()
    assert len(calls) == 1
    assert calls[0].id == "ok"


def test_call_with_blank_customer_is_kept(monkeypatch):
    """Vapi returns customer as '' on some rows — coerce to None, keep the row."""
    body = [
        {"id": "c_blank", "status": "ended", "customer": ""},
        {"id": "c_ok", "customer": {"number": "+15305551234"}},
    ]
    client = _build_client(monkeypatch, body=body)
    calls = client.list_calls()
    assert [c.id for c in calls] == ["c_blank", "c_ok"]
    assert calls[0].customer is None
    assert calls[1].customer.number == "+15305551234"


def test_list_assistants(monkeypatch):
    body = {"data": [{"id": "asst_1", "name": "Morgan"}]}
    client = _build_client(monkeypatch, body=body)
    rows = client.list_assistants()
    assert len(rows) == 1
    assert rows[0].name == "Morgan"


def test_http_error_raises_vapi_error(monkeypatch):
    from backend.voice.client import VapiError
    body = {"message": "Unauthorized", "error": "invalid_api_key"}
    client = _build_client(monkeypatch, status=401, body=body)
    with pytest.raises(VapiError) as ei:
        client.get_call("call_x")
    assert "vapi_http_401" in str(ei.value)
    assert "Unauthorized" in str(ei.value)


def test_transport_error_raises_vapi_error(monkeypatch):
    from backend.voice.client import VapiError
    client = _build_client(monkeypatch, raise_exc=httpx.ConnectError("down"))
    with pytest.raises(VapiError) as ei:
        client.get_call("call_x")
    assert "vapi_transport_error" in str(ei.value)


def test_path_must_start_with_slash():
    from backend.voice.client import VapiClient
    c = VapiClient(api_key="vapi_unit_key")
    with pytest.raises(ValueError):
        c._get("call")  # missing leading slash


def test_create_phone_number_vapi_default(monkeypatch):
    capture: dict = {}
    body = {"id": "phn_v", "number": "+15305550001", "assistantId": "asst_x"}
    client = _build_client(monkeypatch, body=body, capture=capture)
    pn = client.create_phone_number(assistant_id="asst_x", name="Lab", area_code="530")
    assert pn.id == "phn_v"
    payload = capture["json"]
    assert payload["provider"] == "vapi"
    assert payload["assistantId"] == "asst_x"
    assert payload["numberDesiredAreaCode"] == "530"
    assert payload["name"] == "Lab"
    # Twilio-only fields must never leak into a vapi-provider buy.
    assert "number" not in payload
    assert "twilioAccountSid" not in payload


def test_import_twilio_number_posts_camelcase_fields(monkeypatch):
    capture: dict = {}
    body = {"id": "phn_t", "number": "+15305551234", "assistantId": "asst_x"}
    client = _build_client(monkeypatch, body=body, capture=capture)
    pn = client.create_phone_number(
        assistant_id="asst_x", name="Main Line", provider="twilio",
        number="+15305551234", twilio_account_sid="ACxxx",
        twilio_auth_token="tok_secret", sms_enabled=True,
    )
    assert pn.id == "phn_t"
    payload = capture["json"]
    assert payload["provider"] == "twilio"
    assert payload["number"] == "+15305551234"
    assert payload["twilioAccountSid"] == "ACxxx"
    assert payload["twilioAuthToken"] == "tok_secret"
    assert payload["smsEnabled"] is True
    assert payload["assistantId"] == "asst_x"
    # area-code knob is vapi-only; must not appear on a twilio import.
    assert "numberDesiredAreaCode" not in payload


def test_import_twilio_number_with_api_key(monkeypatch):
    """API-key auth posts twilioApiKey/twilioApiSecret and NOT twilioAuthToken."""
    capture: dict = {}
    body = {"id": "phn_t3", "number": "+15005550006"}
    client = _build_client(monkeypatch, body=body, capture=capture)
    client.create_phone_number(
        assistant_id="asst_x", provider="twilio",
        number="+15005550006", twilio_account_sid="AC0b77",
        twilio_api_key="SKabc", twilio_api_secret="sek_secret",
    )
    payload = capture["json"]
    assert payload["twilioAccountSid"] == "AC0b77"
    assert payload["twilioApiKey"] == "SKabc"
    assert payload["twilioApiSecret"] == "sek_secret"
    assert "twilioAuthToken" not in payload


def test_import_twilio_api_key_takes_precedence_over_auth_token(monkeypatch):
    capture: dict = {}
    client = _build_client(monkeypatch, body={"id": "phn_t4"}, capture=capture)
    client.create_phone_number(
        assistant_id="asst_x", provider="twilio", number="+15005550006",
        twilio_account_sid="AC0b77", twilio_auth_token="tok_x",
        twilio_api_key="SKabc", twilio_api_secret="sek",
    )
    payload = capture["json"]
    assert payload["twilioApiKey"] == "SKabc"
    assert "twilioAuthToken" not in payload


def test_import_twilio_rejects_sk_as_account_sid():
    """The exact mis-seed we hit live: an SK API-Key SID in the account slot."""
    from backend.voice.client import VapiClient
    c = VapiClient(api_key="vapi_unit_key")
    with pytest.raises(ValueError) as ei:
        c.create_phone_number(
            assistant_id="asst_x", provider="twilio", number="+15005550006",
            twilio_account_sid="SK00000000000000000000000000000000",
            twilio_auth_token="tok",
        )
    msg = str(ei.value)
    assert "must be the Account SID" in msg
    assert "AC" in msg


def test_import_twilio_fails_closed_without_any_auth():
    """AC sid present but neither auth token nor api-key pair -> raise."""
    from backend.voice.client import VapiClient
    c = VapiClient(api_key="vapi_unit_key")
    with pytest.raises(ValueError) as ei:
        c.create_phone_number(
            assistant_id="asst_x", provider="twilio", number="+15005550006",
            twilio_account_sid="AC0b77",  # no auth of any kind
        )
    assert "twilio_api_key" in str(ei.value) or "twilio_auth_token" in str(ei.value)


def test_import_twilio_number_omits_sms_when_unset(monkeypatch):
    capture: dict = {}
    body = {"id": "phn_t2", "number": "+15305555678"}
    client = _build_client(monkeypatch, body=body, capture=capture)
    client.create_phone_number(
        assistant_id="asst_x", provider="twilio",
        number="+15305555678", twilio_account_sid="ACyyy",
        twilio_auth_token="tok2",  # sms_enabled left None
    )
    assert "smsEnabled" not in capture["json"]


# ---------------------------------------------------------------------------
# patch_assistant_config — fetch-and-merge model PATCH (Vapi model.provider fix)
# ---------------------------------------------------------------------------

def _build_routing_client(monkeypatch, *, get_body: dict, record: dict):
    """Fake httpx that routes by METHOD: GETs return ``get_body``; every
    request (incl. the PATCH) is appended to ``record['requests']``.

    Needed because patch_assistant_config(system_prompt=...) now issues a GET
    (fetch the live model) followed by a PATCH — a single last-write capture
    can't see both. Non-GET requests echo an empty body so the client's typed
    parse (unused here) stays happy.
    """
    record.setdefault("requests", [])

    class _Resp:
        def __init__(self, payload):
            self.status_code = 200
            self._body = payload
            self.text = json.dumps(payload) if payload else ""
            self.content = self.text.encode("utf-8")

        def json(self):
            return self._body

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, headers=None, params=None, json=None):
            record["requests"].append(
                {"method": method, "url": url, "json": json, "params": params}
            )
            if method == "GET":
                return _Resp(get_body)
            return _Resp({"id": "asst_x", "ok": True})

    import backend.voice.client as mod
    monkeypatch.setattr(mod, "httpx", _FakeHttpx(_Client))
    return mod.VapiClient(api_key="vapi_unit_key")


_LIVE_MODEL = {
    "provider": "anthropic",
    "model": "claude-haiku-4-5-20250101",
    "toolIds": ["tool_a", "tool_b"],
    "knowledgeBase": {"provider": "canonical", "fileIds": ["kb1"]},
    "messages": [{"role": "system", "content": "OLD PROMPT"}],
}


def test_patch_system_prompt_fetch_merges_full_model(monkeypatch):
    """system_prompt patch GETs the live model then PATCHes a FULL model
    object: provider/model/toolIds/knowledgeBase preserved, only messages
    swapped to the new system content."""
    record: dict = {}
    client = _build_routing_client(
        monkeypatch, get_body={"id": "asst_x", "model": dict(_LIVE_MODEL)},
        record=record,
    )
    client.patch_assistant_config("asst_x", system_prompt="NEW")

    reqs = record["requests"]
    # Exactly a GET (fetch) then a PATCH (write).
    assert [r["method"] for r in reqs] == ["GET", "PATCH"]
    assert reqs[0]["url"].endswith("/assistant/asst_x")
    patch_body = reqs[1]["json"]
    model = patch_body["model"]
    # Preserved sub-fields.
    assert model["provider"] == "anthropic"
    assert model["model"] == "claude-haiku-4-5-20250101"
    assert model["toolIds"] == ["tool_a", "tool_b"]
    assert model["knowledgeBase"] == {"provider": "canonical", "fileIds": ["kb1"]}
    # Only messages swapped.
    assert model["messages"] == [{"role": "system", "content": "NEW"}]
    # Nothing unrelated leaked into the patch.
    assert set(patch_body.keys()) == {"model"}


def test_patch_first_message_only_no_model_no_get(monkeypatch):
    """firstMessage-only patch touches no model and issues NO fetch GET."""
    record: dict = {}
    client = _build_routing_client(
        monkeypatch, get_body={"id": "asst_x", "model": dict(_LIVE_MODEL)},
        record=record,
    )
    client.patch_assistant_config("asst_x", first_message="Hi there")

    reqs = record["requests"]
    assert [r["method"] for r in reqs] == ["PATCH"]  # no spurious GET
    body = reqs[0]["json"]
    assert body == {"firstMessage": "Hi there"}
    assert "model" not in body


def test_patch_voice_only_no_model_no_get(monkeypatch):
    """voice-only patch touches no model and issues NO fetch GET."""
    record: dict = {}
    client = _build_routing_client(
        monkeypatch, get_body={"id": "asst_x", "model": dict(_LIVE_MODEL)},
        record=record,
    )
    client.patch_assistant_config(
        "asst_x", voice_speed=1.1, voice_similarity_boost=0.8,
    )

    reqs = record["requests"]
    assert [r["method"] for r in reqs] == ["PATCH"]  # no spurious GET
    body = reqs[0]["json"]
    assert body == {"voice": {"speed": 1.1, "similarityBoost": 0.8}}
    assert "model" not in body


def test_patch_system_prompt_with_missing_live_model_falls_back(monkeypatch):
    """If the live assistant has no model object, the merge degrades to a
    messages-only body (still lets Vapi surface its own error) rather than
    crashing on a None model."""
    record: dict = {}
    client = _build_routing_client(
        monkeypatch, get_body={"id": "asst_x"},  # no 'model'
        record=record,
    )
    client.patch_assistant_config("asst_x", system_prompt="NEW")
    reqs = record["requests"]
    assert [r["method"] for r in reqs] == ["GET", "PATCH"]
    assert reqs[1]["json"]["model"] == {
        "messages": [{"role": "system", "content": "NEW"}]
    }


def test_patch_system_prompt_and_voice_combined(monkeypatch):
    """Combined system_prompt + voice patch: model fetch-merged AND voice
    included in the same PATCH (one GET, one PATCH)."""
    record: dict = {}
    client = _build_routing_client(
        monkeypatch, get_body={"id": "asst_x", "model": dict(_LIVE_MODEL)},
        record=record,
    )
    client.patch_assistant_config(
        "asst_x", system_prompt="NEW", voice_speed=0.95,
    )
    reqs = record["requests"]
    assert [r["method"] for r in reqs] == ["GET", "PATCH"]
    body = reqs[1]["json"]
    assert body["model"]["provider"] == "anthropic"
    assert body["model"]["messages"] == [{"role": "system", "content": "NEW"}]
    assert body["voice"] == {"speed": 0.95}


def test_patch_requires_assistant_id():
    from backend.voice.client import VapiClient
    c = VapiClient(api_key="vapi_unit_key")
    with pytest.raises(ValueError):
        c.patch_assistant_config("", system_prompt="NEW")


def test_patch_requires_at_least_one_field(monkeypatch):
    """No fields → ValueError, and NO network request (no fetch GET)."""
    record: dict = {}
    client = _build_routing_client(
        monkeypatch, get_body={"id": "asst_x", "model": dict(_LIVE_MODEL)},
        record=record,
    )
    with pytest.raises(ValueError):
        client.patch_assistant_config("asst_x")
    assert record["requests"] == []


def test_mid_session_monitor_path_builds_provider_bearing_body(monkeypatch):
    """call_session_monitor._patch_vapi_system_prompt passes system_prompt only;
    with fetch-merge the resulting PATCH carries a valid provider-bearing model
    object (would no longer 400 if armed)."""
    import backend.voice.call_session_monitor as csm

    record: dict = {}
    fake = _build_routing_client(
        monkeypatch, get_body={"id": "asst_x", "model": dict(_LIVE_MODEL)},
        record=record,
    )

    # Set the two settings the monitor reads on the REAL cached settings object
    # (don't replace get_settings itself — that would break the conftest
    # reload_settings() teardown, which calls get_settings.cache_clear()).
    from backend.common.config import get_settings
    real_settings = get_settings()
    monkeypatch.setattr(real_settings, "vapi_assistant_id", "asst_x", raising=False)
    monkeypatch.setattr(real_settings, "vapi_api_key", "vapi_unit_key", raising=False)
    # The monitor imports VapiClient inside the function via `from .client import
    # VapiClient`; patch it at the source module so the fake is picked up.
    import backend.voice.client as client_mod
    monkeypatch.setattr(client_mod, "VapiClient", lambda api_key: fake)

    adj = csm.SessionAdjustment(
        generated_ts="2026-07-02T00:00:00Z",
        trigger="STREAK",
        trigger_detail="3 consecutive",
        synthesis="Opener lands as a pitch.",
        recommendations=["Lead with a local observation.", "Ask permission early."],
    )
    csm._patch_vapi_system_prompt(adj)

    methods = [r["method"] for r in record["requests"]]
    # get_assistant (read current prompt) + get_assistant (fetch-merge) + PATCH.
    assert methods.count("PATCH") == 1
    patch_req = next(r for r in record["requests"] if r["method"] == "PATCH")
    model = patch_req["json"]["model"]
    assert model["provider"] == "anthropic"  # valid — no 400
    assert model["model"] == "claude-haiku-4-5-20250101"
    # The patched prompt carries the mid-session directive block.
    content = model["messages"][0]["content"]
    assert "MID-SESSION ADJUSTMENT" in content
    assert "Lead with a local observation." in content