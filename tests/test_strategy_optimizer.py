"""Tests for backend.strategy.optimizer (tier-3 portfolio ranker).

All tests are pure — no I/O, no external calls.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# score_prospect
# ---------------------------------------------------------------------------

def test_score_prospect_zero_probability_returns_zero():
    """Prospect with 0% conversion chance must score 0.0."""
    from backend.strategy.optimizer import ProspectSignals, score_prospect

    p = ProspectSignals(
        prospect_id="z1",
        expected_value=50_000.0,
        conversion_prob=0.0,
        execution_cost=0.0,
    )
    assert score_prospect(p) == 0.0


def test_score_prospect_high_value_beats_low_value():
    """Higher expected_value with equal other params → higher score."""
    from backend.strategy.optimizer import ProspectSignals, score_prospect

    hi = ProspectSignals(
        prospect_id="hi",
        expected_value=20_000.0,
        conversion_prob=0.3,
        time_to_close=7,
        execution_cost=50.0,
    )
    lo = ProspectSignals(
        prospect_id="lo",
        expected_value=500.0,
        conversion_prob=0.3,
        time_to_close=7,
        execution_cost=50.0,
    )
    assert score_prospect(hi) > score_prospect(lo)


def test_score_prospect_time_decay():
    """Longer time_to_close reduces score (time penalty = 1/days)."""
    from backend.strategy.optimizer import ProspectSignals, score_prospect

    fast = ProspectSignals(
        prospect_id="fast",
        expected_value=5_000.0,
        conversion_prob=0.5,
        time_to_close=1,
        execution_cost=10.0,
    )
    slow = ProspectSignals(
        prospect_id="slow",
        expected_value=5_000.0,
        conversion_prob=0.5,
        time_to_close=90,
        execution_cost=10.0,
    )
    assert score_prospect(fast) > score_prospect(slow)


def test_score_prospect_momentum_from_signals():
    """Engagement signals should lift score above baseline when momentum=0."""
    from backend.strategy.optimizer import ProspectSignals, score_prospect

    base = ProspectSignals(
        prospect_id="base",
        expected_value=2_000.0,
        conversion_prob=0.2,
        time_to_close=5,
        execution_cost=10.0,
        momentum=0.0,
        engagement_signals=[],
    )
    engaged = ProspectSignals(
        prospect_id="engaged",
        expected_value=2_000.0,
        conversion_prob=0.2,
        time_to_close=5,
        execution_cost=10.0,
        momentum=0.0,
        engagement_signals=["email_open", "link_click"],
    )
    assert score_prospect(engaged) > score_prospect(base)


def test_score_prospect_explicit_momentum_overrides_signals():
    """When momentum > 0 it is used directly; engagement_signals are ignored."""
    from backend.strategy.optimizer import ProspectSignals, score_prospect

    explicit = ProspectSignals(
        prospect_id="x",
        expected_value=1_000.0,
        conversion_prob=0.5,
        time_to_close=7,
        execution_cost=10.0,
        momentum=0.8,
        engagement_signals=["email_open"],  # would only add 0.2 if used
    )
    # Score with pre-set momentum=0.8 > momentum from signals (0.2)
    signal_only = ProspectSignals(
        prospect_id="s",
        expected_value=1_000.0,
        conversion_prob=0.5,
        time_to_close=7,
        execution_cost=10.0,
        momentum=0.0,
        engagement_signals=["email_open"],
    )
    assert score_prospect(explicit) > score_prospect(signal_only)


def test_score_prospect_cost_exceeds_gross_clamps_to_zero():
    """When execution_cost exceeds gross EV the score is clamped to 0.0."""
    from backend.strategy.optimizer import ProspectSignals, score_prospect

    p = ProspectSignals(
        prospect_id="loss",
        expected_value=100.0,
        conversion_prob=0.01,
        time_to_close=7,
        execution_cost=50_000.0,
    )
    assert score_prospect(p) == 0.0


# ---------------------------------------------------------------------------
# rank_portfolio
# ---------------------------------------------------------------------------

def test_rank_portfolio_empty_returns_empty():
    """Empty portfolio should return empty list without error."""
    from backend.strategy.optimizer import rank_portfolio

    assert rank_portfolio([]) == []


def test_rank_portfolio_ordering_highest_score_first():
    """rank_portfolio must order by descending score."""
    from backend.strategy.optimizer import ProspectSignals, rank_portfolio

    prospects = [
        ProspectSignals(prospect_id="low", expected_value=100.0, conversion_prob=0.05),
        ProspectSignals(prospect_id="high", expected_value=50_000.0, conversion_prob=0.8),
        ProspectSignals(prospect_id="mid", expected_value=5_000.0, conversion_prob=0.3),
    ]
    result = rank_portfolio(prospects)
    ids = [pid for pid, _ in result]
    assert ids[0] == "high"
    assert ids[-1] == "low"


def test_rank_portfolio_action_labels():
    """Actions returned must be members of the allowed set."""
    from backend.strategy.optimizer import ProspectSignals, rank_portfolio

    prospects = [
        ProspectSignals(prospect_id=f"p{i}", expected_value=float(1000 * (20 - i)),
                        conversion_prob=0.5 - i * 0.02)
        for i in range(20)
    ]
    result = rank_portfolio(prospects)
    valid_actions = {"accelerate", "maintain", "defer", "drop"}
    for _, action in result:
        assert action in valid_actions, f"unexpected action: {action!r}"


def test_rank_portfolio_top_tier_gets_accelerate():
    """The first TOP_TIER entries (by score) must all be 'accelerate'."""
    from backend.strategy.optimizer import ProspectSignals, rank_portfolio, TOP_TIER

    # Create 20 prospects with clearly separated scores.
    prospects = [
        ProspectSignals(
            prospect_id=f"p{i:02d}",
            expected_value=float(100_000 - i * 4_000),
            conversion_prob=0.8,
            execution_cost=10.0,
        )
        for i in range(20)
    ]
    result = rank_portfolio(prospects)
    accelerate_ids = [pid for pid, act in result if act == "accelerate"]
    assert len(accelerate_ids) == TOP_TIER


def test_rank_portfolio_zero_score_always_drops():
    """Prospects with score=0 must always receive 'drop' regardless of rank."""
    from backend.strategy.optimizer import ProspectSignals, rank_portfolio

    zero_cost = ProspectSignals(
        prospect_id="zero",
        expected_value=0.0,
        conversion_prob=0.0,
        execution_cost=0.0,
    )
    result = rank_portfolio([zero_cost])
    assert result[0] == ("zero", "drop")


def test_rank_portfolio_custom_tier_boundaries():
    """Custom top_tier/mid_tier parameters must be respected."""
    from backend.strategy.optimizer import ProspectSignals, rank_portfolio

    prospects = [
        ProspectSignals(
            prospect_id=f"p{i}",
            expected_value=float(10_000 - i * 500),
            conversion_prob=0.5,
            execution_cost=5.0,
        )
        for i in range(10)
    ]
    result = rank_portfolio(prospects, top_tier=2, mid_tier=5)
    actions = [act for _, act in result]
    # First 2 → accelerate
    assert all(a == "accelerate" for a in actions[:2])
    # Next 3 (indices 2-4) → maintain
    assert all(a == "maintain" for a in actions[2:5])
    # Remaining → defer
    assert all(a == "defer" for a in actions[5:])
