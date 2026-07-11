"""VapiClient.update_assistant — PATCH /assistant/{id} body shape."""

from __future__ import annotations

import httpx
import pytest


class _FakeHttpx:
    def __init__(self, client_cls):
        self.Client = client_cls

    def __getattr__(self, name):
        return getattr(httpx, name)


def _build_client(monkeypatch, *, capture: dict, body: dict | None = None, status: int = 200):
    class _Resp:
        def __init__(self):
            self.status_code = status
            self._body = body or {"id": "ast_x", "server": {"url": "https://x"}}
            self.text = '{"id":"ast_x"}'
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
            capture["method"] = method
            capture["url"] = url
            capture["body"] = json
            return _Resp()

    import backend.voice.client as mod

    monkeypatch.setattr(mod, "httpx", _FakeHttpx(_Client))
    return mod.VapiClient(api_key="k")


def test_update_assistant_sends_patch_with_server_object(monkeypatch):
    cap: dict = {}
    client = _build_client(monkeypatch, capture=cap)
    client.update_assistant(
        assistant_id="ast_42",
        server_url="https://abc.ngrok-free.app/vapi/webhook",
        server_url_secret="whsec_test",
    )
    assert cap["method"] == "PATCH"
    assert cap["url"].endswith("/assistant/ast_42")
    assert cap["body"] == {
        "server": {
            "url": "https://abc.ngrok-free.app/vapi/webhook",
            "secret": "whsec_test",
        },
    }


def test_update_assistant_omits_secret_when_none(monkeypatch):
    """Rotating only the URL must NOT clobber the existing secret server-side."""
    cap: dict = {}
    client = _build_client(monkeypatch, capture=cap)
    client.update_assistant(
        assistant_id="ast_42",
        server_url="https://abc.ngrok-free.app/vapi/webhook",
        server_url_secret=None,
    )
    assert cap["body"] == {
        "server": {"url": "https://abc.ngrok-free.app/vapi/webhook"},
    }


def test_update_assistant_omits_url_when_none(monkeypatch):
    """Rotating only the secret keeps the URL intact."""
    cap: dict = {}
    client = _build_client(monkeypatch, capture=cap)
    client.update_assistant(
        assistant_id="ast_42",
        server_url=None,
        server_url_secret="whsec_new",
    )
    assert cap["body"] == {"server": {"secret": "whsec_new"}}


def test_update_assistant_rejects_empty_assistant_id(monkeypatch):
    cap: dict = {}
    client = _build_client(monkeypatch, capture=cap)
    with pytest.raises(ValueError, match="non-empty assistant_id"):
        client.update_assistant(assistant_id="", server_url="https://x")


def test_update_assistant_rejects_no_fields(monkeypatch):
    cap: dict = {}
    client = _build_client(monkeypatch, capture=cap)
    with pytest.raises(ValueError, match="at least one of"):
        client.update_assistant(assistant_id="ast_x")


def test_update_assistant_raises_on_vapi_http_error(monkeypatch):
    """Vapi 5xx must propagate as VapiError so the caller can degrade."""
    import backend.voice.client as mod

    cap: dict = {}
    client = _build_client(
        monkeypatch,
        capture=cap,
        status=500,
        body={"message": "internal error"},
    )
    with pytest.raises(mod.VapiError) as ei:
        client.update_assistant(
            assistant_id="ast_x",
            server_url="https://x.ngrok-free.app",
        )
    assert "500" in str(ei.value)
