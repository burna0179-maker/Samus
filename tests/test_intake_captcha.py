"""Intake CAPTCHA verification tests (feat/samus-intake-hardening, Finding 1).

CAPTCHA is opt-in: skipped entirely when no secret is configured, enforced
(fail-closed) when one is. The Turnstile siteverify HTTP call is mocked — no
real network.
"""

from __future__ import annotations

from typing import Any


class _FakeResponse:
    def __init__(
        self, *, status_code: int = 200, json_body: Any = None, raise_on_json: bool = False
    ):
        self.status_code = status_code
        self._json_body = json_body
        self._raise_on_json = raise_on_json

    def json(self):
        if self._raise_on_json:
            raise ValueError("not json")
        return self._json_body


class _FakeHttpClient:
    """Context-manager httpx.Client stub. Records posts, returns a canned response."""

    def __init__(self, response=None, raise_exc: Exception | None = None):
        self._response = response
        self._raise_exc = raise_exc
        self.posts: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, data=None, **kwargs):
        self.posts.append({"url": url, "data": data})
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response


def _patch_httpx(monkeypatch, fake_client):
    import backend.intake.captcha as cap_mod

    monkeypatch.setattr(cap_mod.httpx, "Client", lambda *a, **kw: fake_client)


def _set_captcha_secret(monkeypatch, secret: str):
    if secret:
        monkeypatch.setenv("SAMUS_INTAKE_CAPTCHA_SECRET", secret)
    else:
        monkeypatch.delenv("SAMUS_INTAKE_CAPTCHA_SECRET", raising=False)
    from backend.common.config import reload_settings

    reload_settings()


# ---------------------------------------------------------------------------
# captcha_required — activation gate
# ---------------------------------------------------------------------------


def test_captcha_not_required_when_secret_empty(monkeypatch):
    _set_captcha_secret(monkeypatch, "")
    from backend.intake.captcha import captcha_required

    assert captcha_required() is False


def test_captcha_required_when_secret_set(monkeypatch):
    _set_captcha_secret(monkeypatch, "0x_turnstile_secret")
    from backend.intake.captcha import captcha_required

    assert captcha_required() is True


# ---------------------------------------------------------------------------
# verify_captcha — fail-closed behavior
# ---------------------------------------------------------------------------


def test_verify_captcha_accepts_valid_token(monkeypatch):
    _set_captcha_secret(monkeypatch, "0x_secret")
    fake = _FakeHttpClient(
        response=_FakeResponse(json_body={"success": True}),
    )
    _patch_httpx(monkeypatch, fake)
    from backend.intake.captcha import verify_captcha

    result = verify_captcha("good-token", source_ip="203.0.113.5")
    assert result.ok is True
    # The secret + token + IP were posted to the siteverify endpoint.
    assert fake.posts[0]["data"]["secret"] == "0x_secret"
    assert fake.posts[0]["data"]["response"] == "good-token"
    assert fake.posts[0]["data"]["remoteip"] == "203.0.113.5"


def test_verify_captcha_rejects_invalid_token(monkeypatch):
    _set_captcha_secret(monkeypatch, "0x_secret")
    fake = _FakeHttpClient(
        response=_FakeResponse(
            json_body={"success": False, "error-codes": ["invalid-input-response"]},
        ),
    )
    _patch_httpx(monkeypatch, fake)
    from backend.intake.captcha import verify_captcha

    result = verify_captcha("bad-token")
    assert result.ok is False
    assert "captcha_verification_failed" in result.detail


def test_verify_captcha_rejects_empty_token(monkeypatch):
    _set_captcha_secret(monkeypatch, "0x_secret")
    # No HTTP call should even happen for an empty token.
    fake = _FakeHttpClient(raise_exc=AssertionError("must not call siteverify"))
    _patch_httpx(monkeypatch, fake)
    from backend.intake.captcha import verify_captcha

    result = verify_captcha("")
    assert result.ok is False
    assert result.detail == "captcha_token_missing"
    assert fake.posts == []


def test_verify_captcha_fails_closed_on_transport_error(monkeypatch):
    """A network error rejects the request — CAPTCHA fails CLOSED."""
    import httpx

    _set_captcha_secret(monkeypatch, "0x_secret")
    fake = _FakeHttpClient(raise_exc=httpx.ConnectError("siteverify down"))
    _patch_httpx(monkeypatch, fake)
    from backend.intake.captcha import verify_captcha

    result = verify_captcha("some-token")
    assert result.ok is False
    assert result.detail == "captcha_verify_unreachable"


def test_verify_captcha_fails_closed_on_non_200(monkeypatch):
    _set_captcha_secret(monkeypatch, "0x_secret")
    fake = _FakeHttpClient(response=_FakeResponse(status_code=503, json_body={}))
    _patch_httpx(monkeypatch, fake)
    from backend.intake.captcha import verify_captcha

    result = verify_captcha("some-token")
    assert result.ok is False
    assert "captcha_verify_http_503" in result.detail


def test_verify_captcha_fails_closed_on_non_json_body(monkeypatch):
    _set_captcha_secret(monkeypatch, "0x_secret")
    fake = _FakeHttpClient(
        response=_FakeResponse(json_body=None, raise_on_json=True),
    )
    _patch_httpx(monkeypatch, fake)
    from backend.intake.captcha import verify_captcha

    result = verify_captcha("some-token")
    assert result.ok is False
    assert result.detail == "captcha_verify_bad_response"
