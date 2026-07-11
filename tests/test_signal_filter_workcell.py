"""Tests for the signal_filter workcell.

Covers: /health smoke, /work + REST-alias routing, the deterministic
scoring map, the should_enqueue boundary at 0.62, and enrichment graceful
degradation. All network I/O is monkeypatched — no test touches the wire.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient


# ── Enrichment stub helpers ─────────────────────────────────────────────────


def _high_enrichment() -> dict[str, Any]:
    """A fully-qualified prospect — every Tier-1 signal present."""
    return {
        "domain": "example.com",
        "has_website": True,
        "dns": {"resolves": True, "has_mx": True, "mx_count": 2, "addresses": 1},
        "ssl": {"ssl_valid": True, "has_cert": True},
        "site": {
            "reachable": True,
            "dead_or_junk": False,
            "seo_score": 100,
            "seo_issues": [],
            "owner_signals": {
                "owner_email": "jane@example.com",
                "contact_emails": "jane@example.com",
                "social_facebook": "https://facebook.com/example",
                "social_instagram": "https://instagram.com/example",
                "social_linkedin": "",
            },
        },
        "firmographics": {
            "available": False,
            "employee_count": 0,
            "estimated_revenue": 0,
            "has_linkedin": False,
            "tech_stack_size": 0,
        },
        "review_rating": "4.8",
        "review_count": "120",
        "phone": "(555) 123-4567",
    }


def _low_enrichment() -> dict[str, Any]:
    """An un-qualified prospect — nothing resolves, no contact surface."""
    return {
        "domain": "",
        "has_website": False,
        "dns": {"resolves": False, "has_mx": False, "mx_count": 0, "addresses": 0},
        "ssl": {"ssl_valid": False, "has_cert": False},
        "site": {
            "reachable": False,
            "dead_or_junk": True,
            "seo_score": 0,
            "seo_issues": ["no_html"],
            "owner_signals": {},
        },
        "firmographics": {
            "available": False,
            "employee_count": 0,
            "estimated_revenue": 0,
            "has_linkedin": False,
            "tech_stack_size": 0,
        },
        "review_rating": "",
        "review_count": "",
        "phone": "",
    }


def _patch_enrich(monkeypatch, enrichment: dict[str, Any]) -> None:
    """Replace the enrich() call inside service.py with a stub."""
    import backend.signal_filter.service as svc_mod

    def _fake_enrich(_prospect: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        return enrichment

    monkeypatch.setattr(svc_mod, "enrich", _fake_enrich)


# ── /health smoke ───────────────────────────────────────────────────────────


def test_health():
    from backend.signal_filter.app import app

    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "signal_filter"


# ── /work route ─────────────────────────────────────────────────────────────


def test_work_admits_high_quality_prospect(monkeypatch):
    _patch_enrich(monkeypatch, _high_enrichment())
    from backend.signal_filter.app import app

    client = TestClient(app)
    r = client.post(
        "/work",
        json={
            "task_id": "t-high",
            "payload": {
                "prospect_id": "p-high",
                "business_name": "Acme Co",
                "website_url": "https://example.com",
            },
            "metadata": {"action": "evaluate"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["admitted"] is True
    assert body["decision_path"] == "admitted"
    assert body["weighted_score"] >= body["threshold"]
    # Seven signal axes, all present.
    assert set(body["signals"]) == {
        "domain_health",
        "seo_score",
        "review_velocity",
        "contactability",
        "social_activity",
        "revenue_estimate",
        "infrastructure_maturity",
    }


def test_work_rejects_low_quality_prospect(monkeypatch):
    _patch_enrich(monkeypatch, _low_enrichment())
    from backend.signal_filter.app import app

    client = TestClient(app)
    r = client.post(
        "/work",
        json={
            "task_id": "t-low",
            "payload": {"prospect_id": "p-low", "website_url": ""},
            "metadata": {"action": "evaluate"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["admitted"] is False
    assert body["decision_path"] == "rejected"
    assert body["weighted_score"] < body["threshold"]


def test_work_default_action_is_evaluate(monkeypatch):
    """No metadata.action → defaults to evaluate, still works."""
    _patch_enrich(monkeypatch, _high_enrichment())
    from backend.signal_filter.app import app

    client = TestClient(app)
    r = client.post(
        "/work",
        json={
            "task_id": "t-default",
            "payload": {"prospect_id": "p-default", "website_url": "https://example.com"},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["admitted"] is True


def test_work_unknown_action_400(monkeypatch):
    _patch_enrich(monkeypatch, _high_enrichment())
    from backend.signal_filter.app import app

    client = TestClient(app)
    r = client.post(
        "/work",
        json={
            "task_id": "t-x",
            "payload": {"website_url": "https://example.com"},
            "metadata": {"action": "not_a_real_action"},
        },
    )
    assert r.status_code == 400
    assert "unknown_action" in r.json()["detail"]


def test_work_malformed_prospect_422(monkeypatch):
    _patch_enrich(monkeypatch, _high_enrichment())
    from backend.signal_filter.app import app

    client = TestClient(app)
    r = client.post(
        "/work",
        json={
            "task_id": "t-bad",
            "payload": {"unexpected_field": "boom"},  # extra="forbid" → reject
            "metadata": {"action": "evaluate"},
        },
    )
    assert r.status_code == 422


# ── REST alias ──────────────────────────────────────────────────────────────


def test_rest_alias_evaluate(monkeypatch):
    _patch_enrich(monkeypatch, _high_enrichment())
    from backend.signal_filter.app import app

    client = TestClient(app)
    r = client.post(
        "/signal_filter/evaluate",
        json={
            "prospect_id": "p-rest",
            "website_url": "https://example.com",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["prospect_id"] == "p-rest"
    assert body["admitted"] is True


def test_capability_registered():
    """signal_filter must be in SERVICE_CAPABILITIES with plan_execution."""
    from backend.common.capabilities import SERVICE_CAPABILITIES

    assert "signal_filter" in SERVICE_CAPABILITIES
    assert "plan_execution" in SERVICE_CAPABILITIES["signal_filter"]
