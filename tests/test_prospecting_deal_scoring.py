"""Tests for backend.prospecting.deal_scoring.

Covers: clamp, classify_deal, compute_base_score, adjust_for_signals,
adjust_for_engagement, score_deal.
All functions are pure; no I/O or mocking required.
"""

from __future__ import annotations

import pytest

from backend.prospecting.deal_scoring import (
    BASE_WEIGHTS,
    adjust_for_signals,
    clamp,
    classify_deal,
    compute_base_score,
    score_deal,
)


# ---------------------------------------------------------------------------
# clamp
# ---------------------------------------------------------------------------


def test_clamp_at_boundaries():
    """Values outside [0, 1] are clamped."""
    assert clamp(-0.5) == pytest.approx(0.0)
    assert clamp(1.5) == pytest.approx(1.0)
    assert clamp(0.0) == pytest.approx(0.0)
    assert clamp(1.0) == pytest.approx(1.0)
    assert clamp(0.5) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# classify_deal
# ---------------------------------------------------------------------------


def test_classify_deal_tier_boundaries():
    """Tier transitions occur at published thresholds."""
    assert classify_deal(0.0) == "cold"
    assert classify_deal(0.34) == "cold"
    assert classify_deal(0.35) == "nurture"
    assert classify_deal(0.54) == "nurture"
    assert classify_deal(0.55) == "warm"
    assert classify_deal(0.74) == "warm"
    assert classify_deal(0.75) == "hot"
    assert classify_deal(1.0) == "hot"


# ---------------------------------------------------------------------------
# compute_base_score
# ---------------------------------------------------------------------------


def test_compute_base_score_zero_opportunity():
    """All-zero opportunity scores produce a 0.0 base score."""
    opportunity = {k: 0.0 for k in BASE_WEIGHTS}
    assert compute_base_score(opportunity) == pytest.approx(0.0)


def test_compute_base_score_full_opportunity():
    """All-100 opportunity scores produce the maximum (capped at 1.0) base score."""
    opportunity = {k: 100.0 for k in BASE_WEIGHTS}
    score = compute_base_score(opportunity)
    # sum of weights = 0.25+0.20+0.15+0.20+0.20 = 1.0, so 100/100 * 1.0 = 1.0
    assert score == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# adjust_for_signals
# ---------------------------------------------------------------------------


def test_adjust_for_signals_subtracts_for_existing_assets():
    """Signals for assets the business already has reduce the opportunity score."""
    # Start with a mid-point score.
    base = 0.6

    signals_all_present = {
        "has_website": True,
        "has_cta": True,
        "has_booking": True,
        "review_count": 25,  # > 20 triggers reduction
        "rating": 4.8,  # >= 4.5 triggers reduction
        "ads_detected": True,
    }
    adjusted = adjust_for_signals(base, signals_all_present)
    # All six signal weights are negative, so result must be lower than base.
    assert adjusted < base
    # And still within [0, 1].
    assert 0.0 <= adjusted <= 1.0


def test_adjust_for_signals_no_deductions_when_no_assets():
    """No deductions when the business has no existing digital presence."""
    base = 0.5
    signals_none = {
        "has_website": False,
        "has_cta": False,
        "has_booking": False,
        "review_count": 3,  # <= 20 — no deduction
        "rating": 3.0,  # < 4.5 — no deduction
        "ads_detected": False,
    }
    adjusted = adjust_for_signals(base, signals_none)
    assert adjusted == pytest.approx(base)


# ---------------------------------------------------------------------------
# adjust_for_engagement
# ---------------------------------------------------------------------------


def test_score_deal_with_positive_engagement_raises_score():
    """Positive engagement increases the final probability."""
    intel_no_presence = {
        "opportunity_scores": {
            "website": 100,
            "seo": 80,
            "ads": 80,
            "automation": 90,
            "reputation": 90,
        },
        "signals": {
            "has_website": False,
            "has_cta": False,
            "has_booking": False,
            "review_count": 0,
            "rating": 0.0,
            "ads_detected": False,
        },
    }
    result_no_engagement = score_deal(intel_no_presence)
    result_positive = score_deal(
        intel_no_presence,
        engagement={"positive": True, "questions": True},
    )
    assert result_positive["probability"] > result_no_engagement["probability"]
    # Engagement weights are positive, so tier should be at least as good.
    tier_order = {"cold": 0, "nurture": 1, "warm": 2, "hot": 3}
    assert tier_order[result_positive["tier"]] >= tier_order[result_no_engagement["tier"]]


# ---------------------------------------------------------------------------
# score_deal end-to-end
# ---------------------------------------------------------------------------


def test_score_deal_no_engagement():
    """score_deal without engagement returns a valid result dict."""
    intel = {
        "opportunity_scores": {
            "website": 60,
            "seo": 60,
            "ads": 40,
            "automation": 90,
            "reputation": 30,
        },
        "signals": {
            "has_website": True,
            "has_cta": False,
            "has_booking": False,
            "review_count": 10,
            "rating": 3.8,
            "ads_detected": False,
        },
    }
    result = score_deal(intel)
    assert "probability" in result
    assert "tier" in result
    assert "priority_score" in result
    assert 0.0 <= result["probability"] <= 1.0
    assert result["tier"] in ("cold", "nurture", "warm", "hot")
    assert result["priority_score"] == int(result["probability"] * 100)


def test_score_deal_empty_intel_returns_cold():
    """Empty intel dict results in a cold deal with zero probability."""
    result = score_deal({})
    assert result["probability"] == pytest.approx(0.0)
    assert result["tier"] == "cold"
    assert result["priority_score"] == 0
