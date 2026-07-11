"""Tests for backend.strategy.predictive_allocator.

Exact-formula forecast_score tests + closer-mode boundary tests at 0.65/0.82
+ should_proactively_shift threshold behaviour.
"""
from __future__ import annotations

import pytest

from backend.strategy.momentum_tracker import IndustryForecast
from backend.strategy.predictive_allocator import (
    CLOSER_AGGRESSIVE_THRESHOLD,
    CLOSER_HYBRID_THRESHOLD,
    FORECAST_REWARD_DENSITY_WEIGHT,
    closer_mode_for,
    forecast_score,
    should_proactively_shift,
)


def _forecast(**overrides) -> IndustryForecast:
    base = dict(
        vertical="hvac",
        reward_density=0.0,
        momentum=0.0,
        ema_trend=0.0,
        token_efficiency=0.0,
        saturation_risk=0.0,
    )
    base.update(overrides)
    return IndustryForecast(**base)


def test_forecast_coefficients_match_spec():
    """Named coefficient matches the spec value."""
    assert FORECAST_REWARD_DENSITY_WEIGHT == 0.40


def test_forecast_score_exact():
    """Hand-computed forecast score."""
    f = _forecast(
        reward_density=1.0,
        momentum=0.8,
        ema_trend=0.5,
        token_efficiency=0.4,
        saturation_risk=0.2,
    )
    # 1.0*0.40 + 0.8*0.25 + 0.5*0.20 + 0.4*0.15 - 0.2
    # = 0.40 + 0.20 + 0.10 + 0.06 - 0.20 = 0.56
    assert forecast_score(f) == pytest.approx(0.56)


def test_forecast_score_saturation_penalty():
    """Saturation risk is a straight unit-weight subtraction."""
    low = forecast_score(_forecast(reward_density=1.0, saturation_risk=0.0))
    high = forecast_score(_forecast(reward_density=1.0, saturation_risk=0.5))
    assert low - high == pytest.approx(0.5)


def test_closer_mode_aggressive_above_threshold():
    """Score strictly above 0.82 -> aggressive."""
    assert closer_mode_for(0.83) == "aggressive"
    assert closer_mode_for(0.95) == "aggressive"


def test_closer_mode_boundary_at_aggressive_threshold():
    """Exactly 0.82 is NOT aggressive (strict >)."""
    assert CLOSER_AGGRESSIVE_THRESHOLD == 0.82
    assert closer_mode_for(0.82) == "hybrid"


def test_closer_mode_hybrid_band():
    """Score above 0.65 but not above 0.82 -> hybrid."""
    assert closer_mode_for(0.66) == "hybrid"
    assert closer_mode_for(0.80) == "hybrid"


def test_closer_mode_boundary_at_hybrid_threshold():
    """Exactly 0.65 is NOT hybrid (strict >) -> template_only."""
    assert CLOSER_HYBRID_THRESHOLD == 0.65
    assert closer_mode_for(0.65) == "template_only"


def test_closer_mode_template_only_below_hybrid():
    """Score at or below 0.65 -> template_only."""
    assert closer_mode_for(0.5) == "template_only"
    assert closer_mode_for(0.0) == "template_only"
    assert closer_mode_for(-1.0) == "template_only"


def test_should_proactively_shift_empty_is_false():
    """No forecasts -> no signal -> False."""
    assert should_proactively_shift([], threshold=0.6) is False


def test_should_proactively_shift_fires_above_threshold():
    """A single vertical clearing the threshold fires the shift."""
    strong = _forecast(reward_density=2.0)  # score = 0.80 >= 0.6
    weak = _forecast(reward_density=0.1)
    assert should_proactively_shift([weak, strong], threshold=0.6) is True


def test_should_proactively_shift_no_fire_below_threshold():
    """All verticals below the threshold -> no shift."""
    weak = _forecast(reward_density=0.1)  # score = 0.04
    assert should_proactively_shift([weak], threshold=0.6) is False


def test_should_proactively_shift_boundary_inclusive():
    """A forecast exactly at the threshold fires (>=)."""
    # reward_density 1.5 -> score 0.60 exactly
    at_threshold = _forecast(reward_density=1.5)
    assert forecast_score(at_threshold) == pytest.approx(0.6)
    assert should_proactively_shift([at_threshold], threshold=0.6) is True
