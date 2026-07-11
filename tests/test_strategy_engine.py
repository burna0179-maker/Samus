"""Pure-functional tests for backend.strategy.engine.

No I/O; exercises StrategyContext, StrategyEngine, and pattern-learning
helpers directly.
"""
from __future__ import annotations

import pytest

from backend.strategy.engine import (
    PATTERNS,
    StrategyContext,
    StrategyEngine,
    boost_pattern,
    penalize_pattern,
    reset_patterns,
)


@pytest.fixture(autouse=True)
def _clean_patterns():
    """Reset the module-level pattern counters before each test."""
    reset_patterns()
    yield
    reset_patterns()


# ── Closed-deal guardrails ──────────────────────────────────────────────────


def test_evaluate_closed_won_returns_none():
    ctx = StrategyContext(prospect_id="p1", stage="closed_won")
    result = StrategyEngine().evaluate(ctx)
    assert result["action"] == "none"
    assert result["reason"] == "deal_closed"


def test_evaluate_closed_lost_returns_none():
    ctx = StrategyContext(prospect_id="p2", stage="closed_lost")
    result = StrategyEngine().evaluate(ctx)
    assert result["action"] == "none"
    assert result["reason"] == "deal_closed"


# ── Action selection ────────────────────────────────────────────────────────


def test_evaluate_high_score_returns_escalate_close():
    # lead_score=100 → score = 100*0.4 + 0*0.3 = 40
    # Add pricing_request (+30) and high engagement (+20) → 90 >= 85 threshold
    ctx = StrategyContext(
        prospect_id="p3",
        lead_score=100.0,
        seo_score=100.0,
        stage="active",
        engagement="high",
        conversion_signals=["pricing_request"],
    )
    result = StrategyEngine().evaluate(ctx)
    assert result["action"] == "escalate_close"
    assert result["score"] >= StrategyEngine.HIGH_SCORE_THRESHOLD


def test_evaluate_low_seo_returns_replan_fulfillment():
    # score < 85 but seo_score < LOW_SEO_THRESHOLD
    ctx = StrategyContext(
        prospect_id="p4",
        lead_score=10.0,
        seo_score=30.0,   # below 50
        stage="active",
        engagement="medium",
    )
    result = StrategyEngine().evaluate(ctx)
    assert result["action"] == "replan_fulfillment"


def test_evaluate_low_engagement_returns_trigger_outreach():
    # score < 85, seo_score >= 50, engagement == "low"
    ctx = StrategyContext(
        prospect_id="p5",
        lead_score=20.0,
        seo_score=80.0,
        stage="active",
        engagement="low",
    )
    result = StrategyEngine().evaluate(ctx)
    assert result["action"] == "trigger_outreach"


def test_evaluate_default_returns_monitor():
    # score < 85, seo_score >= 50, engagement != "low"
    ctx = StrategyContext(
        prospect_id="p6",
        lead_score=20.0,
        seo_score=80.0,
        stage="active",
        engagement="medium",
    )
    result = StrategyEngine().evaluate(ctx)
    assert result["action"] == "monitor"


# ── Score calculation ───────────────────────────────────────────────────────


def test_score_opportunity_increases_with_high_engagement():
    engine = StrategyEngine()
    ctx_low = StrategyContext(prospect_id="p7", lead_score=50.0, seo_score=80.0, engagement="low")
    ctx_high = StrategyContext(prospect_id="p8", lead_score=50.0, seo_score=80.0, engagement="high")
    score_low = engine._score_opportunity(ctx_low)
    score_high = engine._score_opportunity(ctx_high)
    assert score_high > score_low
    assert score_high - score_low == pytest.approx(20.0)


def test_score_opportunity_includes_pricing_signal_bonus():
    engine = StrategyEngine()
    ctx_no_signal = StrategyContext(
        prospect_id="p9", lead_score=50.0, seo_score=80.0, engagement="medium"
    )
    ctx_with_signal = StrategyContext(
        prospect_id="p10",
        lead_score=50.0,
        seo_score=80.0,
        engagement="medium",
        conversion_signals=["pricing_request"],
    )
    score_base = engine._score_opportunity(ctx_no_signal)
    score_signal = engine._score_opportunity(ctx_with_signal)
    assert score_signal - score_base == pytest.approx(30.0)


def test_score_opportunity_clamped_to_100():
    engine = StrategyEngine()
    ctx = StrategyContext(
        prospect_id="p11",
        lead_score=100.0,
        seo_score=0.0,
        engagement="high",
        conversion_signals=["pricing_request"],
    )
    score = engine._score_opportunity(ctx)
    assert score == pytest.approx(100.0)
    assert score <= 100.0


# ── Pattern-learning helpers ────────────────────────────────────────────────


def test_boost_pattern_increments():
    boost_pattern("key_a")
    assert PATTERNS["key_a"] == 2   # starts at 1 (default) + 1


def test_penalize_pattern_decrements_floored_at_1():
    # Penalize from default (1) — should floor at 1
    penalize_pattern("key_b")
    assert PATTERNS["key_b"] == 1

    # Boost to 3, then penalize twice
    boost_pattern("key_b")
    boost_pattern("key_b")
    # Now PATTERNS["key_b"] == 3
    penalize_pattern("key_b")
    assert PATTERNS["key_b"] == 2
    penalize_pattern("key_b")
    assert PATTERNS["key_b"] == 1


def test_reset_patterns_clears():
    boost_pattern("key_c")
    boost_pattern("key_d")
    assert len(PATTERNS) > 0
    reset_patterns()
    assert PATTERNS == {}
