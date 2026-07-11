"""Live-wiring tests for the social adapter (LinkedIn + Facebook) + OAuth helpers.

The live network path in ``social_adapter`` is DORMANT by default
(``SAMUS_SOCIAL_DRY_RUN`` defaults true). These tests exercise the live path
explicitly with ``SAMUS_SOCIAL_DRY_RUN=false`` and a MOCKED httpx client — no
real network call is ever made. Every test asserts either that no HTTP was
attempted (fail-closed refusals) or that the mocked response was parsed
correctly.

Mirrors the fake-httpx-Client pattern in ``test_common_http_client_sync.py``.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import httpx
import pytest


# ---------------------------------------------------------------------------
# fake httpx client + reload helpers
# ---------------------------------------------------------------------------


class _FakeClient:
    """Records every .post() call. Returns canned responses in sequence.

    Accepts the kwargs both senders use: LinkedIn posts ``json=`` + ``headers=``,
    Facebook posts ``data=``.
    """

    def __init__(self, responses=None, raise_exc=None):
        # responses: list of httpx.Response served in order (last repeats)
        self._responses = list(responses or [])
        self._raise_exc = raise_exc
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, *, json=None, data=None, headers=None):
        self.calls.append(
            {"url": url, "json": json, "data": data, "headers": dict(headers or {})}
        )
        if self._raise_exc is not None:
            raise self._raise_exc
        if not self._responses:
            raise AssertionError("FakeClient.post called with no canned response")
        if len(self._responses) == 1:
            return self._responses[0]
        return self._responses.pop(0)


def _patch_client(monkeypatch, mod, fake):
    """Replace httpx.Client inside the adapter module with a builder that
    yields ``fake``."""

    class _Builder:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return fake

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mod.httpx, "Client", _Builder)


def _no_sleep(monkeypatch, mod):
    """Neutralize backoff so rate-limit retries don't slow the test."""
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_kw: None)


def _reload_live(
    monkeypatch,
    *,
    dry_run="false",
    ledger: Path | None = None,
    linkedin_token="",
    linkedin_urn="",
    facebook_token="",
    facebook_page_id="",
):
    """Reload social_adapter with controlled env so module constants re-read."""
    monkeypatch.setenv("SAMUS_SOCIAL_DRY_RUN", dry_run)
    if ledger is not None:
        monkeypatch.setenv("SAMUS_SOCIAL_LEDGER_PATH", str(ledger))
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", linkedin_token)
    monkeypatch.setenv("LINKEDIN_AUTHOR_URN", linkedin_urn)
    monkeypatch.setenv("FACEBOOK_PAGE_TOKEN", facebook_token)
    monkeypatch.setenv("FACEBOOK_PAGE_ID", facebook_page_id)
    import backend.outreach.social_adapter as mod
    importlib.reload(mod)
    return mod


def _read_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(l)
        for l in path.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]


# A valid operator stake sentence (passes G1; not in the banned-phrase list).
_STAKE = "Met Priya at the Sutter County mixer Thursday and promised this update."


# ---------------------------------------------------------------------------
# Dormancy: dry-run default makes the live path unreachable
# ---------------------------------------------------------------------------


def test_dry_run_default_makes_no_http_call(monkeypatch, tmp_path):
    """With DRY_RUN at its default (true), send_post returns a dry_run result
    and never constructs an httpx client."""
    ledger = tmp_path / "p.jsonl"
    mod = _reload_live(
        monkeypatch, dry_run="true", ledger=ledger,
        linkedin_token="tok", linkedin_urn="urn:li:person:x",
    )
    # If any HTTP is attempted this fake raises (no canned responses).
    fake = _FakeClient(responses=[])
    _patch_client(monkeypatch, mod, fake)

    result = mod.send_post(
        mod.SocialPost(platform="linkedin", body="hi", stake_sentence=_STAKE)
    )
    assert result.sent is True
    assert result.dry_run is True
    assert fake.calls == []  # no HTTP attempted


# ---------------------------------------------------------------------------
# Fail-closed: missing creds with DRY_RUN off -> token_unset, no HTTP
# ---------------------------------------------------------------------------


def test_live_missing_linkedin_token_fails_closed_no_http(monkeypatch, tmp_path):
    mod = _reload_live(
        monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl",
        linkedin_token="", linkedin_urn="urn:li:person:x",
    )
    fake = _FakeClient(responses=[])
    _patch_client(monkeypatch, mod, fake)

    result = mod.send_post(
        mod.SocialPost(platform="linkedin", body="hi", stake_sentence=_STAKE)
    )
    assert result.sent is False
    assert result.error == "linkedin_token_unset"
    assert fake.calls == []


def test_live_missing_facebook_token_fails_closed_no_http(monkeypatch, tmp_path):
    mod = _reload_live(
        monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl",
        facebook_token="", facebook_page_id="123",
    )
    fake = _FakeClient(responses=[])
    _patch_client(monkeypatch, mod, fake)

    result = mod.send_post(
        mod.SocialPost(platform="facebook", body="hi", stake_sentence=_STAKE)
    )
    assert result.sent is False
    assert result.error == "facebook_token_unset"
    assert fake.calls == []


# ---------------------------------------------------------------------------
# G1: missing stake sentence -> refused before token check / HTTP
# ---------------------------------------------------------------------------


def test_live_missing_stake_sentence_refused_no_http(monkeypatch, tmp_path):
    mod = _reload_live(
        monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl",
        linkedin_token="tok", linkedin_urn="urn:li:person:x",
    )
    fake = _FakeClient(responses=[])
    _patch_client(monkeypatch, mod, fake)

    result = mod.send_post(
        mod.SocialPost(platform="linkedin", body="hi", stake_sentence="")
    )
    assert result.sent is False
    assert result.error == "stake_sentence_required"
    assert fake.calls == []


# ---------------------------------------------------------------------------
# G2: moderation block -> refused before HTTP
# ---------------------------------------------------------------------------


def test_live_moderation_block_banned_phrase_no_http(monkeypatch, tmp_path):
    mod = _reload_live(
        monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl",
        linkedin_token="tok", linkedin_urn="urn:li:person:x",
    )
    fake = _FakeClient(responses=[])
    _patch_client(monkeypatch, mod, fake)

    # "synergy" is in the shared banned-phrase list.
    post = mod.SocialPost(
        platform="linkedin",
        body="Let's leverage synergy across the ecosystem.",
        stake_sentence=_STAKE,
    )
    result = mod.send_post(post)
    assert result.sent is False
    assert result.error.startswith("moderation_blocked:")
    assert fake.calls == []


def test_live_moderation_block_prohibited_term_no_http(monkeypatch, tmp_path):
    mod = _reload_live(
        monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl",
        facebook_token="tok", facebook_page_id="123",
    )
    fake = _FakeClient(responses=[])
    _patch_client(monkeypatch, mod, fake)

    post = mod.SocialPost(
        platform="facebook",
        body="Guaranteed returns on this miracle cure!",
        stake_sentence=_STAKE,
    )
    result = mod.send_post(post)
    assert result.sent is False
    assert result.error.startswith("moderation_blocked:")
    assert fake.calls == []


def test_moderate_post_accepts_clean_content():
    from backend.outreach.social_adapter import SocialPost, moderate_post

    ok, reason = moderate_post(
        SocialPost(platform="linkedin", body="A clean professional update.",
                   stake_sentence=_STAKE)
    )
    assert ok is True
    assert reason == ""


# ---------------------------------------------------------------------------
# Happy path: LinkedIn 201 -> sent=True + post_id from x-restli-id header
# ---------------------------------------------------------------------------


def test_live_linkedin_happy_path_parses_post_id(monkeypatch, tmp_path):
    ledger = tmp_path / "p.jsonl"
    mod = _reload_live(
        monkeypatch, dry_run="false", ledger=ledger,
        linkedin_token="tok", linkedin_urn="urn:li:person:abc",
    )
    resp = httpx.Response(
        201,
        headers={"x-restli-id": "urn:li:share:6789"},
        json={"id": "urn:li:share:6789"},
        request=httpx.Request("POST", "https://api.linkedin.com/v2/ugcPosts"),
    )
    fake = _FakeClient(responses=[resp])
    _patch_client(monkeypatch, mod, fake)

    post = mod.SocialPost(
        platform="linkedin", body="Real post", link="https://ex.com",
        stake_sentence=_STAKE,
    )
    result = mod.send_post(post)

    assert result.sent is True
    assert result.post_id == "urn:li:share:6789"
    assert result.dry_run is False
    # One HTTP call to the UGC endpoint, with a Bearer auth header.
    assert len(fake.calls) == 1
    assert fake.calls[0]["url"].endswith("/v2/ugcPosts")
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer tok"
    assert fake.calls[0]["json"]["author"] == "urn:li:person:abc"
    # Ledger recorded the successful send.
    records = _read_ledger(ledger)
    assert records and records[-1]["sent"] is True


# ---------------------------------------------------------------------------
# Happy path: Facebook 200 -> sent=True + post_id from JSON id
# ---------------------------------------------------------------------------


def test_live_facebook_happy_path_parses_post_id(monkeypatch, tmp_path):
    mod = _reload_live(
        monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl",
        facebook_token="pagetok", facebook_page_id="999",
    )
    resp = httpx.Response(
        200,
        json={"id": "999_12345"},
        request=httpx.Request("POST", "https://graph.facebook.com/v19.0/999/feed"),
    )
    fake = _FakeClient(responses=[resp])
    _patch_client(monkeypatch, mod, fake)

    post = mod.SocialPost(platform="facebook", body="FB real post", stake_sentence=_STAKE)
    result = mod.send_post(post)

    assert result.sent is True
    assert result.post_id == "999_12345"
    assert len(fake.calls) == 1
    assert "/999/feed" in fake.calls[0]["url"]
    assert fake.calls[0]["data"]["access_token"] == "pagetok"
    assert fake.calls[0]["data"]["message"] == "FB real post"


# ---------------------------------------------------------------------------
# Rate-limit: LinkedIn 429 -> linkedin_rate_limited after bounded retries
# ---------------------------------------------------------------------------


def test_live_linkedin_429_returns_rate_limited(monkeypatch, tmp_path):
    mod = _reload_live(
        monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl",
        linkedin_token="tok", linkedin_urn="urn:li:person:abc",
    )
    _no_sleep(monkeypatch, mod)
    req = httpx.Request("POST", "https://api.linkedin.com/v2/ugcPosts")
    # Always 429 — exhausts the bounded retries.
    resp = httpx.Response(429, text="rate limited", request=req)
    fake = _FakeClient(responses=[resp])
    _patch_client(monkeypatch, mod, fake)

    result = mod.send_post(
        mod.SocialPost(platform="linkedin", body="x", stake_sentence=_STAKE)
    )
    assert result.sent is False
    assert result.error == "linkedin_rate_limited"
    # Initial attempt + bounded retries (max 2) = 3 calls, capped (no hang).
    assert len(fake.calls) == mod._RATE_LIMIT_MAX_RETRIES + 1


def test_live_linkedin_429_then_success_retries(monkeypatch, tmp_path):
    mod = _reload_live(
        monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl",
        linkedin_token="tok", linkedin_urn="urn:li:person:abc",
    )
    _no_sleep(monkeypatch, mod)
    req = httpx.Request("POST", "https://api.linkedin.com/v2/ugcPosts")
    fake = _FakeClient(responses=[
        httpx.Response(429, text="slow down", request=req),
        httpx.Response(201, headers={"x-restli-id": "urn:li:share:1"}, request=req),
    ])
    _patch_client(monkeypatch, mod, fake)

    result = mod.send_post(
        mod.SocialPost(platform="linkedin", body="x", stake_sentence=_STAKE)
    )
    assert result.sent is True
    assert result.post_id == "urn:li:share:1"
    assert len(fake.calls) == 2


# ---------------------------------------------------------------------------
# Rate-limit: Facebook error code 613 -> facebook_rate_limited
# ---------------------------------------------------------------------------


def test_live_facebook_code_613_returns_rate_limited(monkeypatch, tmp_path):
    mod = _reload_live(
        monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl",
        facebook_token="pagetok", facebook_page_id="999",
    )
    _no_sleep(monkeypatch, mod)
    req = httpx.Request("POST", "https://graph.facebook.com/v19.0/999/feed")
    resp = httpx.Response(
        400, json={"error": {"code": 613, "message": "Calls over rate limit"}},
        request=req,
    )
    fake = _FakeClient(responses=[resp])
    _patch_client(monkeypatch, mod, fake)

    result = mod.send_post(
        mod.SocialPost(platform="facebook", body="x", stake_sentence=_STAKE)
    )
    assert result.sent is False
    assert result.error == "facebook_rate_limited"
    assert len(fake.calls) == mod._RATE_LIMIT_MAX_RETRIES + 1


def test_live_facebook_code_32_returns_rate_limited(monkeypatch, tmp_path):
    mod = _reload_live(
        monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl",
        facebook_token="pagetok", facebook_page_id="999",
    )
    _no_sleep(monkeypatch, mod)
    req = httpx.Request("POST", "https://graph.facebook.com/v19.0/999/feed")
    resp = httpx.Response(
        200, json={"error": {"code": 32, "message": "Page rate limit"}}, request=req,
    )
    fake = _FakeClient(responses=[resp])
    _patch_client(monkeypatch, mod, fake)

    result = mod.send_post(
        mod.SocialPost(platform="facebook", body="x", stake_sentence=_STAKE)
    )
    assert result.sent is False
    assert result.error == "facebook_rate_limited"


# ---------------------------------------------------------------------------
# Transport error -> clean *_send_failed result, no raise
# ---------------------------------------------------------------------------


def test_live_linkedin_transport_error_no_raise(monkeypatch, tmp_path):
    mod = _reload_live(
        monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl",
        linkedin_token="tok", linkedin_urn="urn:li:person:abc",
    )
    fake = _FakeClient(raise_exc=httpx.ConnectError("boom"))
    _patch_client(monkeypatch, mod, fake)

    result = mod.send_post(
        mod.SocialPost(platform="linkedin", body="x", stake_sentence=_STAKE)
    )
    assert result.sent is False
    assert result.error.startswith("linkedin_send_failed:")


# ---------------------------------------------------------------------------
# Non-2xx server error -> clean linkedin_http_<status>, no raise
# ---------------------------------------------------------------------------


def test_live_linkedin_500_returns_http_error(monkeypatch, tmp_path):
    mod = _reload_live(
        monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl",
        linkedin_token="tok", linkedin_urn="urn:li:person:abc",
    )
    req = httpx.Request("POST", "https://api.linkedin.com/v2/ugcPosts")
    fake = _FakeClient(responses=[httpx.Response(500, text="oops", request=req)])
    _patch_client(monkeypatch, mod, fake)

    result = mod.send_post(
        mod.SocialPost(platform="linkedin", body="x", stake_sentence=_STAKE)
    )
    assert result.sent is False
    assert result.error == "linkedin_http_500"


# ===========================================================================
# OAuth helpers
# ===========================================================================


def test_build_authorize_url_linkedin():
    from backend.outreach.social_oauth import build_authorize_url

    url = build_authorize_url(
        "linkedin", "client123", "https://app/cb", "state-xyz",
    )
    assert url.startswith("https://www.linkedin.com/oauth/v2/authorization?")
    assert "response_type=code" in url
    assert "client_id=client123" in url
    assert "state=state-xyz" in url
    assert "w_member_social" in url
    # redirect_uri is url-encoded
    assert "redirect_uri=https%3A%2F%2Fapp%2Fcb" in url


def test_build_authorize_url_facebook():
    from backend.outreach.social_oauth import build_authorize_url

    url = build_authorize_url(
        "facebook", "fbclient", "https://app/cb", "st",
        scope="pages_manage_posts",
    )
    assert url.startswith("https://www.facebook.com/v19.0/dialog/oauth?")
    assert "client_id=fbclient" in url
    assert "pages_manage_posts" in url


def test_build_authorize_url_rejects_unknown_platform():
    from backend.outreach.social_oauth import build_authorize_url

    with pytest.raises(ValueError):
        build_authorize_url("twitter", "c", "https://app/cb", "st")


def test_build_authorize_url_requires_all_params():
    from backend.outreach.social_oauth import build_authorize_url

    with pytest.raises(ValueError):
        build_authorize_url("linkedin", "", "https://app/cb", "st")


class _FakeOauthClient:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, *, data=None, headers=None):
        self.last = {"url": url, "data": data, "headers": headers}
        return self._response


def _patch_oauth_client(monkeypatch, response):
    import backend.outreach.social_oauth as mod

    class _Builder:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return _FakeOauthClient(response)

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mod.httpx, "Client", _Builder)
    return mod


def test_exchange_code_parses_tokens(monkeypatch):
    resp = httpx.Response(
        200,
        json={"access_token": "AT123", "expires_in": 5184000, "refresh_token": "RT"},
        request=httpx.Request("POST", "https://x/token"),
    )
    mod = _patch_oauth_client(monkeypatch, resp)
    out = mod.exchange_code(
        "linkedin", "code1", "cid", "secret", "https://app/cb",
    )
    assert out["access_token"] == "AT123"
    assert out["refresh_token"] == "RT"


def test_exchange_code_http_error_raises(monkeypatch):
    resp = httpx.Response(
        400,
        json={"error": "invalid_grant", "error_description": "bad code"},
        request=httpx.Request("POST", "https://x/token"),
    )
    mod = _patch_oauth_client(monkeypatch, resp)
    with pytest.raises(mod.SocialOauthError) as exc:
        mod.exchange_code("linkedin", "code1", "cid", "secret", "https://app/cb")
    assert "exchange_code_http_400" in str(exc.value)


def test_exchange_code_missing_access_token_raises(monkeypatch):
    resp = httpx.Response(
        200, json={"token_type": "Bearer"},
        request=httpx.Request("POST", "https://x/token"),
    )
    mod = _patch_oauth_client(monkeypatch, resp)
    with pytest.raises(mod.SocialOauthError) as exc:
        mod.exchange_code("facebook", "c", "cid", "secret", "https://app/cb")
    assert "no_access_token" in str(exc.value)


def test_refresh_linkedin_token_parses(monkeypatch):
    resp = httpx.Response(
        200, json={"access_token": "newAT", "expires_in": 5184000},
        request=httpx.Request("POST", "https://x/token"),
    )
    mod = _patch_oauth_client(monkeypatch, resp)
    out = mod.refresh_linkedin_token("RT", "cid", "secret")
    assert out["access_token"] == "newAT"


def test_refresh_linkedin_token_requires_args():
    from backend.outreach.social_oauth import refresh_linkedin_token

    with pytest.raises(ValueError):
        refresh_linkedin_token("", "cid", "secret")
