"""Tests for backend.strategy.regret_engine.

Exact-formula tests + the missing-telemetry default-token-spend path.
"""
from __future__ import annotations

import pytest

from backend.strategy.regret_engine import (
    DEFAULT_TOKEN_SPEND,
    RegretLedger,
    cumulative_regret,
    regret_per_token,
)


def test_cumulative_regret_exact():
    """Sum of clamped per-trial regret against a fixed optimum."""
    # best=1.0; rewards 1.0, 0.5, 0.0 -> regrets 0.0, 0.5, 1.0 -> 1.5
    assert cumulative_regret([1.0, 0.5, 0.0], 1.0) == pytest.approx(1.5)


def test_cumulative_regret_empty_series_is_zero():
    """No rewards -> no regret."""
    assert cumulative_regret([], 1.0) == 0.0


def test_cumulative_regret_clamps_negative():
    """A reward above the optimum contributes 0 regret, never negative."""
    assert cumulative_regret([2.0, 1.5], 1.0) == 0.0


def test_regret_per_token_exact():
    """regret / token_spend."""
    assert regret_per_token(10.0, 4.0) == pytest.approx(2.5)


def test_regret_per_token_default_token_spend():
    """Missing per-arm token telemetry -> neutral default of 1.0."""
    # regret_per_token(3.0) with DEFAULT_TOKEN_SPEND=1.0 -> 3.0
    assert DEFAULT_TOKEN_SPEND == 1.0
    assert regret_per_token(3.0) == pytest.approx(3.0)


def test_regret_per_token_zero_spend_no_divide_by_zero():
    """Zero token spend uses the epsilon floor, never raises."""
    value = regret_per_token(1.0, 0.0)
    assert value > 0.0  # divided by epsilon, finite


def test_regret_ledger_records_and_totals():
    """RegretLedger accumulates per-arm regret across trials."""
    ledger = RegretLedger()
    # best=1.0
    ledger.record("arm_a", reward=1.0, best_possible_reward=1.0)  # regret 0
    ledger.record("arm_a", reward=0.4, best_possible_reward=1.0)  # regret 0.6
    ledger.record("arm_b", reward=0.0, best_possible_reward=1.0)  # regret 1.0

    assert ledger.for_arm("arm_a") == pytest.approx(0.6)
    assert ledger.for_arm("arm_b") == pytest.approx(1.0)
    assert ledger.total() == pytest.approx(1.6)
    assert ledger.trial_count == 3


def test_regret_ledger_unknown_arm_is_zero():
    """An arm never recorded has zero regret."""
    ledger = RegretLedger()
    assert ledger.for_arm("never_seen") == 0.0


def test_regret_ledger_record_returns_clamped_increment():
    """record() returns the clamped incremental regret for that trial."""
    ledger = RegretLedger()
    inc = ledger.record("arm_x", reward=2.0, best_possible_reward=1.0)
    assert inc == 0.0  # clamped, reward beat the optimum
