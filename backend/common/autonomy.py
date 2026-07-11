"""MAPE-K autonomy engine per doc §3.5.

Observe -> Orient -> Decide -> Act. ``run_cycle`` is called by the gateway's
``/autonomy/plan`` endpoint after governance clears.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .dates import iso_now


# --- dataclasses ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Observation:
    task_id: str
    objective: str
    inputs: dict[str, Any]
    ts: str


@dataclass(frozen=True, slots=True)
class Orientation:
    signals: list[str]
    confidence: float


@dataclass(slots=True)
class PlanStep:
    name: str
    target: str
    action: str
    blocking: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Plan:
    strategy: str = "mape-k-basic"
    steps: list[PlanStep] = field(default_factory=list)


# --- per-signal keyword routing -------------------------------------------

_SIGNAL_TERMS: dict[str, tuple[str, ...]] = {
    "leadgen": ("lead", "prospect", "qualify"),
    "prospecting": ("zipcode", "discover", "call list", "callsheet"),
    "scaffold": ("proposal", "campaign", "brief", "asset"),
    "fulfillment": ("execute", "publish", "deploy", "run", "ship"),
}


# --- MAPE-K cycle ----------------------------------------------------------


def observe(task_id: str, objective: str, inputs: dict[str, Any] | None = None) -> Observation:
    return Observation(
        task_id=task_id,
        objective=objective or "",
        inputs=inputs or {},
        ts=iso_now(),
    )


def orient(objective: str) -> Orientation:
    text = (objective or "").lower()
    signals: list[str] = []
    for service, terms in _SIGNAL_TERMS.items():
        if any(term in text for term in terms):
            signals.append(service)
    if not signals:
        signals = ["fulfillment"]
    confidence = min(1.0, 0.5 + 0.2 * len(signals))
    return Orientation(signals=signals, confidence=confidence)


def decide(observation: Observation, orientation: Orientation) -> Plan:
    steps = [
        PlanStep(name=f"step_{idx + 1}", target=signal, action="execute", blocking=True)
        for idx, signal in enumerate(orientation.signals)
    ]
    return Plan(strategy="mape-k-basic", steps=steps)


def simulate(
    plan: Plan, *, decision_id: str, inputs: dict[str, Any] | None = None
) -> dict[str, Any]:
    """SIMULATE phase (HOTL T5) — dry-run every external-effect plan step.

    Sits between decide (plan) and act (execute). For each step whose ``action``
    is an external effect (send / call / payment / publish), run its dry-run
    path via :mod:`backend.common.simulation` to predict the effect + cost and
    RECORD it against ``decision_id`` — so a downstream gateway dispatch of that
    step is provably backed by a simulation. Internal-only steps are passed
    through untouched (simulating a pure compute step would be theatre).

    Returns ``{"simulated": [ {step, result}... ], "external_effect_steps": n}``.
    Never raises — a simulation fault degrades that step to an unmodelled result.
    """
    from . import simulation

    inputs = inputs or {}
    simulated: list[dict[str, Any]] = []
    n_external = 0
    for s in plan.steps:
        if not simulation.is_external_effect(s.action):
            continue
        n_external += 1
        # Pull a step-specific payload if the planner attached one; else fall
        # back to the cycle inputs (best-effort predictive material).
        payload = s.metadata.get("payload") if isinstance(s.metadata, dict) else None
        result = simulation.simulate_action(
            s.action,
            decision_id=decision_id,
            target=s.target,
            payload=payload or inputs,
        )
        simulated.append({"step": s.name, "result": result.to_record()})
    return {"simulated": simulated, "external_effect_steps": n_external}


def act(plan: Plan) -> dict[str, Any]:
    return {
        "status": "planned",
        "strategy": plan.strategy,
        "steps": [
            {
                "name": s.name,
                "target": s.target,
                "action": s.action,
                "blocking": s.blocking,
                "metadata": s.metadata,
            }
            for s in plan.steps
        ],
    }


def run_cycle(
    task_id: str,
    objective: str,
    inputs: dict[str, Any] | None = None,
    *,
    persist_plan: bool = True,
) -> dict[str, Any]:
    obs = observe(task_id, objective, inputs)
    ori = orient(objective)
    plan = decide(obs, ori)
    # SIMULATE before act — dry-run external-effect steps and record the
    # prediction against this cycle's decision_id (the task_id). Fail-soft.
    try:
        sim = simulate(plan, decision_id=task_id, inputs=inputs)
    except Exception:  # noqa: BLE001 — simulation must never break the cycle
        sim = {"simulated": [], "external_effect_steps": 0}
    action_result = act(plan)
    result = {
        "observation": asdict(obs),
        "orientation": asdict(ori),
        "decision": {
            "strategy": plan.strategy,
            "steps": [
                {
                    "name": s.name,
                    "target": s.target,
                    "action": s.action,
                    "blocking": s.blocking,
                    "metadata": s.metadata,
                }
                for s in plan.steps
            ],
        },
        "simulation": sim,
        "action": action_result,
    }
    # HOTL Tranche 4: persist the one-shot MAPE-K decision as a durable
    # planning.Plan so it is inspectable (GET /autonomy/plan), replaces the
    # ephemeral output with a first-class object, and joins the decision log.
    # ADDITIVE + fail-soft: the return contract above is unchanged, and any
    # persistence failure only augments the result with a note. Every existing
    # caller keeps working (they read observation/orientation/decision/action).
    if persist_plan:
        persisted = _persist_mape_k_plan(task_id, objective, plan)
        if persisted:
            result["persisted_plan"] = persisted
    return result


def _persist_mape_k_plan(
    task_id: str,
    objective: str,
    plan: "Plan",
) -> dict[str, Any] | None:
    """Persist a MAPE-K decision as a planning.Plan + mint a DecisionRecord.

    Best-effort; never raises. Returns a thin {plan_id, decision_id} dict on
    success so the /autonomy/plan POST caller can surface it, else None.
    """
    try:
        import uuid as _uuid

        from backend.planning import store as _pstore
        from backend.planning.models import (
            PLAN_ACTIVE,
            Assumption as _Assumption,
            Goal as _Goal,
            Plan as _PlanModel,
            PlanStep as _PlanStep,
        )

        # A lightweight ad-hoc goal captures the MAPE-K objective so the plan
        # has a parent to hang off (deterministic id -> idempotent per objective
        # per task, so re-running the cycle refreshes rather than piling up).
        goal_id = f"goal::mape-k::{task_id or 'adhoc'}"
        goal = _pstore.get_goal(goal_id) or _Goal(
            id=goal_id,
            horizon="day",
            target_metric="mape_k_objective",
            target_value=0.0,
            label=(objective or "")[:120],
            metadata={"source": "mape-k", "task_id": task_id},
        )
        _pstore.save_goal(goal)

        prior = _pstore.active_plan_for_goal(goal_id)
        generation = (prior.plan_generation + 1) if prior else 1
        steps = [
            _PlanStep(
                name=s.name,
                channel=s.target,
                action=s.action,
                rationale=f"MAPE-K routed to {s.target}",
                metadata=dict(s.metadata),
            )
            for s in plan.steps
        ]
        new_plan = _PlanModel(
            id=_uuid.uuid4().hex,
            goal_id=goal_id,
            plan_generation=generation,
            status=PLAN_ACTIVE,
            strategy=plan.strategy,
            assumptions=[
                _Assumption(
                    id=_uuid.uuid4().hex,
                    description="MAPE-K keyword-routed plan (advisory)",
                    metric="",
                    op=">=",
                    threshold=0.0,
                    window_days=1,
                )
            ],
            steps=steps,
            rationale=f"MAPE-K cycle for objective: {(objective or '')[:120]}",
        )
        # Mint a decision record for the MAPE-K decision.
        # Concept 1 + Concept 5: consult precedent for the objective + link
        # the surfaced belief precedents to the resulting decision so a later
        # belief flip cascades a RECHECK_DECISIONS approval carrying this id.
        try:
            from backend.common.decision_record import record_decision

            precedent_ctx = f"mape-k objective {(objective or '')[:120]}"
            precedent = {
                "mode": "proceed_novel",
                "beliefs": [],
                "decisions": [],
                "leading_belief": None,
            }
            try:
                from backend.cognitive.intelligence_cycle import consult_precedent

                precedent = consult_precedent(precedent_ctx)
            except Exception:  # noqa: BLE001 — cognition is optional
                pass

            extra: dict[str, Any] = {"plan_id": new_plan.id, "steps": [s.name for s in steps]}
            if precedent.get("mode") == "short_circuit":
                extra["precedent_mode"] = "short_circuit"
                lb = precedent.get("leading_belief")
                if lb is not None:
                    extra["precedent_belief_id"] = getattr(lb, "belief_id", "")
            elif precedent.get("beliefs") or precedent.get("decisions"):
                extra["precedent_mode"] = "proceed_novel"

            decision = record_decision(
                "autonomy_mape_k",
                f"MAPE-K plan for '{(objective or '')[:80]}' ({len(steps)} steps)",
                workcell="gateway",
                data_used=[f"task_id={task_id}", f"strategy={plan.strategy}"],
                expected_outcome="Route the objective through the signalled workcells",
                confidence=0.5,
                extra=extra,
            )
            new_plan.decision_id = decision.decision_id

            beliefs = precedent.get("beliefs") or []
            if decision.decision_id and beliefs:
                try:
                    from backend.cognitive.belief_ledger import link_decision

                    for m in beliefs:
                        bid = getattr(m, "belief_id", "")
                        if bid:
                            link_decision(bid, decision.decision_id)
                except Exception:  # noqa: BLE001 — link is best-effort
                    pass
        except Exception:  # noqa: BLE001 — decision record is best-effort
            pass

        if prior is not None:
            try:
                from backend.planning.models import PLAN_SUPERSEDED

                prior.status = PLAN_SUPERSEDED
                _pstore.save_plan(prior)
            except Exception:  # noqa: BLE001
                pass
        _pstore.save_plan(new_plan)
        return {
            "plan_id": new_plan.id,
            "decision_id": new_plan.decision_id,
            "goal_id": goal_id,
            "plan_generation": generation,
        }
    except Exception:  # noqa: BLE001 — planning persistence must not break MAPE-K
        return None
