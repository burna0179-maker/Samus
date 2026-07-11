"""Plan generation, assumption checking, and automatic replanning.

HOTL Tranche 4 (framework Phases 4-5). Three jobs:

1. GENERATE (:func:`generate_plan`) — from a goal + the current funnel
   economics + budget posture, mint a :class:`~backend.planning.models.Plan`
   whose steps are the channel actions that close the gap and whose
   ``assumptions`` are checkable predicates against the unified event stream
   (e.g. ">= N leads/day", "connect_rate >= 0.12"). Each generation mints a
   :class:`~backend.common.decision_record.DecisionRecord`.

2. CHECK (:func:`evaluate_assumptions`) — evaluate every active plan's
   assumptions against ``read_events``; return the violated ones.

3. REPLAN (:func:`evaluate_and_replan`) — the control-tick hook. For each
   active plan with a violated assumption: regenerate the plan (Plan B), mark
   the old generation superseded, emit ``decision.made`` carrying the old/new
   diff. Escalate to the operator (an approval request) ONLY when replanning
   would breach budget posture (conserve) or risk tier (governance
   high/critical).

Fail-soft throughout: a planning fault never raises to the control tick.
"""

from __future__ import annotations

import datetime as _dt
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from backend.common.dates import iso_now

from . import store
from .goal_tree import ensure_goal_tree, read_funnel_economics
from .models import (
    HORIZON_DAY,
    PLAN_ACTIVE,
    PLAN_SUPERSEDED,
    Assumption,
    Goal,
    Plan,
    PlanStep,
)

_LOG = logging.getLogger("samus.planning.planner")

PLANNER_ACTOR = "planner"

# Which goals drive a plan. Every horizon can hold a plan, but the daily
# lead/task goals are the ones whose assumptions are cheaply checkable against
# the event stream on the control-tick cadence — those get the operational
# plans that replanning watches. Higher horizons get a thin "roll-up" plan.
_OPERATIONAL_HORIZONS = frozenset({HORIZON_DAY})


# ---------------------------------------------------------------------------
# Assumption metrics — checkable predicates against the event stream
# ---------------------------------------------------------------------------
# Each metric is a callable (window_days) -> float, computed from read_events.
# Kept small + guarded: a metric that can't be computed returns 0.0, which for
# a ">=" assumption reads as "violated" — a conservative default that triggers
# a replan rather than silently masking a stalled funnel.


def _count_events(event_type: str, window_days: int) -> int:
    from backend.common.business_events import read_events

    since = _since_iso(window_days)
    return len(read_events(since=since, event_types=[event_type], limit=100_000))


def _since_iso(window_days: int) -> str:
    days = max(0, int(window_days or 0))
    start = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=max(1, days))
    return start.strftime("%Y-%m-%dT%H:%M:%SZ")


def _leads_per_day(window_days: int) -> float:
    from backend.common.business_events import LEAD_CREATED

    n = _count_events(LEAD_CREATED, window_days)
    return n / max(1, int(window_days or 1))


def _tasks_per_day(window_days: int) -> float:
    # A "task" here is an outreach touch: an email sent or a call placed.
    from backend.common.business_events import CALL_PLACED, EMAIL_SENT

    n = _count_events(EMAIL_SENT, window_days) + _count_events(CALL_PLACED, window_days)
    return n / max(1, int(window_days or 1))


def _connect_rate(window_days: int) -> float:
    from backend.common.business_events import CALL_ANSWERED, CALL_PLACED

    placed = _count_events(CALL_PLACED, window_days)
    answered = _count_events(CALL_ANSWERED, window_days)
    if placed <= 0:
        return 0.0
    return answered / placed


_ASSUMPTION_METRICS: dict[str, Callable[[int], float]] = {
    "leads_per_day": _leads_per_day,
    "tasks_per_day": _tasks_per_day,
    "connect_rate": _connect_rate,
}


def compute_metric(metric: str, window_days: int) -> float:
    """Evaluate one named metric over ``window_days``. 0.0 when unknown/failed."""
    fn = _ASSUMPTION_METRICS.get(metric)
    if fn is None:
        return 0.0
    try:
        return float(fn(window_days))
    except Exception as exc:  # noqa: BLE001 — a metric fault never blocks planning
        _LOG.debug("planner: metric %s failed: %s", metric, exc)
        return 0.0


def _op_holds(value: float, op: str, threshold: float) -> bool:
    if op == ">=":
        return value >= threshold
    if op == ">":
        return value > threshold
    if op == "<=":
        return value <= threshold
    if op == "<":
        return value < threshold
    if op in ("==", "="):
        return value == threshold
    # Unknown operator -> treat as satisfied (never spuriously replan).
    return True


def check_assumption(assumption: Assumption) -> tuple[bool, float]:
    """Return (holds, observed_value) for one assumption against the stream."""
    observed = compute_metric(assumption.metric, assumption.window_days)
    return _op_holds(observed, assumption.op, assumption.threshold), observed


# ---------------------------------------------------------------------------
# Plan generation
# ---------------------------------------------------------------------------


def _assumptions_for_goal(goal: Goal, econ_leads_per_day: float) -> list[Assumption]:
    """Derive the checkable predicates a daily goal's plan depends on."""
    if goal.target_metric == "leads_created":
        need = max(1.0, float(goal.target_value))
        return [
            Assumption(
                id=uuid.uuid4().hex,
                description=f">= {need:.0f} leads/day (7d avg)",
                metric="leads_per_day",
                op=">=",
                threshold=round(need, 2),
                window_days=7,
            ),
            Assumption(
                id=uuid.uuid4().hex,
                description="call connect rate >= 12% (7d)",
                metric="connect_rate",
                op=">=",
                threshold=0.12,
                window_days=7,
            ),
        ]
    if goal.target_metric == "tasks_completed":
        need = max(1.0, float(goal.target_value))
        return [
            Assumption(
                id=uuid.uuid4().hex,
                description=f">= {need:.0f} outreach tasks/day (7d avg)",
                metric="tasks_per_day",
                op=">=",
                threshold=round(need, 2),
                window_days=7,
            ),
        ]
    # Revenue-horizon goals carry a thin lead-flow assumption (leads are the
    # upstream driver of revenue) so even roll-up plans are checkable.
    return [
        Assumption(
            id=uuid.uuid4().hex,
            description=f">= {econ_leads_per_day:.1f} leads/day sustains the run-rate",
            metric="leads_per_day",
            op=">=",
            threshold=round(max(1.0, econ_leads_per_day), 2),
            window_days=7,
        ),
    ]


def _steps_for_goal(goal: Goal, posture: str) -> list[PlanStep]:
    """Channel actions that pursue the goal, gated by budget posture.

    conserve -> free channels only (call opportunities, no paid spend);
    lean     -> free + email (low-cost);
    invest   -> everything incl. paid/voice volume.
    """
    steps: list[PlanStep] = []
    target = float(goal.target_value or 0.0)

    # Prospecting always runs — creating leads is the upstream of everything and
    # is a fixed-cost daily job, not discretionary marketing spend.
    if goal.target_metric == "leads_created":
        steps.append(
            PlanStep(
                name="run_discovery",
                channel="prospecting",
                action="discover_leads",
                target_value=target,
                rationale=f"Create {target:.0f} qualified leads/day to feed the funnel",
            )
        )

    # Email is low-cost — allowed at lean and invest.
    if posture in ("lean", "invest"):
        steps.append(
            PlanStep(
                name="email_outreach",
                channel="email",
                action="send_outreach",
                target_value=target,
                rationale="Low-cost personalised outreach to warm/hot leads",
            )
        )

    # Calls carry Vapi cost — full volume only at invest; a conserve/lean posture
    # still generates operator call OPPORTUNITIES (free) but does not commit paid
    # autonomous dialing volume.
    if posture == "invest":
        steps.append(
            PlanStep(
                name="voice_outreach",
                channel="call",
                action="place_calls",
                target_value=target,
                rationale="Paid dialing volume — affordable this tick (invest posture)",
            )
        )
    else:
        steps.append(
            PlanStep(
                name="operator_call_queue",
                channel="call",
                action="queue_operator_calls",
                target_value=target,
                rationale="Free: surface priority call opportunities for the operator",
            )
        )

    if goal.target_metric == "tasks_completed":
        steps.append(
            PlanStep(
                name="followup_touches",
                channel="retention",
                action="followups_due",
                target_value=target,
                rationale="Work the follow-up queue to complete daily outreach tasks",
            )
        )
    return steps


def generate_plan(
    goal: Goal,
    *,
    posture: str | None = None,
    prior_plan: Plan | None = None,
    replan_reason: str = "",
) -> Plan:
    """Mint + persist a plan toward ``goal`` and record the decision.

    ``plan_generation`` = prior generation + 1 (starts at 1). When a
    ``prior_plan`` is supplied it is marked superseded. Mints a DecisionRecord
    (carrying the old/new step diff when replanning) and stamps its id onto the
    plan. Never raises.
    """
    econ = read_funnel_economics()
    if posture is None:
        posture = _current_posture()

    # Estimate the run-rate lead requirement so revenue-horizon assumptions have
    # a sensible threshold (reuse the goal-tree funnel arithmetic).
    econ_leads_per_day = 1.0
    try:
        # weekly leads / 7, using the goal's own target when it is a lead goal.
        if goal.target_metric == "leads_created":
            econ_leads_per_day = max(1.0, float(goal.target_value))
        else:
            econ_leads_per_day = max(
                1.0,
                econ.leads_for_revenue(float(goal.target_value)) / 7.0,
            )
    except Exception:  # noqa: BLE001
        econ_leads_per_day = 1.0

    generation = (prior_plan.plan_generation + 1) if prior_plan else 1
    assumptions = _assumptions_for_goal(goal, econ_leads_per_day)
    steps = _steps_for_goal(goal, posture)

    rationale = (
        replan_reason
        or f"Initial plan for {goal.horizon} goal '{goal.target_metric}' "
        f"(target {goal.target_value:g}); posture={posture}"
    )

    plan = Plan(
        id=uuid.uuid4().hex,
        goal_id=goal.id,
        plan_generation=generation,
        status=PLAN_ACTIVE,
        strategy="revenue-decomposition",
        assumptions=assumptions,
        steps=steps,
        rationale=rationale,
        created_at=iso_now(),
        metadata={
            "posture": posture,
            "avg_deal_usd": econ.avg_deal_usd,
            "close_rate": econ.close_rate,
            "economics_source": econ.source,
        },
    )

    # Mint the decision record (with a diff when this is a replan).
    decision = _record_plan_decision(goal, plan, prior_plan, posture, replan_reason)
    plan.decision_id = decision.decision_id

    # Supersede the prior generation, then persist the new plan.
    if prior_plan is not None:
        try:
            prior_plan.status = PLAN_SUPERSEDED
            store.save_plan(prior_plan)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("generate_plan: superseding prior failed: %s", exc)

    try:
        store.save_plan(plan)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("generate_plan: persist failed: %s", exc)
    return plan


def _plan_step_summary(plan: Plan) -> list[dict[str, Any]]:
    return [
        {"name": s.name, "channel": s.channel, "action": s.action, "target_value": s.target_value}
        for s in plan.steps
    ]


def _record_plan_decision(
    goal: Goal,
    plan: Plan,
    prior_plan: Plan | None,
    posture: str,
    replan_reason: str,
) -> Any:
    """Mint a DecisionRecord for a plan generation / replan.

    Concept 1 + Concept 5 wiring: consult precedent for this goal/replan
    situation BEFORE minting the decision; when a leading belief precedent
    surfaces, link the resulting decision back to it via
    :func:`backend.cognitive.belief_ledger.link_decision` so ``depended_by``
    populates and a later belief flip auto-queues a RECHECK_DECISIONS
    approval carrying this decision id.
    """
    from backend.common.decision_record import record_decision

    is_replan = prior_plan is not None
    why = (
        f"Replanned '{goal.target_metric}' goal (gen {plan.plan_generation}): {replan_reason}"
        if is_replan
        else f"Generated plan for '{goal.target_metric}' goal (target {goal.target_value:g})"
    )
    alternatives: list[Any] = []
    if posture != "invest":
        alternatives.append(
            f"Full paid dialing volume — held back (posture={posture}, cash-constrained)"
        )
    data_used = [
        f"goal={goal.id}",
        f"posture={posture}",
        f"avg_deal_usd={plan.metadata.get('avg_deal_usd')}",
        f"close_rate={plan.metadata.get('close_rate')}",
    ]
    extra: dict[str, Any] = {
        "goal_id": goal.id,
        "plan_id": plan.id,
        "plan_generation": plan.plan_generation,
        "posture": posture,
        "new_steps": _plan_step_summary(plan),
    }
    if is_replan:
        extra["plan_diff"] = {
            "old_generation": prior_plan.plan_generation,
            "old_plan_id": prior_plan.id,
            "old_steps": _plan_step_summary(prior_plan),
            "new_steps": _plan_step_summary(plan),
        }

    # Concept 1 — consult precedent for this planning situation. The context
    # string is human-readable so belief.query_precedent's keyword scoring +
    # codex.search_decisions text search both bind on the goal + posture.
    precedent = _consult_precedent_safe(
        f"plan {goal.target_metric} posture={posture}"
        + (f" replan {replan_reason}" if is_replan else " initial")
    )
    if precedent.get("mode") == "short_circuit":
        # Record precedent visibility on the decision even though the planner
        # never short-circuits mid-flight (a plan MUST persist to survive the
        # replan cadence). The precedent tag becomes a queryable signal on
        # decision.made stream that "prior belief guided this planning turn".
        extra["precedent_mode"] = "short_circuit"
        lb = precedent.get("leading_belief")
        if lb is not None:
            extra["precedent_belief_id"] = getattr(lb, "belief_id", "")
    elif precedent.get("beliefs") or precedent.get("decisions"):
        extra["precedent_mode"] = "proceed_novel"

    decision = record_decision(
        PLANNER_ACTOR,
        why,
        workcell="planning",
        alternatives_considered=alternatives,
        data_used=data_used,
        expected_outcome=(f"Sustain the run-rate toward {goal.label or goal.target_metric}"),
        confidence=0.6 if is_replan else 0.7,
        risk_level="normal",
        ev_usd=float(goal.metadata.get("target_value") or 0.0),
        extra=extra,
    )

    # Concept 5 — link the decision to every belief precedent that surfaced,
    # so belief_ledger.depended_by populates and a later belief flip enqueues
    # the RECHECK_DECISIONS approval carrying this decision_id.
    _link_decision_to_beliefs(decision.decision_id, precedent.get("beliefs"))
    return decision


def _consult_precedent_safe(context: str) -> dict[str, Any]:
    """Wrap :func:`intelligence_cycle.consult_precedent` so a cognitive-layer
    import fault never breaks planning. Returns the empty proceed-novel record
    on any failure.
    """
    try:
        from backend.cognitive.intelligence_cycle import consult_precedent

        return consult_precedent(context)
    except Exception as exc:  # noqa: BLE001 — planner must not depend on cognition
        _LOG.debug("planner: consult_precedent unavailable: %s", exc)
        return {
            "mode": "proceed_novel",
            "beliefs": [],
            "decisions": [],
            "leading_belief": None,
            "rationale": "cognition-unavailable",
        }


def _link_decision_to_beliefs(decision_id: str, beliefs: Any) -> None:
    """Link ``decision_id`` back to each belief precedent that surfaced.

    Idempotent + fail-soft: unknown beliefs are dropped by
    :func:`belief_ledger.link_decision`; import faults are swallowed.
    """
    if not decision_id or not beliefs:
        return
    try:
        from backend.cognitive.belief_ledger import link_decision

        for m in beliefs:
            bid = getattr(m, "belief_id", "")
            if bid:
                link_decision(bid, decision_id)
    except Exception as exc:  # noqa: BLE001 — link is best-effort
        _LOG.debug("planner: link_decision skipped: %s", exc)


# ---------------------------------------------------------------------------
# Assumption evaluation + replanning
# ---------------------------------------------------------------------------


@dataclass
class ViolatedAssumption:
    plan: Plan
    goal: Goal
    assumption: Assumption
    observed: float


def evaluate_assumptions(
    plans: list[Plan] | None = None,
) -> list[ViolatedAssumption]:
    """Evaluate every active plan's assumptions; return the violated ones.

    Only OPERATIONAL-horizon plans (daily) are checked each tick — their
    metrics are the cheaply-computable event-stream counts. Never raises.
    """
    violations: list[ViolatedAssumption] = []
    try:
        active = plans if plans is not None else store.list_plans(status=PLAN_ACTIVE)
        for plan in active:
            goal = store.get_goal(plan.goal_id)
            if goal is None:
                continue
            if goal.horizon not in _OPERATIONAL_HORIZONS:
                continue
            for assumption in plan.assumptions:
                holds, observed = check_assumption(assumption)
                if not holds:
                    violations.append(ViolatedAssumption(plan, goal, assumption, observed))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("evaluate_assumptions failed: %s", exc)
    return violations


def _current_posture() -> str:
    try:
        from backend.cash_engine.affordability import assess_affordability

        return assess_affordability().posture
    except Exception as exc:  # noqa: BLE001 — degrade to lean (cautious)
        _LOG.debug("planner: posture read degraded: %s", exc)
        return "lean"


def _replan_would_breach(goal: Goal, posture: str) -> tuple[bool, str, str]:
    """Would replanning this goal breach budget posture or risk tier?

    Returns (breach, risk_level, why). Breach conditions (per the plan):
      * budget posture == "conserve" (at/below the cash reserve) — a replan
        that would push MORE spend needs operator sign-off; OR
      * governance.classify_risk of the goal's intent is high/critical.
    """
    from backend.common.governance import classify_risk

    objective = f"replan {goal.target_metric} {goal.label}".strip()
    actions = [s.action for s in _steps_for_goal(goal, posture)]
    level, reasons = classify_risk(objective, actions)
    if level in ("high", "critical"):
        return True, level, f"risk tier {level}: {'; '.join(reasons)}"
    if posture == "conserve":
        return (
            True,
            "normal",
            (
                "budget posture is CONSERVE (at/below cash reserve) — a replan that "
                "increases spend needs operator approval"
            ),
        )
    return False, level, ""


def evaluate_and_replan(
    *,
    ensure_tree: bool = True,
) -> dict[str, Any]:
    """The control-tick hook: check assumptions, replan violated plans.

    For each active daily plan whose assumption is violated:
      * if replanning would breach budget posture / risk tier -> create an
        approval request (operator gate) and DO NOT auto-swap the plan;
      * otherwise regenerate the plan (Plan B), supersede the old generation,
        and emit the decision + diff.

    Returns a structured summary (safe to log / surface). Never raises.
    """
    summary: dict[str, Any] = {
        "ok": True,
        "checked_plans": 0,
        "violations": 0,
        "replanned": [],
        "escalated": [],
        "ts": iso_now(),
    }
    try:
        if ensure_tree:
            ensure_goal_tree()
            _ensure_operational_plans()

        active = store.list_plans(status=PLAN_ACTIVE)
        summary["checked_plans"] = len(
            [
                p
                for p in active
                if (
                    store.get_goal(p.goal_id)
                    or Goal(
                        id="",
                        horizon="",
                        target_metric="",
                        target_value=0.0,
                    )
                ).horizon
                in _OPERATIONAL_HORIZONS
            ]
        )
        violations = evaluate_assumptions(active)
        summary["violations"] = len(violations)

        posture = _current_posture()
        # De-dup: one replan per plan even if multiple assumptions failed.
        handled: set[str] = set()
        for v in violations:
            if v.plan.id in handled:
                continue
            handled.add(v.plan.id)
            reason = (
                f"assumption '{v.assumption.description}' violated "
                f"(observed {v.observed:g} vs {v.assumption.op} "
                f"{v.assumption.threshold:g})"
            )
            breach, risk_level, why = _replan_would_breach(v.goal, posture)
            if breach:
                approval = _escalate_replan(v, posture, reason, risk_level, why)
                summary["escalated"].append(
                    {
                        "plan_id": v.plan.id,
                        "goal_id": v.goal.id,
                        "reason": reason,
                        "risk_level": risk_level,
                        "why": why,
                        "approval_id": approval.get("id") if approval else None,
                    }
                )
                continue
            # Anti-churn guard: a violated assumption whose regenerated plan
            # would be IDENTICAL to the current one (same posture -> same steps)
            # is not a meaningful Plan B — the plan is already right, execution
            # is just behind. Auto-swap only when the plan materially changes
            # OR this is the FIRST response to a violation (gen 1 -> gen 2), so
            # the operator sees at least one recorded Plan B without a 30-min
            # generation treadmill forever after on a cold stream.
            candidate_steps = _plan_step_summary(
                Plan(id="", goal_id=v.goal.id, steps=_steps_for_goal(v.goal, posture))
            )
            if v.plan.plan_generation >= 2 and candidate_steps == _plan_step_summary(v.plan):
                summary.setdefault("held", []).append(
                    {
                        "plan_id": v.plan.id,
                        "goal_id": v.goal.id,
                        "reason": reason,
                        "why": "plan unchanged under current posture; keep executing",
                    }
                )
                continue
            new_plan = generate_plan(
                v.goal,
                posture=posture,
                prior_plan=v.plan,
                replan_reason=reason,
            )
            summary["replanned"].append(
                {
                    "goal_id": v.goal.id,
                    "old_plan_id": v.plan.id,
                    "new_plan_id": new_plan.id,
                    "new_generation": new_plan.plan_generation,
                    "reason": reason,
                }
            )
    except Exception as exc:  # noqa: BLE001 — the tick must never die here
        _LOG.warning("evaluate_and_replan failed: %s", exc)
        summary["ok"] = False
        summary["error"] = str(exc)
    return summary


def _escalate_replan(
    violation: ViolatedAssumption,
    posture: str,
    reason: str,
    risk_level: str,
    why: str,
) -> dict[str, Any] | None:
    """Create an operator approval for a replan that breaches posture/risk.

    Also mints a DecisionRecord recording the escalation (so the decision log
    shows WHY the plan was not auto-swapped). Never raises.
    """
    try:
        from backend.common.approvals import create_approval
        from backend.common.decision_record import record_decision

        proposed_steps = _plan_step_summary(
            # A dry proposed Plan B (not persisted) so the operator sees what
            # would change — build steps under the current posture.
            Plan(
                id="",
                goal_id=violation.goal.id,
                steps=_steps_for_goal(violation.goal, posture),
            )
        )
        approval = create_approval(
            "replan",
            {
                "goal_id": violation.goal.id,
                "plan_id": violation.plan.id,
                "goal_label": violation.goal.label,
                "reason": reason,
                "why_escalated": why,
                "posture": posture,
                "proposed_steps": proposed_steps,
                "violated_assumption": violation.assumption.to_dict(),
                "observed": violation.observed,
            },
            risk_level=risk_level,
        )
        # Concept 1 — consult precedent for this escalation situation.
        precedent = _consult_precedent_safe(
            f"escalate replan {violation.goal.target_metric} risk={risk_level}"
        )
        extra: dict[str, Any] = {
            "goal_id": violation.goal.id,
            "plan_id": violation.plan.id,
            "approval_id": approval.get("id"),
            "escalation": True,
        }
        if precedent.get("mode") == "short_circuit":
            extra["precedent_mode"] = "short_circuit"
            lb = precedent.get("leading_belief")
            if lb is not None:
                extra["precedent_belief_id"] = getattr(lb, "belief_id", "")
        elif precedent.get("beliefs") or precedent.get("decisions"):
            extra["precedent_mode"] = "proceed_novel"

        decision = record_decision(
            PLANNER_ACTOR,
            f"Escalated replan of '{violation.goal.target_metric}' to operator: {why}",
            workcell="planning",
            alternatives_considered=[
                "Auto-swap Plan B — declined; breaches budget posture / risk tier",
            ],
            data_used=[reason, f"posture={posture}", f"risk_level={risk_level}"],
            expected_outcome="Operator approves/denies the replan before it takes effect",
            confidence=0.5,
            risk_level=risk_level,
            extra=extra,
        )
        # Concept 5 — link the escalation decision to its belief precedents.
        _link_decision_to_beliefs(decision.decision_id, precedent.get("beliefs"))
        return approval
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("_escalate_replan failed: %s", exc)
        return None


def _ensure_operational_plans() -> None:
    """Ensure each active operational (daily) goal has an active plan.

    Idempotent: a goal that already has an active plan is left alone; a goal
    with none gets an initial (generation 1) plan. Cheap on the steady state
    (one list_plans read per goal). Never raises.
    """
    try:
        from .models import GOAL_ACTIVE

        posture = _current_posture()
        for goal in store.list_goals(status=GOAL_ACTIVE):
            if goal.horizon not in _OPERATIONAL_HORIZONS:
                continue
            if store.active_plan_for_goal(goal.id) is None:
                generate_plan(goal, posture=posture)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("_ensure_operational_plans failed: %s", exc)


def run_planning_cycle() -> dict[str, Any]:
    """One full planning pass: ensure tree + plans, then evaluate + replan.

    The single entry point the in-container planner timer calls each tick.
    """
    return evaluate_and_replan(ensure_tree=True)


# ---------------------------------------------------------------------------
# Inspection helpers (for GET /autonomy/plan + command center)
# ---------------------------------------------------------------------------


def current_plans_view() -> dict[str, Any]:
    """A read-only snapshot of the goal tree + active plans (never raises)."""
    try:
        from .models import GOAL_ACTIVE

        goals = store.list_goals(status=GOAL_ACTIVE)
        plans = store.list_plans(status=PLAN_ACTIVE)
        return {
            "ok": True,
            "goals": [g.to_dict() for g in goals],
            "active_plans": [p.to_dict() for p in plans],
            "goal_count": len(goals),
            "plan_count": len(plans),
        }
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("current_plans_view failed: %s", exc)
        return {"ok": False, "error": str(exc), "goals": [], "active_plans": []}


__all__ = [
    "PLANNER_ACTOR",
    "compute_metric",
    "check_assumption",
    "generate_plan",
    "evaluate_assumptions",
    "evaluate_and_replan",
    "run_planning_cycle",
    "current_plans_view",
    "ViolatedAssumption",
]
