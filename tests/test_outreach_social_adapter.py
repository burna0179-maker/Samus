"""Tests for the outreach social channel adapter (LinkedIn + Facebook).

All tests run in dry-run mode — no real API calls are ever made. The
dry-run default is enforced by the module itself (SAMUS_SOCIAL_DRY_RUN
defaults to "true") and re-asserted in the helpers below.
"""
from __future__ import annotations

import importlib
import json
import os
from dataclasses import asdict
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _reload_adapter(monkeypatch, *, dry_run: str = "true", ledger: Path | None = None):
    """Return a freshly-imported social_adapter module with controlled env."""
    monkeypatch.setenv("SAMUS_SOCIAL_DRY_RUN", dry_run)
    if ledger is not None:
        monkeypatch.setenv("SAMUS_SOCIAL_LEDGER_PATH", str(ledger))
    # Force module-level constants to be re-evaluated by re-importing
    import backend.outreach.social_adapter as mod
    importlib.reload(mod)
    return mod


def _read_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [json.loads(l) for l in lines]


# ---------------------------------------------------------------------------
# test_socialpost_dataclass_defaults
# ---------------------------------------------------------------------------


def test_socialpost_dataclass_defaults():
    """SocialPost has correct defaults for optional fields."""
    from backend.outreach.social_adapter import SocialPost

    post = SocialPost(platform="linkedin", body="Hello world")
    assert post.platform == "linkedin"
    assert post.body == "Hello world"
    assert post.link == ""
    assert post.image_url == ""
    assert post.scheduled_at == ""
    assert post.tags == []


# ---------------------------------------------------------------------------
# test_send_post_dry_run_default_returns_sent_true_dry_run_true
# ---------------------------------------------------------------------------


def test_send_post_dry_run_default_returns_sent_true_dry_run_true(
    monkeypatch, tmp_path
):
    """Default dry-run: send_post returns sent=True, dry_run=True."""
    mod = _reload_adapter(monkeypatch, dry_run="true", ledger=tmp_path / "posts.jsonl")

    post = mod.SocialPost(platform="linkedin", body="Test post body")
    result = mod.send_post(post)

    assert result.sent is True
    assert result.dry_run is True
    assert result.platform == "linkedin"
    assert result.post_id.startswith("dry-")
    assert result.error == ""


# ---------------------------------------------------------------------------
# test_send_post_dry_run_writes_to_ledger
# ---------------------------------------------------------------------------


def test_send_post_dry_run_writes_to_ledger(monkeypatch, tmp_path):
    """Dry-run post is appended to the JSONL ledger."""
    ledger = tmp_path / "posts.jsonl"
    mod = _reload_adapter(monkeypatch, dry_run="true", ledger=ledger)

    post = mod.SocialPost(platform="facebook", body="Facebook test", link="https://example.com")
    mod.send_post(post)

    records = _read_ledger(ledger)
    assert len(records) == 1
    r = records[0]
    assert r["platform"] == "facebook"
    assert r["sent"] is True
    assert r["dry_run"] is True
    assert r["link"] == "https://example.com"
    assert "ts" in r


# ---------------------------------------------------------------------------
# test_send_post_includes_all_post_fields_in_ledger_line
# ---------------------------------------------------------------------------


def test_send_post_includes_all_post_fields_in_ledger_line(monkeypatch, tmp_path):
    """The ledger line records all post fields including tags, image_url, scheduled_at."""
    ledger = tmp_path / "posts.jsonl"
    mod = _reload_adapter(monkeypatch, dry_run="true", ledger=ledger)

    post = mod.SocialPost(
        platform="linkedin",
        body="Full fields post",
        link="https://example.com/page",
        image_url="https://example.com/img.png",
        scheduled_at="2026-06-01T09:00:00Z",
        tags=["#sales", "#ai"],
    )
    mod.send_post(post)

    records = _read_ledger(ledger)
    assert len(records) == 1
    r = records[0]
    assert r["image_url"] == "https://example.com/img.png"
    assert r["scheduled_at"] == "2026-06-01T09:00:00Z"
    assert r["tags"] == ["#sales", "#ai"]
    assert "Full fields post" in r["body_preview"]


# ---------------------------------------------------------------------------
# test_send_post_live_mode_no_token_returns_error
# ---------------------------------------------------------------------------


def test_send_post_live_mode_no_token_returns_error(monkeypatch, tmp_path):
    """Live mode with a valid stake sentence but no token returns sent=False
    and a fail-closed ``*_token_unset`` error (no network call)."""
    ledger = tmp_path / "posts.jsonl"
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "")
    monkeypatch.setenv("FACEBOOK_PAGE_TOKEN", "")
    mod = _reload_adapter(monkeypatch, dry_run="false", ledger=ledger)

    stake = "Met Dana at the Yuba City chamber mixer last Tuesday — promised a follow-up."

    post_li = mod.SocialPost(
        platform="linkedin", body="LinkedIn no token", stake_sentence=stake
    )
    result_li = mod.send_post(post_li)
    assert result_li.sent is False
    assert result_li.error == "linkedin_token_unset"
    assert result_li.dry_run is False

    post_fb = mod.SocialPost(
        platform="facebook", body="Facebook no token", stake_sentence=stake
    )
    result_fb = mod.send_post(post_fb)
    assert result_fb.sent is False
    assert result_fb.error == "facebook_token_unset"

    # Both failures must be recorded to the ledger
    records = _read_ledger(ledger)
    assert len(records) == 2
    assert records[0]["sent"] is False
    assert records[1]["sent"] is False


# ---------------------------------------------------------------------------
# test_send_linkedin_missing_author_urn_fails_closed
# ---------------------------------------------------------------------------


def test_send_linkedin_missing_author_urn_fails_closed(monkeypatch, tmp_path):
    """_send_linkedin with a token but no author URN fails closed (no network)."""
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "fake-token-for-test")
    monkeypatch.delenv("LINKEDIN_AUTHOR_URN", raising=False)
    mod = _reload_adapter(monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl")

    post = mod.SocialPost(platform="linkedin", body="LinkedIn test")
    result = mod._send_linkedin(post)

    assert result.sent is False
    assert result.error == "linkedin_author_urn_unset"
    assert result.platform == "linkedin"


# ---------------------------------------------------------------------------
# test_send_facebook_missing_page_id_fails_closed
# ---------------------------------------------------------------------------


def test_send_facebook_missing_page_id_fails_closed(monkeypatch, tmp_path):
    """_send_facebook with a token but no page id fails closed (no network)."""
    monkeypatch.setenv("FACEBOOK_PAGE_TOKEN", "fake-page-token")
    monkeypatch.delenv("FACEBOOK_PAGE_ID", raising=False)
    mod = _reload_adapter(monkeypatch, dry_run="false", ledger=tmp_path / "p.jsonl")

    post = mod.SocialPost(platform="facebook", body="Facebook test")
    result = mod._send_facebook(post)

    assert result.sent is False
    assert result.error == "facebook_page_id_unset"
    assert result.platform == "facebook"


# ---------------------------------------------------------------------------
# test_audit_send_appends_to_ledger
# ---------------------------------------------------------------------------


def test_audit_send_appends_to_ledger(monkeypatch, tmp_path):
    """_audit_send writes one JSONL line per call."""
    ledger = tmp_path / "audit.jsonl"
    mod = _reload_adapter(monkeypatch, dry_run="true", ledger=ledger)

    post = mod.SocialPost(platform="linkedin", body="Audit test")
    result = mod.SocialSendResult(
        sent=True, platform="linkedin", post_id="dry-001", dry_run=True
    )
    mod._audit_send(post, result)
    mod._audit_send(post, result)

    records = _read_ledger(ledger)
    assert len(records) == 2
    for r in records:
        assert r["sent"] is True
        assert r["dry_run"] is True
        assert r["platform"] == "linkedin"


# ---------------------------------------------------------------------------
# test_compose_post_via_llm_falls_back_to_template_on_no_api_key
# ---------------------------------------------------------------------------


def test_compose_post_via_llm_falls_back_to_template_on_no_api_key(monkeypatch):
    """compose_post_via_llm falls back to template when ANTHROPIC_API_KEY is unset."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from backend.outreach.social_adapter import compose_post_via_llm

    intel = {"summary": "a great new SaaS product", "business_name": "AcmeCorp"}
    post = compose_post_via_llm(intel, "linkedin")

    assert post.platform == "linkedin"
    assert isinstance(post.body, str)
    assert len(post.body) > 0
    # Template path uses the summary
    assert "AcmeCorp" in post.body or "great new SaaS" in post.body


# ---------------------------------------------------------------------------
# test_compose_post_via_llm_template_respects_platform_char_limit
# ---------------------------------------------------------------------------


def test_compose_post_via_llm_template_respects_platform_char_limit(monkeypatch):
    """compose_post_via_llm body never exceeds platform char limit."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from backend.outreach import social_adapter

    for platform, limit in social_adapter._PLATFORM_CHAR_LIMIT.items():
        # Use a very long summary to exercise truncation
        intel = {"summary": "X" * 400}
        post = social_adapter.compose_post_via_llm(intel, platform)  # type: ignore[arg-type]
        assert len(post.body) <= limit, (
            f"{platform}: body length {len(post.body)} exceeds limit {limit}"
        )


# ---------------------------------------------------------------------------
# test_compose_post_via_llm_falls_back_on_llm_error (monkeypatch llm_client)
# ---------------------------------------------------------------------------


def test_compose_post_via_llm_falls_back_on_llm_error(monkeypatch, tmp_path):
    """compose_post_via_llm falls back to template when anthropic_messages raises.

    Injects a fake llm_client module that raises on anthropic_messages so the
    test does not depend on the real llm_client being present in this branch.
    """
    import sys
    import types

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-error-test")

    # Build a minimal fake llm_client module that raises on anthropic_messages
    class _FakeLlmCallError(Exception):
        pass

    fake_mod = types.ModuleType("backend.common.llm_client")
    fake_mod.LlmCallError = _FakeLlmCallError  # type: ignore[attr-defined]
    fake_mod.BudgetExceeded = _FakeLlmCallError  # type: ignore[attr-defined]

    def _boom(**kw):
        raise _FakeLlmCallError("simulated llm error")

    fake_mod.anthropic_messages = _boom  # type: ignore[attr-defined]

    # Inject into sys.modules so the lazy import inside compose_post_via_llm finds it
    monkeypatch.setitem(sys.modules, "backend.common.llm_client", fake_mod)

    import backend.outreach.social_adapter as sa_mod

    intel = {"summary": "integration test fallback"}
    post = sa_mod.compose_post_via_llm(intel, "facebook")

    assert post.platform == "facebook"
    assert len(post.body) > 0
    limit = sa_mod._PLATFORM_CHAR_LIMIT["facebook"]
    assert len(post.body) <= limit


# ---------------------------------------------------------------------------
# test_send_post_live_missing_stake_sentence_refused
# ---------------------------------------------------------------------------


def test_send_post_live_missing_stake_sentence_refused(monkeypatch, tmp_path):
    """Live mode + token present but NO stake_sentence is refused before any
    token check or network call (G1 fail-closed)."""
    ledger = tmp_path / "posts.jsonl"
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "live-token-123")
    monkeypatch.setenv("LINKEDIN_AUTHOR_URN", "urn:li:person:abc")
    mod = _reload_adapter(monkeypatch, dry_run="false", ledger=ledger)

    post = mod.SocialPost(platform="linkedin", body="Live no-stake test")
    result = mod.send_post(post)

    assert result.sent is False
    assert result.error == "stake_sentence_required"
    assert result.dry_run is False

    # Audit entry must still be written
    records = _read_ledger(ledger)
    assert len(records) == 1
    assert records[0]["sent"] is False
    assert records[0]["has_stake_sentence"] is False
