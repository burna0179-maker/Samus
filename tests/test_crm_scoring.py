"""Unit tests for CRM deal scoring helpers (pure functions)."""
from __future__ import annotations

import pytest

from backend.crm.scoring import (
    classify_tier,
    effective_deal_size_usd,
    estimate_deal_size_usd,
    score_opportunity_from_lead,
    tier_close_probability,
)


# ---------------------------------------------------------------------------
# classify_tier — boundary checks at 40 / 70 / 90
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("score,expected", [
    (0, "low"),
    (1, "low"),
    (39, "low"),
    (40, "warm"),
    (50, "warm"),
    (69, "warm"),
    (70, "hot"),
    (80, "hot"),
    (89, "hot"),
    (90, "priority"),
    (100, "priority"),
])
def test_classify_tier_boundaries(score, expected):
    assert classify_tier(score) == expected


def test_classify_tier_handles_none_like_input():
    # Defensive: zero-coerce on missing / None.
    assert classify_tier(0) == "low"


def test_tier_close_probability_monotonic():
    assert tier_close_probability("low") < tier_close_probability("warm")
    assert tier_close_probability("warm") < tier_close_probability("hot")
    assert tier_close_probability("hot") < tier_close_probability("priority")


# ---------------------------------------------------------------------------
# estimate_deal_size_usd
# ---------------------------------------------------------------------------

def test_estimate_deal_size_500_2000_band_single_service():
    # midpoint=1250 -> annual=15000
    assert estimate_deal_size_usd("$500-$2000", ["seo_audit"]) == 15000.0


def test_estimate_deal_size_empty_budget_is_zero():
    assert estimate_deal_size_usd("", ["seo_audit"]) == 0.0
    assert estimate_deal_size_usd("", []) == 0.0


def test_estimate_deal_size_under_150_uses_floor():
    # midpoint=100 -> annual=1200
    assert estimate_deal_size_usd("under_$150", []) == 1200.0


def test_estimate_deal_size_5000_plus_uses_anchor():
    # midpoint=7500 -> annual=90000
    assert estimate_deal_size_usd("$5000+", []) == 90000.0


def test_estimate_deal_size_multi_service_bumps_15pct():
    # base annual=15000 with two services -> 15000 * 1.15 = 17250
    out = estimate_deal_size_usd("$500-$2000", ["seo_audit", "workflow_rescue"])
    assert out == 17250.0


def test_estimate_deal_size_not_sure_ignored_for_multiplier():
    # "not_sure" is a real category but shouldn't count as multi-service intent.
    out = estimate_deal_size_usd("$500-$2000", ["seo_audit", "not_sure"])
    assert out == 15000.0  # no 15% bump


def test_estimate_deal_size_unknown_budget_returns_zero():
    assert estimate_deal_size_usd("not_an_enum_value", []) == 0.0


# ---------------------------------------------------------------------------
# score_opportunity_from_lead — composite
# ---------------------------------------------------------------------------

def test_score_opportunity_priority_tier_high_budget():
    lead = {
        "intent_score": 95,
        "monthly_budget": "$5000+",
        "service_interest": ["seo_optimization", "workflow_buildout"],
    }
    tier, prob, size = score_opportunity_from_lead(lead)
    assert tier == "priority"
    assert prob == tier_close_probability("priority")
    # 7500 * 12 * 1.15 = 103500
    assert size == 103500.0


def test_score_opportunity_low_tier_low_budget():
    lead = {
        "intent_score": 10,
        "monthly_budget": "under_$150",
        "service_interest": [],
    }
    tier, prob, size = score_opportunity_from_lead(lead)
    assert tier == "low"
    assert prob == tier_close_probability("low")
    assert size == 1200.0


def test_score_opportunity_missing_keys_safe():
    # No keys at all -> low tier, zero size.
    tier, prob, size = score_opportunity_from_lead({})
    assert tier == "low"
    assert prob == tier_close_probability("low")
    assert size == 0.0


def test_score_opportunity_handles_non_list_services():
    # Defensive: corrupt row with services as a string shouldn't blow up.
    lead = {
        "intent_score": 70,
        "monthly_budget": "$500-$2000",
        "service_interest": "seo_audit",  # wrong shape
    }
    tier, prob, size = score_opportunity_from_lead(lead)
    assert tier == "hot"
    assert size == 15000.0  # treated as single (or empty) service


# ---------------------------------------------------------------------------
# effective_deal_size_usd — ranking fallback for budget-unknown opps
# ---------------------------------------------------------------------------

def test_effective_deal_size_zero_returns_floor():
    assert effective_deal_size_usd(0.0) == 1200.0


def test_effective_deal_size_nonzero_passes_through():
    assert effective_deal_size_usd(15000.0) == 15000.0


def test_effective_deal_size_negative_passes_through():
    assert effective_deal_size_usd(-1.0) == -1.0
