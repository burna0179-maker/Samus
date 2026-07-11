"""Tests for backend.strategy.saturation_monitor.

Exact-formula tests + boundary cases (no trials, fair-share floor, full share).
"""
from __future__ import annotations

import pytest

from backend.strategy.saturation_monitor import (
    SATURATION_FAIR_SHARE_FLOOR,
    compute_saturation_risk,
    saturation_risk_by_vertical,
)


def test_no_trials_is_zero_risk():
    """No total trials -> no signal -> risk 0.0."""
    assert compute_saturation_risk(0.0, 0.0) == 0.0


def test_share_below_floor_is_zero_risk():
    """A vertical below the fair-share floor is under-explored -> risk 0.0."""
    # share = 0.5/10 = 0.05 < 0.10 floor
    assert compute_saturation_risk(0.5, 10.0) == 0.0


def test_share_at_floor_is_zero_risk():
    """Exactly at the floor -> still 0.0 (boundary)."""
    # share = 1.0/10 = 0.10 == floor
    assert compute_saturation_risk(1.0, 10.0) == 0.0


def test_full_share_is_max_risk():
    """A vertical absorbing all trials -> risk 1.0."""
    assert compute_saturation_risk(10.0, 10.0) == pytest.approx(1.0)


def test_midpoint_share_exact():
    """Risk ramps linearly from floor to 1.0."""
    # share = 0.55; span = 1 - 0.10 = 0.90
    # risk = (0.55 - 0.10) / 0.90 = 0.45/0.90 = 0.5
    assert compute_saturation_risk(5.5, 10.0) == pytest.approx(0.5)


def test_risk_stays_in_unit_range():
    """Risk never escapes [0,1] even with odd inputs."""
    val = compute_saturation_risk(100.0, 10.0)  # share clamps to 1.0
    assert 0.0 <= val <= 1.0
    assert val == pytest.approx(1.0)


def test_saturation_risk_by_vertical_distribution():
    """Per-vertical risk computed against the summed total."""
    risks = saturation_risk_by_vertical({"hvac": 8.0, "plumbing": 1.0, "roofing": 1.0})
    # total = 10; hvac share 0.8 -> high risk; others 0.1 -> floor -> 0.0
    assert risks["hvac"] == pytest.approx((0.8 - 0.1) / 0.9)
    assert risks["plumbing"] == 0.0
    assert risks["roofing"] == 0.0


def test_saturation_risk_by_vertical_empty():
    """Empty input -> empty map."""
    assert saturation_risk_by_vertical({}) == {}


def test_fair_share_floor_constant():
    """Floor constant matches the documented value."""
    assert SATURATION_FAIR_SHARE_FLOOR == 0.1
