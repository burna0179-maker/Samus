"""Tests for backend.strategy.reward_density.

Exact-formula tests with known inputs -> known outputs, boundary clamping,
and graceful-degradation when the neutral-default telemetry fields are omitted.
"""

from __future__ import annotations

import pytest

from backend.strategy.reward_density import (
    EFFICIENCY_WEIGHT,
    ENRICHMENT_FACEBOOK_WEIGHT,
    ENRICHMENT_INSTAGRAM_WEIGHT,
    ENRICHMENT_OWNER_EMAIL_WEIGHT,
    LATENCY_SATURATION_SEC,
    RewardSignal,
    compute_reward_density,
)


def test_coefficients_match_spec():
    """Named coefficients must equal the spec values."""
    assert ENRICHMENT_OWNER_EMAIL_WEIGHT == 0.30
    assert ENRICHMENT_FACEBOOK_WEIGHT == 0.20
    assert ENRICHMENT_INSTAGRAM_WEIGHT == 0.10
    assert EFFICIENCY_WEIGHT == 0.25
    assert LATENCY_SATURATION_SEC == 86_400.0


def test_compute_reward_density_exact_known_inputs():
    """Hand-computed exact value for a fully-specified signal."""
    signal = RewardSignal(
        outcome=1.0,
        owner_email=True,
        social_facebook=True,
        social_instagram=False,
        seo_score=0.4,
        contactability=0.5,
        infrastructure_health=0.8,
        estimated_close_probability=0.6,
        latency_to_resolution_sec=43_200.0,  # half a day
        token_cost_usd=0.02,
    )
    # enrichment = 0.30*1 + 0.20*1 + 0.10*0 = 0.50
    # infrastructure = 0.8*0.20 + 0.5*0.20 = 0.16 + 0.10 = 0.26
    # seo_gap = (1.0 - 0.4)*0.15 = 0.09
    # efficiency = (0.6 / 0.02) * 0.25 = 30 * 0.25 = 7.5
    # latency = min(43200/86400, 1.0)*0.20 = 0.5*0.20 = 0.10
    # density = 1.0 + 0.50 + 0.26 + 0.09 + 7.5 - 0.10 = 9.25
    assert compute_reward_density(signal) == pytest.approx(9.25)


def test_compute_reward_density_all_false_minimal():
    """No enrichment, neutral defaults via explicit minimal construction."""
    signal = RewardSignal(
        outcome=0.0,
        owner_email=False,
        social_facebook=False,
        social_instagram=False,
        seo_score=1.0,
        contactability=0.0,
        infrastructure_health=0.0,
    )
    # enrichment = 0, infrastructure = 0, seo_gap = (1-1)*0.15 = 0
    # efficiency = (0.5 / 0.01) * 0.25 = 50 * 0.25 = 12.5
    # latency = 0
    assert compute_reward_density(signal) == pytest.approx(12.5)


def test_neutral_defaults_no_raise_when_telemetry_absent():
    """Omitting estimated_close_probability / latency / token_cost must not raise."""
    signal = RewardSignal(
        outcome=1.0,
        owner_email=True,
        social_facebook=False,
        social_instagram=False,
        seo_score=0.5,
        contactability=0.5,
        infrastructure_health=0.5,
    )
    # Uses neutral defaults: close_prob=0.5, latency=0.0, token_cost=0.01
    value = compute_reward_density(signal)
    assert isinstance(value, float)
    # efficiency = (0.5/0.01)*0.25 = 12.5; enrichment=0.30; infra=0.20; seo_gap=0.075
    assert value == pytest.approx(1.0 + 0.30 + 0.20 + 0.075 + 12.5)


def test_zero_token_cost_clamped_no_divide_by_zero():
    """token_cost_usd=0.0 must clamp to the floor, not divide by zero."""
    signal = RewardSignal(
        outcome=0.0,
        owner_email=False,
        social_facebook=False,
        social_instagram=False,
        seo_score=0.5,
        contactability=0.0,
        infrastructure_health=0.0,
        estimated_close_probability=0.5,
        token_cost_usd=0.0,
    )
    value = compute_reward_density(signal)
    # token cost clamps to 0.01: efficiency = (0.5/0.01)*0.25 = 12.5
    assert value == pytest.approx(0.075 + 12.5)


def test_seo_score_clamped_above_one():
    """seo_score > 1 (e.g. a 0-100 audit score) clamps so seo_gap stays >= 0."""
    signal = RewardSignal(
        outcome=0.0,
        owner_email=False,
        social_facebook=False,
        social_instagram=False,
        seo_score=75.0,  # out of [0,1] range
        contactability=0.0,
        infrastructure_health=0.0,
    )
    value = compute_reward_density(signal)
    # seo clamps to 1.0 -> seo_gap = 0; never negative
    assert value == pytest.approx((0.5 / 0.01) * 0.25)


def test_latency_penalty_saturates_at_one_day():
    """Latency past 86400s saturates the penalty at the full weight (0.20)."""
    base = dict(
        outcome=0.0,
        owner_email=False,
        social_facebook=False,
        social_instagram=False,
        seo_score=1.0,
        contactability=0.0,
        infrastructure_health=0.0,
        estimated_close_probability=0.0,
        token_cost_usd=1.0,
    )
    one_day = compute_reward_density(RewardSignal(latency_to_resolution_sec=86_400.0, **base))
    two_days = compute_reward_density(RewardSignal(latency_to_resolution_sec=172_800.0, **base))
    # Both saturate to the same -0.20 penalty.
    assert one_day == pytest.approx(two_days)
    assert one_day == pytest.approx(-0.20)


def test_infrastructure_and_contactability_clamped():
    """Out-of-range infra/contactability clamp into [0,1]."""
    signal = RewardSignal(
        outcome=0.0,
        owner_email=False,
        social_facebook=False,
        social_instagram=False,
        seo_score=1.0,
        contactability=5.0,  # clamps to 1.0
        infrastructure_health=-2.0,  # clamps to 0.0
        estimated_close_probability=0.0,
        token_cost_usd=1.0,
    )
    value = compute_reward_density(signal)
    # infra = 0.0*0.20 + 1.0*0.20 = 0.20
    assert value == pytest.approx(0.20)
