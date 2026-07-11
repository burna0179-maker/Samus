"""Tests for the CODB-coverage goal-pace signal.

behind_pace = MRR < CODB (behind => push); False if covering burn; None if
either side is unknown (moderate default, never a false 'behind')."""
from __future__ import annotations

from backend.cash_engine.goal_pace import (
    _combine,
    compute_behind_pace,
    deadline_behind,
)


def _pace(mrr, codb):
    return compute_behind_pace(mrr_reader=lambda: mrr, codb_reader=lambda: codb)


def test_underwater_is_behind():
    # MRR 300 vs CODB 5749 -> behind, push.
    assert _pace(300.0, 5749.0) is True


def test_covering_burn_is_not_behind():
    assert _pace(6000.0, 5749.0) is False


def test_exactly_break_even_is_not_behind():
    assert _pace(5749.0, 5749.0) is False  # 5749 < 5749 is False


def test_zero_mrr_is_behind():
    assert _pace(0.0, 5749.0) is True


def test_unknown_mrr_is_none():
    assert _pace(None, 5749.0) is None


def test_unknown_codb_is_none():
    assert _pace(300.0, None) is None


def test_zero_codb_is_none_not_ahead():
    # No burn on record must NOT read as 'ahead' (which would silence the drive).
    assert _pace(300.0, 0.0) is None


def test_reader_fault_degrades_to_none():
    def _boom():
        raise RuntimeError("finance down")

    assert compute_behind_pace(mrr_reader=_boom, codb_reader=lambda: 5749.0) is None
    assert compute_behind_pace(mrr_reader=lambda: 300.0, codb_reader=_boom) is None


# --- deadline run-rate signal ------------------------------------------------

def _dl(goal=40000, total=30, remaining=10, daily=None):
    return deadline_behind(goal_usd=goal, total_campaign_days=total,
                           days_remaining=remaining, recent_daily_revenue=daily)


def test_deadline_behind_when_velocity_below_required():
    # need 40000/30 = $1333/day; only doing $500/day -> behind.
    assert _dl(daily=500.0) is True


def test_deadline_not_behind_when_velocity_meets_required():
    assert _dl(daily=1500.0) is False  # >= 1333/day


def test_deadline_unknown_velocity_is_none():
    assert _dl(daily=None) is None


def test_deadline_passed_defers_to_coverage():
    assert _dl(daily=0.0, remaining=-1) is None


# --- OR-combination of coverage + deadline -----------------------------------

def test_combine_any_behind_wins():
    assert _combine(False, True) is True   # covering burn but behind deadline
    assert _combine(True, False) is True
    assert _combine(False, False) is False  # sustainable AND on-track -> hold
    assert _combine(None, None) is None
    assert _combine(False, None) is False
    assert _combine(None, True) is True


def test_compute_with_deadline_reader_ors_the_signals():
    # covering burn (coverage False) but behind deadline (True) -> overall behind.
    out = compute_behind_pace(
        mrr_reader=lambda: 6000.0, codb_reader=lambda: 5890.0,
        deadline_reader=lambda: True,
    )
    assert out is True


# --- threading into the idle-drive decision ----------------------------------

def test_behind_pace_flows_into_decision_and_intensity():
    from backend.cash_engine.idle_production import decide_idle_production
    d = decide_idle_production(
        enabled=True, now_ts=1000.0, last_activity_ts=None,
        in_business_hours=True, behind_pace=True, idle_threshold_s=100.0,
    )
    assert d.should_produce is True and d.behind_pace is True
    assert "behind pace" in d.reason

    # And the portfolio turns behind=True into full-capacity intent.
    from backend.cash_engine.campaign_portfolio import target_campaign_count
    assert target_campaign_count(behind_pace=d.behind_pace, max_concurrent=4) == 4


def test_covering_burn_holds_the_drive():
    from backend.cash_engine.idle_production import decide_idle_production
    d = decide_idle_production(
        enabled=True, now_ts=10_000.0, last_activity_ts=None,
        in_business_hours=True, behind_pace=False, idle_threshold_s=100.0,
    )
    assert d.should_produce is False and d.reason == "on/ahead of pace"
