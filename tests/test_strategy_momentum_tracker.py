"""Tests for backend.strategy.momentum_tracker.

Exact-formula tests + boundary cases (empty / single-point series).
"""
from __future__ import annotations

import pytest

from backend.strategy.momentum_tracker import (
    IndustryForecast,
    compute_ema,
    compute_ema_trend,
    compute_momentum,
)


def test_momentum_empty_series_is_zero():
    """No history -> no change -> 0.0."""
    assert compute_momentum([]) == 0.0


def test_momentum_single_point_is_zero():
    """One point -> nothing to compare -> 0.0."""
    assert compute_momentum([0.7]) == 0.0


def test_momentum_rising_series_positive():
    """Rising series -> positive momentum, normalised to [-1,1]."""
    # (1.0 - 0.0) / max(0, 1.0, eps) = 1.0
    assert compute_momentum([0.0, 0.5, 1.0]) == pytest.approx(1.0)


def test_momentum_falling_series_negative():
    """Falling series -> negative momentum."""
    # (0.0 - 1.0) / max(1.0, 0.0) = -1.0
    assert compute_momentum([1.0, 0.5, 0.0]) == pytest.approx(-1.0)


def test_momentum_clamped_to_unit_range():
    """Momentum never escapes [-1, 1]."""
    val = compute_momentum([0.1, 100.0])
    assert -1.0 <= val <= 1.0


def test_compute_ema_empty_is_zero():
    """Empty series -> 0.0."""
    assert compute_ema([]) == 0.0


def test_compute_ema_single_point_returns_point():
    """One point -> that point."""
    assert compute_ema([0.42]) == pytest.approx(0.42)


def test_compute_ema_exact_two_points():
    """EMA of two points with alpha=0.4 is hand-computable."""
    # ema starts at 1.0; second point 2.0: 0.4*2.0 + 0.6*1.0 = 1.4
    assert compute_ema([1.0, 2.0], alpha=0.4) == pytest.approx(1.4)


def test_ema_trend_empty_is_zero():
    """No history -> no trend."""
    assert compute_ema_trend([]) == 0.0


def test_ema_trend_single_point_is_zero():
    """One point -> no trend."""
    assert compute_ema_trend([0.5]) == 0.0


def test_ema_trend_rising_series_positive():
    """A clearly rising series yields a positive trend."""
    assert compute_ema_trend([0.1, 0.2, 0.8, 0.9]) > 0.0


def test_ema_trend_falling_series_negative():
    """A clearly falling series yields a negative trend."""
    assert compute_ema_trend([0.9, 0.8, 0.2, 0.1]) < 0.0


def test_industry_forecast_dataclass_fields():
    """IndustryForecast carries the six declared fields."""
    f = IndustryForecast(
        vertical="hvac",
        reward_density=2.0,
        momentum=0.3,
        ema_trend=0.1,
        token_efficiency=0.5,
        saturation_risk=0.2,
    )
    assert f.vertical == "hvac"
    assert f.reward_density == 2.0
    assert f.saturation_risk == 0.2
