"""Planner + automatic replanning (HOTL Tranche 4) — plan generation,
assumption evaluation against the event stream, auto-replan (Plan B), and
operator escalation on budget/risk breach.

Isolation: tmp goals/plans/approvals/business-events paths per test. Budget
posture is injected by monkeypatching affordability.assess_affordability so we
can drive the conserve/lean/invest branches deterministically.
"""

from __future__ import annotations


import pytest

from backend.common import approvals, business_events
from backend.common.decision_record import list_decisions
from backend.planning import planner, store
from backend.planning.models import (
    HORIZON_DAY,
    PLAN_ACTIVE,
    PLAN_SUPERSEDED,
    Assumption,
    Goal,
)


@pytest.fixture
def iso_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_GOALS_PATH", str(tmp_path / "goals.json"))
    monkeypatch.setenv("SAMUS_PLANS_PATH", str(tmp_path / "plans.json"))
    monkeypatch.setenv("SAMUS_APPROVALS_PATH", str(tmp_path / "approvals.json"))
    monkeypatch.setenv("SAMUS_BUSINESS_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    monkeypatch.delenv("SAMUS_LEDGER_BACKEND", raising=False)
    return tmp_path


class _Posture:
    def __init__(self, posture):
        self.posture = posture


def _set_posture(monkeypatch, posture):
    monkeypatch.setattr(
        "backend.cash_engine.affordability.assess_affordability",
        lambda **_: _Posture(posture),
    )


def _daily_leads_goal(target=8.0):
    return Goal(
        id="goal::day::x::leads",
        horizon=HORIZON_DAY,
        target_metric="leads_created",
        target_value=target,
        label="create leads",
    )


# --- assumption metrics ----------------------------------------------------


def test_leads_per_day_metric_counts_stream(iso_env):
    for _ in range(14):
        business_events.emit_business_event(
            business_events.LEAD_CREATED,
            workcell="intake",
        )
    # 14 leads over a 7-day window => 2/day
    assert planner.compute_metric("leads_per_day", 7) == pytest.approx(2.0)


def test_connect_rate_metric(iso_env):
    for _ in range(10):
        business_events.emit_business_event(
            business_events.CALL_PLACED,
            workcell="voice",
        )
    for _ in range(3):
        business_events.emit_business_event(
            business_events.CALL_ANSWERED,
            workcell="voice",
        )
    assert planner.compute_metric("connect_rate", 7) == pytest.approx(0.3)


def test_connect_rate_zero_when_no_calls(iso_env):
    assert planner.compute_metric("connect_rate", 7) == 0.0


def test_unknown_metric_returns_zero(iso_env):
    assert planner.compute_metric("does_not_exist", 7) == 0.0


def test_check_assumption_holds_and_violates(iso_env):
    for _ in range(21):
        business_events.emit_business_event(
            business_events.LEAD_CREATED,
            workcell="intake",
        )
    # 21/7 = 3 leads/day
    a_ok = Assumption(
        id="a", description="", metric="leads_per_day", op=">=", threshold=2.0, window_days=7
    )
    holds, observed = planner.check_assumption(a_ok)
    assert holds is True
    assert observed == pytest.approx(3.0)

    a_bad = Assumption(
        id="b", description="", metric="leads_per_day", op=">=", threshold=8.0, window_days=7
    )
    holds2, observed2 = planner.check_assumption(a_bad)
    assert holds2 is False
    assert observed2 == pytest.approx(3.0)


# --- plan generation -------------------------------------------------------


def test_generate_plan_persists_with_assumptions_and_steps(iso_env, monkeypatch):
    _set_posture(monkeypatch, "invest")
    goal = _daily_leads_goal(8.0)
    store.save_goal(goal)
    plan = planner.generate_plan(goal)
    assert plan.plan_generation == 1
    assert plan.status == PLAN_ACTIVE
    assert plan.goal_id == goal.id
    assert plan.decision_id  # a decision record was minted
    assert len(plan.assumptions) >= 1
    # leads goal => a leads_per_day assumption at the goal's target threshold
    lead_assumptions = [a for a in plan.assumptions if a.metric == "leads_per_day"]
    assert lead_assumptions
    assert lead_assumptions[0].threshold == pytest.approx(8.0)
    # invest posture => a paid voice step exists
    channels = {s.channel for s in plan.steps}
    assert "call" in channels
    # persisted
    assert store.get_plan(plan.id) is not None


def test_generate_plan_posture_gates_channels(iso_env, monkeypatch):
    goal = _daily_leads_goal(8.0)
    store.save_goal(goal)

    _set_posture(monkeypatch, "conserve")
    conserve_plan = planner.generate_plan(goal, posture="conserve")
    conserve_actions = {s.action for s in conserve_plan.steps}
    # conserve: no paid email send, no paid dialing; only free operator queue
    assert "send_outreach" not in conserve_actions
    assert "place_calls" not in conserve_actions
    assert "queue_operator_calls" in conserve_actions

    lean_plan = planner.generate_plan(goal, posture="lean")
    lean_actions = {s.action for s in lean_plan.steps}
    assert "send_outreach" in lean_actions  # email allowed at lean
    assert "place_calls" not in lean_actions  # paid dialing still gated

    invest_plan = planner.generate_plan(goal, posture="invest")
    invest_actions = {s.action for s in invest_plan.steps}
    assert "place_calls" in invest_actions  # paid dialing allowed at invest


def test_generate_plan_replan_supersedes_prior(iso_env, monkeypatch):
    _set_posture(monkeypatch, "invest")
    goal = _daily_leads_goal(8.0)
    store.save_goal(goal)
    gen1 = planner.generate_plan(goal)
    gen2 = planner.generate_plan(goal, prior_plan=gen1, replan_reason="test replan")
    assert gen2.plan_generation == 2
    # prior flipped to superseded
    reloaded_gen1 = store.get_plan(gen1.id)
    assert reloaded_gen1.status == PLAN_SUPERSEDED
    assert store.active_plan_for_goal(goal.id).id == gen2.id


def test_generate_plan_mints_decision_record(iso_env, monkeypatch):
    _set_posture(monkeypatch, "invest")
    goal = _daily_leads_goal(8.0)
    store.save_goal(goal)
    planner.generate_plan(goal)
    decisions = list_decisions(actor=planner.PLANNER_ACTOR, limit=10)
    assert decisions
    assert any("Generated plan" in d["why"] for d in decisions)


# --- evaluate + replan -----------------------------------------------------


def test_evaluate_assumptions_flags_violations(iso_env, monkeypatch):
    _set_posture(monkeypatch, "invest")
    goal = _daily_leads_goal(8.0)
    store.save_goal(goal)
    planner.generate_plan(goal)  # assumption: >=8 leads/day, but stream empty
    violations = planner.evaluate_assumptions()
    assert violations
    assert any(v.goal.id == goal.id for v in violations)


def test_evaluate_and_replan_auto_swaps_when_no_breach(iso_env, monkeypatch):
    # invest posture + normal risk => replan happens automatically (no escalation)
    _set_posture(monkeypatch, "invest")
    goal = _daily_leads_goal(8.0)
    store.save_goal(goal)
    gen1 = planner.generate_plan(goal)  # empty stream violates >=8 leads/day
    summary = planner.evaluate_and_replan(ensure_tree=False)
    assert summary["ok"] is True
    assert summary["violations"] >= 1
    assert len(summary["replanned"]) >= 1
    assert summary["escalated"] == []
    # a new active generation exists; gen1 superseded
    active = store.active_plan_for_goal(goal.id)
    assert active.plan_generation >= 2
    assert store.get_plan(gen1.id).status == PLAN_SUPERSEDED


def test_evaluate_and_replan_does_not_churn_generations(iso_env, monkeypatch):
    # On a persistently-empty stream at a stable posture, the plan is replanned
    # ONCE (gen1 -> gen2) then HELD — it must not grow a generation every tick.
    _set_posture(monkeypatch, "invest")
    goal = _daily_leads_goal(8.0)
    store.save_goal(goal)
    planner.generate_plan(goal)  # gen1, violated (empty stream)

    s1 = planner.evaluate_and_replan(ensure_tree=False)  # -> gen2
    assert len(s1["replanned"]) == 1
    gen_after_first = store.active_plan_for_goal(goal.id).plan_generation
    assert gen_after_first == 2

    # Subsequent ticks: same posture -> identical candidate plan -> HELD.
    for _ in range(3):
        s = planner.evaluate_and_replan(ensure_tree=False)
        assert s["replanned"] == []
        assert s.get("held")
    # Generation did NOT grow past 2.
    assert store.active_plan_for_goal(goal.id).plan_generation == 2


def test_replan_churn_guard_still_swaps_on_posture_change(iso_env, monkeypatch):
    # gen1 at invest -> gen2 (first violation). Then posture shifts to lean:
    # steps materially change (paid dialing dropped) -> a real Plan B swaps.
    _set_posture(monkeypatch, "invest")
    goal = _daily_leads_goal(8.0)
    store.save_goal(goal)
    planner.generate_plan(goal)
    planner.evaluate_and_replan(ensure_tree=False)  # gen2 at invest
    assert store.active_plan_for_goal(goal.id).plan_generation == 2

    _set_posture(monkeypatch, "lean")  # steps change -> real Plan B
    s = planner.evaluate_and_replan(ensure_tree=False)
    assert len(s["replanned"]) == 1
    assert store.active_plan_for_goal(goal.id).plan_generation == 3


def test_evaluate_and_replan_escalates_on_conserve_posture(iso_env, monkeypatch):
    # conserve posture => a replan that increases spend needs operator approval;
    # it must NOT auto-swap, and an approval request must be created.
    _set_posture(monkeypatch, "conserve")
    goal = _daily_leads_goal(8.0)
    store.save_goal(goal)
    gen1 = planner.generate_plan(goal, posture="conserve")
    summary = planner.evaluate_and_replan(ensure_tree=False)
    assert summary["ok"] is True
    assert summary["violations"] >= 1
    assert summary["replanned"] == []  # NOT auto-swapped
    assert len(summary["escalated"]) >= 1
    esc = summary["escalated"][0]
    assert esc["approval_id"]
    # plan generation unchanged (still gen1, still active)
    assert store.active_plan_for_goal(goal.id).id == gen1.id
    # an approval landed in the queue
    pending = approvals.list_approvals(status="pending", kind="replan")
    assert pending
    assert pending[0]["payload"]["goal_id"] == goal.id


def test_escalation_mints_decision_record(iso_env, monkeypatch):
    _set_posture(monkeypatch, "conserve")
    goal = _daily_leads_goal(8.0)
    store.save_goal(goal)
    planner.generate_plan(goal, posture="conserve")
    planner.evaluate_and_replan(ensure_tree=False)
    decisions = list_decisions(actor=planner.PLANNER_ACTOR, limit=20)
    assert any("Escalated replan" in d["why"] for d in decisions)


def test_replan_emits_decision_with_diff(iso_env, monkeypatch):
    _set_posture(monkeypatch, "invest")
    goal = _daily_leads_goal(8.0)
    store.save_goal(goal)
    planner.generate_plan(goal)
    planner.evaluate_and_replan(ensure_tree=False)
    # the replan decision carries the old/new plan diff in extra
    decisions = list_decisions(actor=planner.PLANNER_ACTOR, limit=20)
    replans = [d for d in decisions if "Replanned" in d["why"]]
    assert replans
    assert "plan_diff" in replans[0].get("extra", {})
    diff = replans[0]["extra"]["plan_diff"]
    assert "old_steps" in diff and "new_steps" in diff


def test_run_planning_cycle_seeds_and_evaluates(iso_env, monkeypatch):
    _set_posture(monkeypatch, "invest")
    # No goals yet — the cycle seeds the tree, generates operational plans,
    # then evaluates. With an empty stream the daily assumptions violate and
    # (invest, normal-risk) auto-replan.
    summary = planner.run_planning_cycle()
    assert summary["ok"] is True
    assert store.list_goals()  # tree seeded
    assert store.list_plans(status=PLAN_ACTIVE)  # plans exist


def test_ensure_operational_plans_is_idempotent(iso_env, monkeypatch):
    _set_posture(monkeypatch, "invest")
    goal = _daily_leads_goal(8.0)
    store.save_goal(goal)
    planner._ensure_operational_plans()
    planner._ensure_operational_plans()  # second call must not add a duplicate
    active_plans = [p for p in store.list_plans(goal_id=goal.id) if p.status == PLAN_ACTIVE]
    assert len(active_plans) == 1


def test_current_plans_view_shape(iso_env, monkeypatch):
    _set_posture(monkeypatch, "invest")
    goal = _daily_leads_goal(8.0)
    store.save_goal(goal)
    planner.generate_plan(goal)
    view = planner.current_plans_view()
    assert view["ok"] is True
    assert view["plan_count"] >= 1
    assert view["goal_count"] >= 1
    assert isinstance(view["active_plans"], list)


def test_replan_would_breach_high_risk(iso_env, monkeypatch):
    # A goal whose intent classifies high/critical must escalate even at invest.
    goal = Goal(
        id="g-danger",
        horizon=HORIZON_DAY,
        target_metric="leads_created",
        target_value=8.0,
        label="bulk outbound messaging blast",  # matches a HIGH risk term
    )
    breach, level, why = planner._replan_would_breach(goal, "invest")
    assert breach is True
    assert level in ("high", "critical")


# --- autonomy.run_cycle Plan persistence extension -------------------------


def test_run_cycle_persists_a_plan(iso_env):
    from backend.common import autonomy

    result = autonomy.run_cycle("t-abc", "discover leads and qualify prospects")
    # existing contract preserved
    assert "observation" in result
    assert "orientation" in result
    assert "decision" in result
    assert "action" in result
    # new: a persisted planning.Plan was minted
    assert result.get("persisted_plan") is not None
    plan_id = result["persisted_plan"]["plan_id"]
    assert store.get_plan(plan_id) is not None
    assert result["persisted_plan"]["decision_id"]


def test_run_cycle_persist_plan_can_be_disabled(iso_env):
    from backend.common import autonomy

    result = autonomy.run_cycle("t-xyz", "ship the campaign", persist_plan=False)
    assert "persisted_plan" not in result
    assert store.list_plans() == []


def test_run_cycle_persist_replans_supersede(iso_env):
    from backend.common import autonomy

    r1 = autonomy.run_cycle("t-same", "discover leads")
    r2 = autonomy.run_cycle("t-same", "discover leads")
    # same task_id => same goal => second run supersedes the first's plan
    assert r2["persisted_plan"]["plan_generation"] == 2
    active = store.active_plan_for_goal("goal::mape-k::t-same")
    assert active.id == r2["persisted_plan"]["plan_id"]


# --- Concept 1 + Concept 5 wiring: planner -> consult_precedent + link_decision


def test_generate_plan_links_belief_precedents_to_decision(iso_env, monkeypatch, tmp_path):
    """Wiring test: when a belief precedent matches the plan situation, the
    resulting decision's id is linked back onto the belief's depended_by so a
    later belief flip fires the RECHECK_DECISIONS approval carrying this id.
    """
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path / "state_linked"))
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "jsonl")

    from backend.cognitive import belief_ledger as bl

    def _ev(source, weight=1.0):
        return {"source": source, "detail": "d", "weight": weight, "ts": ""}

    # Seed a belief tagged with a situation_key that the planner's context
    # ("plan leads_created posture=invest initial") will match on.
    b = bl.record_belief(
        "invest posture on leads_created plans yields best conversion",
        belief_id="plan_belief_A",
        supporting=[_ev("s1"), _ev("s2"), _ev("s3")],
        situation_key=bl.situation_key_for("plan leads_created posture=invest initial"),
    )
    assert b.depended_by == []  # baseline

    _set_posture(monkeypatch, "invest")
    goal = _daily_leads_goal(8.0)
    store.save_goal(goal)
    plan = planner.generate_plan(goal)

    # The decision landed…
    assert plan.decision_id
    # …and the belief was linked back to it (Concept 5 wiring).
    linked = bl.dependent_decisions("plan_belief_A")
    assert plan.decision_id in linked


def test_generate_plan_no_precedent_leaves_belief_ledger_untouched(iso_env, monkeypatch, tmp_path):
    """Wiring test: with no matching belief precedent, the planner minted
    decision must NOT create bogus links onto pre-existing beliefs. Isolating
    the belief_ledger store to this test's tmp_path proves the untouched
    contract without any cross-test bleed.
    """
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path / "state_untouched"))
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "jsonl")

    from backend.cognitive import belief_ledger as bl

    _set_posture(monkeypatch, "invest")
    goal = _daily_leads_goal(8.0)
    store.save_goal(goal)
    planner.generate_plan(goal)

    # No belief seeded => nothing recorded, nothing linked.
    assert bl._load() == {}  # noqa: SLF001 — asserting the untouched-store contract


def test_generate_plan_survives_cognition_import_error(iso_env, monkeypatch):
    """Wiring test: a cognition-layer import fault must not break planning.
    Simulate it by patching consult_precedent to raise.
    """

    def _boom(*_a, **_k):
        raise RuntimeError("cognition offline")

    monkeypatch.setattr("backend.cognitive.intelligence_cycle.consult_precedent", _boom)
    _set_posture(monkeypatch, "invest")
    goal = _daily_leads_goal(8.0)
    store.save_goal(goal)
    plan = planner.generate_plan(goal)
    # Plan still landed; decision still minted.
    assert plan.plan_generation == 1
    assert plan.decision_id
