"""voice.tunnel — degraded modes for ngrok startup + Vapi PATCH."""
from __future__ import annotations

import pytest

from backend.voice.tunnel import (
    TunnelResult,
    patch_vapi_assistant_server_url,
    start_ngrok_listener,
)


# ---------------------------------------------------------------------------
# start_ngrok_listener
# ---------------------------------------------------------------------------

def test_start_ngrok_listener_skips_when_authtoken_unset():
    result = start_ngrok_listener(port=8080, authtoken="")
    assert result.url is None
    assert result.error == "authtoken_unset"
    assert result.listener is None


def test_start_ngrok_listener_handles_forward_error(monkeypatch):
    """ngrok.forward() raising must NOT crash the workcell."""
    class _FakeNgrok:
        @staticmethod
        def forward(*a, **kw):
            raise RuntimeError("simulated ngrok edge unreachable")
    import sys
    monkeypatch.setitem(sys.modules, "ngrok", _FakeNgrok())
    result = start_ngrok_listener(port=8080, authtoken="tok_x")
    assert result.url is None
    assert result.error is not None
    assert "ngrok_forward_failed" in result.error
    assert "simulated ngrok edge unreachable" in result.error


def test_start_ngrok_listener_handles_import_error(monkeypatch):
    """If the ngrok package isn't available, degraded mode."""
    import sys
    # Force import to fail by removing + blocking the import.
    monkeypatch.setitem(sys.modules, "ngrok", None)
    result = start_ngrok_listener(port=8080, authtoken="tok_x")
    assert result.url is None
    assert result.error == "ngrok_import_failed"


def test_start_ngrok_listener_returns_url_on_success(monkeypatch):
    class _FakeListener:
        def url(self):
            return "https://abc123.ngrok-free.app"

    class _FakeNgrok:
        @staticmethod
        def forward(*a, **kw):
            assert kw["authtoken"] == "tok_real"
            return _FakeListener()

    import sys
    monkeypatch.setitem(sys.modules, "ngrok", _FakeNgrok())
    result = start_ngrok_listener(port=8080, authtoken="tok_real")
    assert result.url == "https://abc123.ngrok-free.app"
    assert result.error is None


def test_start_ngrok_listener_forwards_reserved_domain(monkeypatch):
    captured: dict = {}

    class _FakeListener:
        def url(self):
            return "https://samus-voice.ngrok.app"

    class _FakeNgrok:
        @staticmethod
        def forward(*a, **kw):
            captured.update(kw)
            captured["positional"] = a
            return _FakeListener()

    import sys
    monkeypatch.setitem(sys.modules, "ngrok", _FakeNgrok())
    result = start_ngrok_listener(
        port=8080, authtoken="tok_x",
        reserved_domain="samus-voice.ngrok.app",
    )
    assert result.url == "https://samus-voice.ngrok.app"
    assert captured["domain"] == "samus-voice.ngrok.app"


# ---------------------------------------------------------------------------
# patch_vapi_assistant_server_url
# ---------------------------------------------------------------------------

class _StubVapiClient:
    def __init__(self, *, raise_exc=None):
        self.calls: list[dict] = []
        self._raise = raise_exc

    def update_assistant(self, *, assistant_id, server_url=None,
                         server_url_secret=None):
        self.calls.append({
            "assistant_id": assistant_id,
            "server_url": server_url,
            "server_url_secret": server_url_secret,
        })
        if self._raise:
            raise self._raise
        return {"id": assistant_id, "server": {"url": server_url}}


def test_patch_vapi_skips_when_assistant_id_unset():
    ok, err = patch_vapi_assistant_server_url(
        assistant_id="",
        server_url="https://x.ngrok-free.app/vapi/webhook",
        vapi_api_key="k",
    )
    assert ok is False
    assert err == "assistant_id_unset"


def test_patch_vapi_skips_when_api_key_unset_and_no_injected_client():
    ok, err = patch_vapi_assistant_server_url(
        assistant_id="ast_x",
        server_url="https://x.ngrok-free.app/vapi/webhook",
        vapi_api_key="",
    )
    assert ok is False
    assert err == "vapi_api_key_unset"


def test_patch_vapi_calls_update_assistant_on_success():
    stub = _StubVapiClient()
    ok, err = patch_vapi_assistant_server_url(
        assistant_id="ast_x",
        server_url="https://x.ngrok-free.app/vapi/webhook",
        server_url_secret="whsec_test",
        vapi_api_key="",  # bypassed because client is injected
        client=stub,
    )
    assert ok is True
    assert err is None
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["assistant_id"] == "ast_x"
    assert call["server_url"].endswith("/vapi/webhook")
    assert call["server_url_secret"] == "whsec_test"


def test_patch_vapi_handles_update_assistant_failure():
    stub = _StubVapiClient(raise_exc=RuntimeError("vapi_http_500: server error"))
    ok, err = patch_vapi_assistant_server_url(
        assistant_id="ast_x",
        server_url="https://x.ngrok-free.app/vapi/webhook",
        vapi_api_key="",
        client=stub,
    )
    assert ok is False
    assert err is not None
    assert "vapi_patch_failed" in err


def test_patch_vapi_omits_secret_when_not_provided():
    """Tunnel-rotation flow: URL changes, secret stays — caller passes empty."""
    stub = _StubVapiClient()
    ok, err = patch_vapi_assistant_server_url(
        assistant_id="ast_x",
        server_url="https://new.ngrok-free.app/vapi/webhook",
        server_url_secret="",  # don't touch the secret
        vapi_api_key="",
        client=stub,
    )
    assert ok is True
    # Our helper translates empty string -> None so VapiClient knows to omit
    # the field entirely.
    assert stub.calls[0]["server_url_secret"] is None
