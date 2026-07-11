"""voice.app FastAPI startup wiring — _run_tunnel_startup happy + degraded paths."""

from __future__ import annotations

from backend.voice import app as voice_app


class _StubSettings:
    def __init__(
        self,
        *,
        ngrok_authtoken="",
        ngrok_reserved_domain="",
        vapi_assistant_id="",
        vapi_api_key="",
        vapi_webhook_secret="",
        vapi_inbound_assistant_id="",
    ):
        self.ngrok_authtoken = ngrok_authtoken
        self.ngrok_reserved_domain = ngrok_reserved_domain
        self.vapi_assistant_id = vapi_assistant_id
        self.vapi_api_key = vapi_api_key
        self.vapi_webhook_secret = vapi_webhook_secret
        # AI Digital Receptionist inbound assistant — _run_tunnel_startup
        # PATCHes it too when set; empty -> skipped.
        self.vapi_inbound_assistant_id = vapi_inbound_assistant_id


def test_run_tunnel_startup_skips_when_no_authtoken(monkeypatch):
    """Most common dev path: no NGROK_AUTHTOKEN -> startup is a no-op."""
    monkeypatch.setattr(voice_app, "get_settings", lambda: _StubSettings(ngrok_authtoken=""))
    forward_called = []
    patch_called = []

    def fake_start(**kw):
        forward_called.append(kw)
        from backend.voice.tunnel import TunnelResult

        return TunnelResult(url=None, error="authtoken_unset")

    def fake_patch(**kw):
        patch_called.append(kw)
        return True, None

    monkeypatch.setattr(voice_app, "start_ngrok_listener", fake_start, raising=False)
    monkeypatch.setattr(voice_app, "patch_vapi_assistant_server_url", fake_patch, raising=False)
    # call the inner helper directly — FastAPI on_event wraps it.
    voice_app._run_tunnel_startup()
    # We don't assert forward_called.empty here because start_ngrok_listener's
    # own "skip if no authtoken" check is the canonical guard; the helper
    # forwards even an empty token and the listener fast-returns. What
    # matters: patch_vapi_assistant_server_url is NOT called (no URL to
    # patch with).
    assert patch_called == []


def test_run_tunnel_startup_patches_vapi_with_webhook_path(monkeypatch):
    """Happy path: tunnel up -> server_url is tunnel_url + '/vapi/webhook'."""
    monkeypatch.setattr(
        voice_app,
        "get_settings",
        lambda: _StubSettings(
            ngrok_authtoken="tok_x",
            vapi_assistant_id="ast_42",
            vapi_api_key="vapi_key",
            vapi_webhook_secret="whsec_test",
        ),
    )

    captured: dict = {}
    from backend.voice import tunnel as tunnel_mod

    def fake_start_ngrok(*, port, authtoken, reserved_domain=""):
        captured["port"] = port
        captured["authtoken"] = authtoken
        return tunnel_mod.TunnelResult(url="https://abc.ngrok-free.app", error=None)

    def fake_patch(**kw):
        captured["patch"] = kw
        return True, None

    monkeypatch.setattr(tunnel_mod, "start_ngrok_listener", fake_start_ngrok)
    monkeypatch.setattr(tunnel_mod, "patch_vapi_assistant_server_url", fake_patch)

    voice_app._run_tunnel_startup()
    # The helper must append the canonical webhook path to the tunnel URL,
    # so the operator doesn't have to remember that detail.
    assert captured["patch"]["server_url"] == "https://abc.ngrok-free.app/vapi/webhook"
    assert captured["patch"]["assistant_id"] == "ast_42"
    assert captured["patch"]["server_url_secret"] == "whsec_test"
    assert captured["patch"]["vapi_api_key"] == "vapi_key"


def test_run_tunnel_startup_does_not_patch_when_tunnel_failed(monkeypatch):
    """Tunnel failure -> no Vapi PATCH attempted (would clobber prod URL with junk)."""
    monkeypatch.setattr(
        voice_app,
        "get_settings",
        lambda: _StubSettings(
            ngrok_authtoken="tok_x",
            vapi_assistant_id="ast_42",
            vapi_api_key="k",
        ),
    )

    from backend.voice import tunnel as tunnel_mod

    patch_called: list = []

    def fake_start_ngrok(**kw):
        return tunnel_mod.TunnelResult(
            url=None,
            error="ngrok_forward_failed: simulated",
        )

    def fake_patch(**kw):
        patch_called.append(kw)
        return True, None

    monkeypatch.setattr(tunnel_mod, "start_ngrok_listener", fake_start_ngrok)
    monkeypatch.setattr(tunnel_mod, "patch_vapi_assistant_server_url", fake_patch)

    voice_app._run_tunnel_startup()
    assert patch_called == []


def test_run_tunnel_startup_appends_path_idempotently(monkeypatch):
    """If somehow the tunnel URL ALREADY ends in /, the join is still correct."""
    monkeypatch.setattr(
        voice_app,
        "get_settings",
        lambda: _StubSettings(
            ngrok_authtoken="tok_x",
            vapi_assistant_id="ast_42",
            vapi_api_key="k",
        ),
    )
    from backend.voice import tunnel as tunnel_mod

    captured: dict = {}

    def fake_start_ngrok(**kw):
        return tunnel_mod.TunnelResult(url="https://abc.ngrok-free.app/", error=None)

    def fake_patch(**kw):
        captured.update(kw)
        return True, None

    monkeypatch.setattr(tunnel_mod, "start_ngrok_listener", fake_start_ngrok)
    monkeypatch.setattr(tunnel_mod, "patch_vapi_assistant_server_url", fake_patch)

    voice_app._run_tunnel_startup()
    # No double slash.
    assert captured["server_url"] == "https://abc.ngrok-free.app/vapi/webhook"
