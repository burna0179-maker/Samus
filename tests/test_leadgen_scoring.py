"""Tests for backend.leadgen.scoring."""
from __future__ import annotations


def _make_request(**overrides):
    from backend.leadgen.models import LeadRequest

    defaults = dict(
        company="Acme",
        domain="acme.com",
        industry="finance",
        employee_count=75,
        annual_revenue_usd=5_000_000,
        geo="US",
        signals=["manual_ops", "fragmented_tooling"],
    )
    defaults.update(overrides)
    return LeadRequest(**defaults)


def test_classify_segment_thresholds():
    from backend.leadgen.scoring import classify_segment

    assert classify_segment(5, 500_000) == "micro"
    assert classify_segment(50, 5_000_000) == "smb"
    assert classify_segment(500, 50_000_000) == "midmarket"
    assert classify_segment(5_000, 500_000_000) == "enterprise"


def test_score_lead_priority_finance_smb():
    from backend.leadgen.enrichment import enrich_lead
    from backend.leadgen.scoring import score_lead, tier_for_score

    req = _make_request()
    enrichment = enrich_lead(req)
    total, breakdown, matched = score_lead(req, enrichment)

    assert 0 <= total <= 100
    assert breakdown["industry"] == 20  # finance
    assert breakdown["geo"] == 8        # US
    assert "manual_ops" in matched and "fragmented_tooling" in matched
    assert breakdown["signals"] == 14 + 12
    assert tier_for_score(total) in ("medium", "high", "priority")


def test_score_lead_caps_at_100():
    from backend.leadgen.enrichment import enrich_lead
    from backend.leadgen.scoring import score_lead

    req = _make_request(
        signals=[
            "manual_ops",
            "fragmented_tooling",
            "high_ticket_volume",
            "funding",
            "compliance_pressure",
            "slow_reporting",
            "hiring",
            "expansion",
        ],
        employee_count=500,
        annual_revenue_usd=50_000_000,
    )
    enrichment = enrich_lead(req)
    total, _, _ = score_lead(req, enrichment)
    assert total == 100


def test_build_recommendations_returns_3_to_5():
    from backend.leadgen.scoring import build_recommendations

    recs = build_recommendations("smb", "high", ["manual_ops"])
    assert 3 <= len(recs) <= 5
    assert any("Workflow replacement" in r for r in recs)


def test_low_tier_for_weak_lead():
    from backend.leadgen.enrichment import enrich_lead
    from backend.leadgen.scoring import score_lead, tier_for_score

    req = _make_request(
        industry="retail",
        employee_count=5,
        annual_revenue_usd=200_000,
        geo="ZZ",
        signals=[],
    )
    enrichment = enrich_lead(req)
    total, _, _ = score_lead(req, enrichment)
    assert tier_for_score(total) in ("low", "medium")
