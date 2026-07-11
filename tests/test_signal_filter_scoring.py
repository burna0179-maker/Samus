"""Scoring + queue-gate unit tests for the signal_filter workcell.

Covers the deterministic enrichment→ProspectSignal map, float clamping, and
the should_enqueue boundary at exactly 0.62 (just-below and just-above).
"""
from __future__ import annotations

import pytest

from backend.signal_filter.queue_gate import (
    ADMISSION_THRESHOLD,
    should_enqueue,
    weighted_score,
)
from backend.signal_filter.scoring import ProspectSignal, signals_from_enrichment


# ── ProspectSignal dataclass ────────────────────────────────────────────────


def test_prospect_signal_clamps_above_one():
    sig = ProspectSignal(domain_health=2.5, seo_score=1.4)
    assert sig.domain_health == 1.0
    assert sig.seo_score == 1.0


def test_prospect_signal_clamps_below_zero():
    sig = ProspectSignal(contactability=-3.0, review_velocity=-0.1)
    assert sig.contactability == 0.0
    assert sig.review_velocity == 0.0


def test_prospect_signal_as_dict_has_seven_axes():
    sig = ProspectSignal()
    keys = set(sig.as_dict())
    assert keys == {
        "domain_health", "seo_score", "review_velocity", "contactability",
        "social_activity", "revenue_estimate", "infrastructure_maturity",
    }


def test_signals_from_empty_enrichment_is_all_zero_ish():
    """Empty enrichment never raises; revenue_estimate stays neutral 0.5."""
    sig = signals_from_enrichment({})
    assert sig.domain_health == 0.0
    assert sig.seo_score == 0.0
    assert sig.contactability == 0.0
    # Firmographics unavailable → revenue axis is the neutral midpoint.
    assert sig.revenue_estimate == 0.5


def test_signals_from_full_enrichment_scores_high():
    enrichment = {
        "dns": {"resolves": True, "has_mx": True},
        "ssl": {"ssl_valid": True, "has_cert": True},
        "site": {
            "reachable": True,
            "dead_or_junk": False,
            "seo_score": 100,
            "owner_signals": {
                "owner_email": "jane@acme.com",
                "social_facebook": "https://facebook.com/acme",
            },
        },
        "firmographics": {"available": False},
        "review_rating": "4.9",
        "review_count": "200",
        "phone": "(555) 222-3333",
    }
    sig = signals_from_enrichment(enrichment)
    assert sig.domain_health == 1.0
    assert sig.seo_score == 1.0
    assert sig.infrastructure_maturity == 1.0
    assert sig.contactability >= 0.8


# ── weighted_score / should_enqueue boundary at 0.62 ────────────────────────


def test_threshold_constant_is_062():
    assert ADMISSION_THRESHOLD == 0.62


def test_weighted_score_formula():
    """weighted = dh*.20 + seo*.25 + contact*.25 + review*.10 + infra*.20."""
    sig = ProspectSignal(
        domain_health=1.0,
        seo_score=1.0,
        review_velocity=1.0,
        contactability=1.0,
        social_activity=1.0,      # not weighted
        revenue_estimate=1.0,     # not weighted
        infrastructure_maturity=1.0,
    )
    # 0.20 + 0.25 + 0.25 + 0.10 + 0.20 == 1.0
    assert weighted_score(sig) == 1.0


def test_should_enqueue_just_below_threshold_rejects():
    """A signal whose weighted score lands just below 0.62 is rejected."""
    # Tune one axis so the weighted score is ~0.615 < 0.62.
    # dh=0.5(.20=.10) seo=0.6(.25=.15) contact=0.6(.25=.15) rev=0.5(.10=.05)
    # infra=1.0(.20=.20) → total = 0.65 ... build a precise just-below case:
    sig = ProspectSignal(
        domain_health=0.6,            # .12
        seo_score=0.6,                # .15
        contactability=0.6,           # .15
        review_velocity=0.5,          # .05
        infrastructure_maturity=0.6,  # .12
    )
    score = weighted_score(sig)
    assert score < ADMISSION_THRESHOLD  # 0.59
    assert should_enqueue(sig) is False


def test_should_enqueue_just_above_threshold_admits():
    """A signal whose weighted score lands just above 0.62 is admitted."""
    sig = ProspectSignal(
        domain_health=0.65,            # .13
        seo_score=0.65,                # .1625
        contactability=0.65,           # .1625
        review_velocity=0.6,           # .06
        infrastructure_maturity=0.6,   # .12
    )
    score = weighted_score(sig)
    assert score >= ADMISSION_THRESHOLD  # ~0.635
    assert should_enqueue(sig) is True


def test_should_enqueue_exactly_at_threshold_admits():
    """weighted ~= 0.62 admits — the gate is a >= comparison."""
    # All axes 0.62 → weighted = 0.62 * (sum of weights = 1.0) ≈ 0.62
    # (term-by-term float summation lands within ~1e-16 of 0.62).
    sig = ProspectSignal(
        domain_health=0.62,
        seo_score=0.62,
        contactability=0.62,
        review_velocity=0.62,
        infrastructure_maturity=0.62,
    )
    assert weighted_score(sig) == pytest.approx(ADMISSION_THRESHOLD)
    assert should_enqueue(sig) is True
