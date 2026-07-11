"""Enrichment graceful-degradation tests for the signal_filter workcell.

The Tier-1 enrichment cascade must NEVER raise and must return neutral
defaults when DNS/SSL/homepage are unreachable or no API keys are set. All
network I/O is monkeypatched — no test touches the wire.
"""
from __future__ import annotations

from typing import Any

from backend.signal_filter import enrichment as enr_mod
from backend.signal_filter.enrichment import (
    enrich,
    firmographic_enrichment,
    resolve_dns,
    validate_ssl,
)


# ── firmographic_enrichment (optional / pluggable) ──────────────────────────


def test_firmographic_enrichment_no_key_is_neutral_noop():
    """No CLEARBIT_API_KEY → neutral default, no raise."""
    result = firmographic_enrichment("example.com", api_key=None)
    assert result["available"] is False
    assert result["employee_count"] == 0
    assert result["has_linkedin"] is False


def test_firmographic_enrichment_empty_key_is_neutral():
    result = firmographic_enrichment("example.com", api_key="")
    assert result["available"] is False


def test_firmographic_enrichment_with_key_still_neutral_when_unwired():
    """A key is present but the provider is deliberately unwired (local-first)."""
    result = firmographic_enrichment("example.com", api_key="ck_test_123")
    assert result["available"] is False


# ── resolve_dns / validate_ssl fail-soft ────────────────────────────────────


def test_resolve_dns_empty_domain_is_neutral():
    result = resolve_dns("")
    assert result == {
        "resolves": False, "has_mx": False, "mx_count": 0, "addresses": 0,
    }


def test_resolve_dns_failure_returns_neutral(monkeypatch):
    """A getaddrinfo failure degrades to a neutral dict, never raises."""
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("dns unreachable")

    monkeypatch.setattr(enr_mod.socket, "getaddrinfo", _boom)
    result = resolve_dns("example.com")
    assert result["resolves"] is False
    assert result["addresses"] == 0


def test_validate_ssl_empty_domain_is_neutral():
    assert validate_ssl("") == {"ssl_valid": False, "has_cert": False}


def test_validate_ssl_connect_failure_is_neutral(monkeypatch):
    """A TLS connect failure degrades to a neutral dict, never raises."""
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(enr_mod.socket, "create_connection", _boom)
    result = validate_ssl("example.com")
    assert result["ssl_valid"] is False
    assert result["has_cert"] is False


# ── full enrich() cascade with everything offline ───────────────────────────


def _patch_all_offline(monkeypatch) -> None:
    """Make DNS, SSL, and the homepage fetch all fail — full offline mode."""
    def _dns_boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("offline")

    def _ssl_boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("offline")

    def _fetch_offline(_url: str) -> dict[str, Any]:
        return {"final_url": _url, "status_code": 0, "html": None, "fetch_error": "offline"}

    monkeypatch.setattr(enr_mod.socket, "getaddrinfo", _dns_boom)
    monkeypatch.setattr(enr_mod.socket, "create_connection", _ssl_boom)
    monkeypatch.setattr(enr_mod, "fetch_homepage", _fetch_offline)


def test_enrich_fully_offline_never_raises(monkeypatch):
    """With every network source down, enrich() returns neutral defaults."""
    _patch_all_offline(monkeypatch)
    result = enrich({"prospect_id": "p-1", "website_url": "https://example.com"})

    assert result["dns"]["resolves"] is False
    assert result["ssl"]["ssl_valid"] is False
    assert result["site"]["reachable"] is False
    assert result["site"]["dead_or_junk"] is True
    assert result["firmographics"]["available"] is False
    # The cascade still produced a structurally-complete dict.
    assert set(result) >= {"domain", "dns", "ssl", "site", "firmographics"}


def test_enrich_no_website_is_handled(monkeypatch):
    """A prospect with no website at all enriches without raising."""
    _patch_all_offline(monkeypatch)
    result = enrich({"prospect_id": "p-2", "website_url": ""})
    assert result["has_website"] is False
    assert result["domain"] == ""


def test_enrich_offline_result_scores_low(monkeypatch):
    """An offline prospect scores below the admission threshold."""
    from backend.signal_filter.queue_gate import should_enqueue
    from backend.signal_filter.scoring import signals_from_enrichment

    _patch_all_offline(monkeypatch)
    result = enrich({"prospect_id": "p-3", "website_url": "https://example.com"})
    signal = signals_from_enrichment(result)
    assert should_enqueue(signal) is False
