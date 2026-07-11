"""Tests for backend.outreach.social_oauth_cli — the operator OAuth entry point.

No network: authorize_url is pure string building; the exchange path is tested
by patching exchange_code at the CLI module level. Secrets are injected via an
env mapping so nothing touches the real environment.
"""
from __future__ import annotations

import json

import pytest

from backend.outreach import social_oauth_cli as cli


_ENV = {
    "SAMUS_LINKEDIN_CLIENT_ID": "cid-123",
    "SAMUS_LINKEDIN_CLIENT_SECRET": "sec-456",
}


def test_authorize_url_builds_consent_url_with_generated_state():
    url, state = cli.authorize_url(
        "linkedin", "https://localhost/cb", env=_ENV,
    )
    assert url.startswith("https://www.linkedin.com/oauth/v2/authorization?")
    assert "client_id=cid-123" in url
    assert state and f"state={state}" in url


def test_authorize_url_honours_supplied_state():
    url, state = cli.authorize_url(
        "linkedin", "https://localhost/cb", state="fixed-state", env=_ENV,
    )
    assert state == "fixed-state"
    assert "state=fixed-state" in url


def test_authorize_url_missing_client_id_raises():
    with pytest.raises(ValueError, match="SAMUS_LINKEDIN_CLIENT_ID"):
        cli.authorize_url("linkedin", "https://localhost/cb", env={})


def test_exchange_delegates_with_env_secrets(monkeypatch):
    captured = {}

    def _fake_exchange(platform, code, client_id, client_secret, redirect_uri):
        captured.update(locals())
        return {"access_token": "tok", "expires_in": 3600}

    monkeypatch.setattr(cli, "exchange_code", _fake_exchange)
    result = cli.exchange("linkedin", "auth-code", "https://localhost/cb", env=_ENV)

    assert result["access_token"] == "tok"
    assert captured["client_id"] == "cid-123"
    assert captured["client_secret"] == "sec-456"
    assert captured["code"] == "auth-code"


def test_exchange_missing_secret_raises():
    with pytest.raises(ValueError, match="SAMUS_LINKEDIN_CLIENT_SECRET"):
        cli.exchange(
            "linkedin", "code", "https://localhost/cb",
            env={"SAMUS_LINKEDIN_CLIENT_ID": "cid"},
        )


# --- main() argparse surface -----------------------------------------------


def test_main_url_prints_state_and_url(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "authorize_url", lambda *a, **k: ("https://consent/url", "st8"),
    )
    rc = cli.main(["url", "--platform", "linkedin", "--redirect-uri", "https://localhost/cb"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "state: st8" in out
    assert "https://consent/url" in out


def test_main_exchange_prints_token_json(monkeypatch, capsys):
    monkeypatch.setattr(cli, "exchange", lambda *a, **k: {"access_token": "tok"})
    rc = cli.main([
        "exchange", "--platform", "linkedin",
        "--code", "c", "--redirect-uri", "https://localhost/cb",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out)["access_token"] == "tok"


def test_main_missing_env_returns_2(monkeypatch, capsys):
    monkeypatch.delenv("SAMUS_LINKEDIN_CLIENT_ID", raising=False)
    rc = cli.main(["url", "--platform", "linkedin", "--redirect-uri", "https://localhost/cb"])
    assert rc == 2
    assert "SAMUS_LINKEDIN_CLIENT_ID" in capsys.readouterr().err


def test_main_rejects_unknown_platform():
    with pytest.raises(SystemExit):
        cli.main(["url", "--platform", "myspace", "--redirect-uri", "x"])
