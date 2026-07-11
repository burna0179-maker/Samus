"""Tests for backend.strategy.trust_scorer.

Covers the formula, tier-boundary semantics, input validation, breakdown
arithmetic, tier comparison, and dataclass immutability.
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from backend.strategy.trust_scorer import (
    AccessTier,
    TrustInputs,
    TrustResult,
    WEIGHTS,
    can_access,
    score_trust,
    tier_for_score,
)


# ---------------------------------------------------------------------------
# Core formula
# ---------------------------------------------------------------------------


def test_perfect_inputs_returns_autonomous() -> None:
    result = score_trust(
        TrustInputs(
            success_rate=1.0,
            policy_compliance=1.0,
            resource_efficiency=1.0,
            stability_score=1.0,
        )
    )
    assert result.score == 1.0
    assert result.tier is AccessTier.AUTONOMOUS


def test_zero_inputs_returns_sandbox() -> None:
    result = score_trust(
        TrustInputs(
            success_rate=0.0,
            policy_compliance=0.0,
            resource_efficiency=0.0,
            stability_score=0.0,
        )
    )
    assert result.score == 0.0
    assert result.tier is AccessTier.SANDBOX


def test_known_blueprint_example() -> None:
    # Mid-range mix that should land squarely in multi_agent:
    #   0.8 * 0.4 = 0.32
    #   0.7 * 0.3 = 0.21
    #   0.6 * 0.2 = 0.12
    #   0.5 * 0.1 = 0.05
    #   total = 0.70
    result = score_trust(
        TrustInputs(
            success_rate=0.8,
            policy_compliance=0.7,
            resource_efficiency=0.6,
            stability_score=0.5,
        )
    )
    assert result.score == 0.7
    assert result.tier is AccessTier.MULTI_AGENT


def test_weights_sum_to_one() -> None:
    assert math.isclose(sum(WEIGHTS.values()), 1.0, abs_tol=1e-9)


def test_weights_match_blueprint_values() -> None:
    assert WEIGHTS == {
        "success_rate": 0.4,
        "policy_compliance": 0.3,
        "resource_efficiency": 0.2,
        "stability_score": 0.1,
    }


# ---------------------------------------------------------------------------
# Tier boundaries
# ---------------------------------------------------------------------------


def test_tier_boundary_at_0_2_is_sandbox() -> None:
    assert tier_for_score(0.2) is AccessTier.SANDBOX


def test_tier_boundary_just_above_0_2_is_restricted() -> None:
    assert tier_for_score(0.21) is AccessTier.RESTRICTED_TOOLS


def test_tier_boundary_at_0_5_is_restricted() -> None:
    assert tier_for_score(0.5) is AccessTier.RESTRICTED_TOOLS


def test_tier_boundary_just_above_0_5_is_multi_agent() -> None:
    assert tier_for_score(0.5001) is AccessTier.MULTI_AGENT


def test_tier_boundary_at_0_8_is_multi_agent() -> None:
    assert tier_for_score(0.8) is AccessTier.MULTI_AGENT


def test_tier_boundary_just_above_0_8_is_autonomous() -> None:
    assert tier_for_score(0.8001) is AccessTier.AUTONOMOUS


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_input_validation_rejects_negative_success_rate() -> None:
    with pytest.raises(ValueError) as exc:
        TrustInputs(
            success_rate=-0.01,
            policy_compliance=0.5,
            resource_efficiency=0.5,
            stability_score=0.5,
        )
    assert "success_rate" in str(exc.value)


def test_input_validation_rejects_above_one_policy_compliance() -> None:
    with pytest.raises(ValueError) as exc:
        TrustInputs(
            success_rate=0.5,
            policy_compliance=1.01,
            resource_efficiency=0.5,
            stability_score=0.5,
        )
    assert "policy_compliance" in str(exc.value)


def test_tier_for_score_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        tier_for_score(1.5)
    with pytest.raises(ValueError):
        tier_for_score(-0.1)


# ---------------------------------------------------------------------------
# Breakdown arithmetic
# ---------------------------------------------------------------------------


def test_breakdown_components_sum_to_score() -> None:
    inputs = TrustInputs(
        success_rate=0.42,
        policy_compliance=0.83,
        resource_efficiency=0.17,
        stability_score=0.95,
    )
    result = score_trust(inputs)
    summed = sum(result.breakdown.values())
    # The stored score is rounded to 4dp; the raw breakdown sum should match
    # the score within rounding tolerance.
    assert math.isclose(round(summed, 4), result.score, abs_tol=1e-9)


def test_breakdown_keys_are_complete() -> None:
    inputs = TrustInputs(
        success_rate=0.5,
        policy_compliance=0.5,
        resource_efficiency=0.5,
        stability_score=0.5,
    )
    result = score_trust(inputs)
    assert set(result.breakdown.keys()) == {
        "success_rate_weighted",
        "policy_compliance_weighted",
        "resource_efficiency_weighted",
        "stability_score_weighted",
    }


# ---------------------------------------------------------------------------
# can_access ordering
# ---------------------------------------------------------------------------


def test_can_access_autonomous_satisfies_restricted() -> None:
    assert (
        can_access(
            tier_required=AccessTier.RESTRICTED_TOOLS,
            agent_tier=AccessTier.AUTONOMOUS,
        )
        is True
    )


def test_can_access_sandbox_does_not_satisfy_multi_agent() -> None:
    assert (
        can_access(
            tier_required=AccessTier.MULTI_AGENT,
            agent_tier=AccessTier.SANDBOX,
        )
        is False
    )


def test_can_access_same_tier_is_true() -> None:
    for tier in AccessTier:
        assert can_access(tier_required=tier, agent_tier=tier) is True


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_trust_result_is_immutable() -> None:
    result = score_trust(
        TrustInputs(
            success_rate=0.5,
            policy_compliance=0.5,
            resource_efficiency=0.5,
            stability_score=0.5,
        )
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.score = 0.9  # type: ignore[misc]


def test_trust_inputs_is_immutable() -> None:
    inputs = TrustInputs(
        success_rate=0.5,
        policy_compliance=0.5,
        resource_efficiency=0.5,
        stability_score=0.5,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        inputs.success_rate = 0.9  # type: ignore[misc]
