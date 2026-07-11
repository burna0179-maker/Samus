"""Planning store (HOTL Tranche 4) — Goal/Plan persistence round-trips.

Store isolation follows the approvals/business-events test pattern: point the
SAMUS_GOALS_PATH / SAMUS_PLANS_PATH env vars at a tmp_path file per test so the
JSON fallback is used with a clean slate (no DDB in the test env).
"""
from __future__ import annotations

import pytest

from backend.planning import store
from backend.planning.models import (
    GOAL_ACTIVE,
    GOAL_MET,
    HORIZON_DAY,
    HORIZON_YEAR,
    PLAN_ACTIVE,
    PLAN_SUPERSEDED,
    Assumption,
    Goal,
    Plan,
    PlanStep,
)


@pytest.fixture
def planning_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_GOALS_PATH", str(tmp_path / "goals.json"))
    monkeypatch.setenv("SAMUS_PLANS_PATH", str(tmp_path / "plans.json"))
    return tmp_path


# --- Goal round-trip -------------------------------------------------------

def test_save_and_get_goal(planning_paths):
    g = Goal(
        id="goal::year::2026",
        horizon=HORIZON_YEAR,
        target_metric="revenue_usd",
        target_value=40000.0,
        label="hit $40k",
    )
    store.save_goal(g)
    loaded = store.get_goal("goal::year::2026")
    assert loaded is not None
    assert loaded.id == g.id
    assert loaded.horizon == HORIZON_YEAR
    assert loaded.target_metric == "revenue_usd"
    assert loaded.target_value == 40000.0
    assert loaded.status == GOAL_ACTIVE
    # created_at / updated_at stamped on save
    assert loaded.created_at
    assert loaded.updated_at


def test_get_goal_unknown_returns_none(planning_paths):
    assert store.get_goal("nope") is None


def test_save_goal_overwrites_in_place(planning_paths):
    g = Goal(id="g1", horizon=HORIZON_DAY, target_metric="leads_created",
             target_value=10.0)
    store.save_goal(g)
    g.status = GOAL_MET
    g.target_value = 12.0
    store.save_goal(g)
    all_goals = store.list_goals()
    assert len(all_goals) == 1
    assert all_goals[0].status == GOAL_MET
    assert all_goals[0].target_value == 12.0


def test_list_goals_filters(planning_paths):
    store.save_goals([
        Goal(id="y", horizon=HORIZON_YEAR, target_metric="revenue_usd",
             target_value=40000.0),
        Goal(id="d1", horizon=HORIZON_DAY, target_metric="leads_created",
             target_value=10.0),
        Goal(id="d2", horizon=HORIZON_DAY, target_metric="tasks_completed",
             target_value=10.0, status=GOAL_MET),
    ])
    assert len(store.list_goals(horizon=HORIZON_DAY)) == 2
    assert len(store.list_goals(horizon=HORIZON_YEAR)) == 1
    assert len(store.list_goals(status=GOAL_ACTIVE)) == 2
    assert len(store.list_goals(status=GOAL_MET)) == 1


def test_list_goals_by_parent(planning_paths):
    store.save_goals([
        Goal(id="y", horizon=HORIZON_YEAR, target_metric="revenue_usd",
             target_value=40000.0, parent_id=""),
        Goal(id="q", horizon="90d", target_metric="revenue_usd",
             target_value=10000.0, parent_id="y"),
    ])
    children = store.list_goals(parent_id="y")
    assert [g.id for g in children] == ["q"]
    roots = store.list_goals(parent_id="")
    assert [g.id for g in roots] == ["y"]


# --- Plan round-trip -------------------------------------------------------

def test_save_and_get_plan(planning_paths):
    p = Plan(
        id="p1",
        goal_id="g1",
        plan_generation=1,
        assumptions=[Assumption(id="a1", description=">=8 leads/day",
                                metric="leads_per_day", op=">=", threshold=8.0,
                                window_days=7)],
        steps=[PlanStep(name="email", channel="email", action="send",
                        target_value=8.0)],
        rationale="initial",
    )
    store.save_plan(p)
    loaded = store.get_plan("p1")
    assert loaded is not None
    assert loaded.goal_id == "g1"
    assert loaded.plan_generation == 1
    assert len(loaded.assumptions) == 1
    assert loaded.assumptions[0].metric == "leads_per_day"
    assert loaded.assumptions[0].threshold == 8.0
    assert len(loaded.steps) == 1
    assert loaded.steps[0].channel == "email"


def test_list_plans_filters(planning_paths):
    store.save_plan(Plan(id="p1", goal_id="g1", plan_generation=1,
                         status=PLAN_SUPERSEDED))
    store.save_plan(Plan(id="p2", goal_id="g1", plan_generation=2,
                         status=PLAN_ACTIVE))
    store.save_plan(Plan(id="p3", goal_id="g2", plan_generation=1,
                         status=PLAN_ACTIVE))
    assert len(store.list_plans(goal_id="g1")) == 2
    assert len(store.list_plans(status=PLAN_ACTIVE)) == 2
    assert len(store.list_plans(goal_id="g1", status=PLAN_ACTIVE)) == 1


def test_active_plan_for_goal_picks_latest_generation(planning_paths):
    store.save_plan(Plan(id="p1", goal_id="g1", plan_generation=1,
                         status=PLAN_ACTIVE))
    store.save_plan(Plan(id="p2", goal_id="g1", plan_generation=2,
                         status=PLAN_ACTIVE))
    active = store.active_plan_for_goal("g1")
    assert active is not None
    assert active.plan_generation == 2
    assert active.id == "p2"


def test_active_plan_for_goal_none_when_all_superseded(planning_paths):
    store.save_plan(Plan(id="p1", goal_id="g1", plan_generation=1,
                         status=PLAN_SUPERSEDED))
    assert store.active_plan_for_goal("g1") is None


def test_latest_generation_for_goal(planning_paths):
    assert store.latest_generation_for_goal("g1") == 0
    store.save_plan(Plan(id="p1", goal_id="g1", plan_generation=1))
    store.save_plan(Plan(id="p2", goal_id="g1", plan_generation=3))
    assert store.latest_generation_for_goal("g1") == 3


# --- degradation -----------------------------------------------------------

def test_store_reads_empty_when_no_file(planning_paths):
    # No writes yet -> both lists empty, no raise.
    assert store.list_goals() == []
    assert store.list_plans() == []
