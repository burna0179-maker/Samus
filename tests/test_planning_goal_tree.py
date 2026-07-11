"""Goal-tree decomposition (HOTL Tranche 4) — the funnel arithmetic + seeding.

The decomposition math (build_goal_tree) is pure given injected economics, so
most assertions hand-compute the expected values. Seeding/idempotency tests use
tmp store paths.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from backend.planning import goal_tree as gt
from backend.planning import store
from backend.planning.goal_tree import FunnelEconomics
from backend.planning.models import (
    HORIZON_30D,
    HORIZON_90D,
    HORIZON_DAY,
    HORIZON_WEEK,
    HORIZON_YEAR,
)


@pytest.fixture
def planning_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_GOALS_PATH", str(tmp_path / "goals.json"))
    monkeypatch.setenv("SAMUS_PLANS_PATH", str(tmp_path / "plans.json"))
    return tmp_path


# --- funnel arithmetic (pure) ----------------------------------------------


def test_funnel_leads_for_revenue():
    econ = FunnelEconomics(avg_deal_usd=3500.0, close_rate=0.15, source="test")
    # needed_leads = revenue / (avg_deal * close_rate)
    # = 10500 / (3500 * 0.15) = 10500 / 525 = 20
    assert econ.leads_for_revenue(10500.0) == pytest.approx(20.0)


def test_funnel_leads_for_revenue_floors_close_rate():
    # A degenerate close_rate is floored so we never divide by ~0 -> inf.
    econ = FunnelEconomics(avg_deal_usd=3500.0, close_rate=0.0, source="test")
    leads = econ.leads_for_revenue(3500.0)
    assert leads < 1e6  # bounded (floor kicks in), not infinite
    assert leads > 0


def test_funnel_leads_for_zero_revenue_is_zero():
    econ = FunnelEconomics(avg_deal_usd=3500.0, close_rate=0.15, source="test")
    assert econ.leads_for_revenue(0.0) == 0.0


# --- tree shape ------------------------------------------------------------


def test_build_goal_tree_has_all_horizons():
    today = _dt.date(2026, 1, 1)
    target_date = _dt.date(2026, 12, 31)  # ~364 days out
    econ = FunnelEconomics(avg_deal_usd=3500.0, close_rate=0.15, source="test")
    goals = gt.build_goal_tree(
        target_usd=40000.0,
        target_date=target_date,
        today=today,
        economics=econ,
    )
    horizons = [g.horizon for g in goals]
    assert HORIZON_YEAR in horizons
    assert HORIZON_90D in horizons
    assert HORIZON_30D in horizons
    assert HORIZON_WEEK in horizons
    assert horizons.count(HORIZON_DAY) == 2  # leads goal + tasks goal


def test_build_goal_tree_parent_links_form_a_chain():
    today = _dt.date(2026, 1, 1)
    target_date = _dt.date(2026, 12, 31)
    goals = gt.build_goal_tree(
        target_usd=40000.0,
        target_date=target_date,
        today=today,
        economics=FunnelEconomics(3500.0, 0.15, "test"),
    )
    by_horizon = {g.horizon: g for g in goals if g.horizon != HORIZON_DAY}
    # year is root (no parent); each descends from the horizon above it.
    assert by_horizon[HORIZON_YEAR].parent_id == ""
    assert by_horizon[HORIZON_90D].parent_id == by_horizon[HORIZON_YEAR].id
    assert by_horizon[HORIZON_30D].parent_id == by_horizon[HORIZON_90D].id
    assert by_horizon[HORIZON_WEEK].parent_id == by_horizon[HORIZON_30D].id
    # daily goals hang off the weekly goal
    daily = [g for g in goals if g.horizon == HORIZON_DAY]
    for d in daily:
        assert d.parent_id == by_horizon[HORIZON_WEEK].id


def test_build_goal_tree_revenue_splits_by_days():
    # 365 days remaining, $36500 target -> $100/day run-rate.
    today = _dt.date(2026, 1, 1)
    target_date = today + _dt.timedelta(days=365)
    goals = gt.build_goal_tree(
        target_usd=36500.0,
        target_date=target_date,
        today=today,
        economics=FunnelEconomics(3500.0, 0.15, "test"),
    )
    by_horizon = {g.horizon: g for g in goals if g.horizon != HORIZON_DAY}
    assert by_horizon[HORIZON_YEAR].target_value == pytest.approx(36500.0)
    # 90d window: 100/day * 90 = 9000
    assert by_horizon[HORIZON_90D].target_value == pytest.approx(9000.0, abs=1.0)
    # 30d window: 100/day * 30 = 3000
    assert by_horizon[HORIZON_30D].target_value == pytest.approx(3000.0, abs=1.0)
    # week window: 100/day * 7 = 700
    assert by_horizon[HORIZON_WEEK].target_value == pytest.approx(700.0, abs=1.0)


def test_build_goal_tree_daily_leads_from_funnel():
    # Weekly revenue -> leads via funnel. 365d, $36500 -> $100/day -> $700/week.
    # avg_deal=3500, close_rate=0.10 -> deal-per-lead value = 350
    # weekly_leads = 700 / 350 = 2 -> daily = 2/7 = 0.29 -> ceil to 1
    today = _dt.date(2026, 1, 1)
    target_date = today + _dt.timedelta(days=365)
    goals = gt.build_goal_tree(
        target_usd=36500.0,
        target_date=target_date,
        today=today,
        economics=FunnelEconomics(3500.0, 0.10, "test"),
    )
    leads_goal = next(
        g for g in goals if g.horizon == HORIZON_DAY and g.target_metric == "leads_created"
    )
    assert leads_goal.target_value >= 1.0
    tasks_goal = next(
        g for g in goals if g.horizon == HORIZON_DAY and g.target_metric == "tasks_completed"
    )
    assert tasks_goal.target_value >= 1.0


def test_build_goal_tree_near_deadline_clamps_windows():
    # Deadline only 3 days out: every window clamps to 3 days, so 90d/30d/week
    # all carry the same (full) remaining revenue. This is correct, not a bug.
    today = _dt.date(2026, 7, 6)
    target_date = _dt.date(2026, 7, 9)  # 3 days
    goals = gt.build_goal_tree(
        target_usd=40000.0,
        target_date=target_date,
        today=today,
        economics=FunnelEconomics(3500.0, 0.15, "test"),
    )
    by_horizon = {g.horizon: g for g in goals if g.horizon != HORIZON_DAY}
    # daily run-rate = 40000/3; each window = run-rate * 3 = 40000
    assert by_horizon[HORIZON_90D].target_value == pytest.approx(40000.0, abs=1.0)
    assert by_horizon[HORIZON_WEEK].target_value == pytest.approx(40000.0, abs=1.0)


def test_build_goal_tree_past_deadline_still_produces_tree():
    # Deadline in the past: days_remaining floored to 1, tree still builds.
    today = _dt.date(2026, 7, 6)
    target_date = _dt.date(2026, 7, 1)  # already passed
    goals = gt.build_goal_tree(
        target_usd=40000.0,
        target_date=target_date,
        today=today,
        economics=FunnelEconomics(3500.0, 0.15, "test"),
    )
    assert len(goals) == 6
    year = next(g for g in goals if g.horizon == HORIZON_YEAR)
    assert year.metadata["days_remaining"] == 1


# --- seeding + idempotency -------------------------------------------------


def test_seed_goal_tree_persists(planning_paths):
    goals = gt.seed_goal_tree(
        target_usd=40000.0,
        target_date=_dt.date(2026, 12, 31),
        today=_dt.date(2026, 1, 1),
    )
    assert len(goals) == 6
    stored = store.list_goals()
    assert len(stored) == 6


def test_seed_goal_tree_idempotent(planning_paths):
    kw = dict(target_usd=40000.0, target_date=_dt.date(2026, 12, 31), today=_dt.date(2026, 1, 1))
    gt.seed_goal_tree(**kw)
    gt.seed_goal_tree(**kw)  # re-seed same day -> same deterministic ids
    stored = store.list_goals()
    # Deterministic ids mean re-seeding refreshes in place, no duplicates.
    assert len(stored) == 6


def test_ensure_goal_tree_seeds_once_then_reuses(planning_paths):
    first = gt.ensure_goal_tree(today=_dt.date(2026, 1, 1))
    assert len(first) == 6
    # Second call sees existing active goals and does NOT re-seed with today's
    # date (which would create new deterministic ids for a different day).
    second = gt.ensure_goal_tree(today=_dt.date(2026, 6, 1))
    assert len(second) == 6
    assert len(store.list_goals()) == 6


def test_read_funnel_economics_never_raises(planning_paths, monkeypatch):
    # Even with no ROI data on this host, returns a usable economics object.
    econ = gt.read_funnel_economics()
    assert econ.avg_deal_usd > 0
    assert econ.close_rate > 0
    assert isinstance(econ.source, str)
