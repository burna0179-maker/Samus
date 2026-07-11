"""DecayRiskScore — the signal_decay trigger's core computation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.cash_engine.decay import (
    STAGE_DECAY_WEIGHT,
    TERMINAL_STAGES,
    compute_decay_risk,
)

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
FMT = "%Y-%m-%dT%H:%M:%SZ"


class _Opp:
    """Duck-typed Opportunity stand-in for the pure decay function."""

    def __init__(
        self,
        stage: str,
        *,
        age_days: float | None = 0.0,
        opportunity_id: str = "op-1",
        prospect_id: str = "pr-1",
    ) -> None:
        self.stage = stage
        self.opportunity_id = opportunity_id
        self.prospect_id = prospect_id
        if age_days is None:
            self.created_at = ""
            self.updated_at = ""
        else:
            stamp = (NOW - timedelta(days=age_days)).strftime(FMT)
            self.created_at = stamp
            self.updated_at = stamp


def test_proposal_gone_cold_is_max_risk():
    a = compute_decay_risk(_Opp("proposal", age_days=30), stall_days=7, now=NOW)
    assert a.stage_weight == STAGE_DECAY_WEIGHT["proposal"] == 1.0
    assert a.stall_factor == 1.0
    assert a.decay_risk == 1.0
    assert a.crosses(0.6) is True
    assert a.is_terminal is False
    assert round(a.staleness_days) == 30


def test_fresh_new_lead_is_no_risk():
    a = compute_decay_risk(_Opp("new", age_days=0), stall_days=7, now=NOW)
    assert a.stall_factor == 0.0
    assert a.decay_risk == 0.0
    assert a.crosses(0.6) is False


def test_terminal_stage_never_fires():
    for stage in TERMINAL_STAGES:
        a = compute_decay_risk(_Opp(stage, age_days=90), stall_days=7, now=NOW)
        assert a.is_terminal is True
        assert a.decay_risk == 0.0
        assert a.crosses(0.0) is False  # not even at a zero threshold


def test_qualified_at_window_is_half_risk():
    a = compute_decay_risk(_Opp("qualified", age_days=7), stall_days=7, now=NOW)
    assert a.stall_factor == 1.0
    assert a.decay_risk == 0.5
    assert a.crosses(0.6) is False
    assert a.crosses(0.4) is True


def test_external_factor_raises_risk_monotonically():
    base = compute_decay_risk(_Opp("new", age_days=7), stall_days=7, now=NOW)
    lifted = compute_decay_risk(
        _Opp("new", age_days=7), stall_days=7, external_factor=0.8, now=NOW,
    )
    # base = 0.25 * 1.0 = 0.25 ; lifted = 0.25 + 0.8*(1-0.25) = 0.85
    assert base.decay_risk == 0.25
    assert lifted.decay_risk == pytest.approx(0.85)
    assert lifted.decay_risk > base.decay_risk
    assert lifted.crosses(0.6) is True


def test_external_factor_clamped_to_one():
    a = compute_decay_risk(
        _Opp("new", age_days=7), stall_days=7, external_factor=5.0, now=NOW,
    )
    assert a.external_factor == 1.0
    assert a.decay_risk == 1.0


def test_unparseable_timestamps_yield_no_decay():
    a = compute_decay_risk(_Opp("proposal", age_days=None), stall_days=7, now=NOW)
    assert a.staleness_days == 0.0
    assert a.decay_risk == 0.0
    assert a.crosses(0.6) is False


def test_call_state_last_attempt_counts_as_contact():
    # Opp itself is stale (30d) but a recent dial resets the clock -> low risk.
    class _CS:
        last_attempt_at = NOW.strftime(FMT)

    a = compute_decay_risk(
        _Opp("proposal", age_days=30), call_state=_CS(), stall_days=7, now=NOW,
    )
    assert a.staleness_days == 0.0
    assert a.decay_risk == 0.0
