#!/usr/bin/env python3
"""
FulfillmentWorker v2 — DAG-based meta-orchestrator
Source: ChatGPT recovery chat 02 (fulfillment upgrade section)

Canonical relationship:
- [NEW pack] business/fulfillment — domain-specific extension to canonical agents plane (§6)
- [EXPANDS §6 orchestration] DAG dispatch + dependency-resolution + step idempotency
- [EXPANDS §6 application] CRM artifact write + domain-event projection
- [DEFERRED] partial failure handling, circuit-breaker-per-step-type, SLA enforcement
- Replaces prior-iteration thin stub (see memory: project_samus_fulfillment_design)

Action surface expanded from 1 → 5:
  plan_execution | execute_step | resume_plan | validate_plan | finalize_plan
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("samus.fulfillment.worker.v2")


# -------------------------------------------------------------------
# Plan + Step dataclasses
# -------------------------------------------------------------------
@dataclass
class PlanStep:
    id: str
    type: str                                # "<service>.<action>"
    depends_on: List[str] = field(default_factory=list)
    payload: Dict[str, Any] = field(default_factory=dict)
    retryable: bool = True
    timeout_sec: Optional[int] = None
    status: str = "pending"                  # pending|running|done|failed|skipped


@dataclass
class FulfillmentPlan:
    plan_id: str
    task_id: str
    steps: List[PlanStep]
    risk: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    status: str = "planned"                  # planned|running|complete|failed


# -------------------------------------------------------------------
# Action enum (string-stable for SQS envelopes)
# -------------------------------------------------------------------
ACTIONS = {
    "PLAN_EXECUTION": "plan_execution",
    "EXECUTE_STEP": "execute_step",
    "RESUME_PLAN": "resume_plan",
    "VALIDATE_PLAN": "validate_plan",
    "FINALIZE_PLAN": "finalize_plan",
}


# -------------------------------------------------------------------
# Worker
# -------------------------------------------------------------------
class FulfillmentWorkerV2:
    """
    Skeleton — actual base class is `BaseSqsWorker` in target env.
    Provided here as a free-standing class for cross-version review.
    """

    def __init__(
        self,
        dispatch_fn: Callable[..., None],
        crm: Any,
        plan_store: Any,
        metrics: Optional[Any] = None,
    ):
        self.dispatch = dispatch_fn
        self.crm = crm
        self.plan_store = plan_store
        self.metrics = metrics

    # -----  router  -----
    def handle(self, envelope) -> Dict[str, Any]:
        action_map = {
            ACTIONS["PLAN_EXECUTION"]: self._plan,
            ACTIONS["EXECUTE_STEP"]:   self._execute_step,
            ACTIONS["RESUME_PLAN"]:    self._resume,
            ACTIONS["VALIDATE_PLAN"]:  self._validate,
            ACTIONS["FINALIZE_PLAN"]:  self._finalize,
        }
        if envelope.action not in action_map:
            raise ValueError(f"Unsupported fulfillment action: {envelope.action}")
        return action_map[envelope.action](envelope)

    # -----  phase: plan  -----
    def _plan(self, envelope) -> Dict[str, Any]:
        from backend.fulfillment.logic import plan_fulfillment  # type: ignore
        result = plan_fulfillment(envelope.task_id, envelope.payload, envelope.metadata)
        plan = self._coerce_plan(envelope.task_id, result)
        self._store_plan(envelope, plan)
        self._emit_event("plan_created", envelope, plan_id=plan.plan_id)

        for step in plan.steps:
            if not step.depends_on:
                self._dispatch_step(step, envelope, plan)
        return {"status": "planned", "plan_id": plan.plan_id, "steps_dispatched": sum(1 for s in plan.steps if not s.depends_on)}

    # -----  phase: execute_step  -----
    def _execute_step(self, envelope) -> Dict[str, Any]:
        step = PlanStep(**envelope.payload["step"])
        plan = self._load_plan(envelope.payload["plan_id"])
        try:
            self._mark_step(plan, step.id, "running")
            # actual logic runs in the target worker (envelope.action="<service>.<action>")
            self._mark_step(plan, step.id, "done")
            self._unlock_dependents(plan, step, envelope)
            self._emit_event("step_completed", envelope, plan_id=plan.plan_id, step_id=step.id)
            return {"status": "step_completed"}
        except Exception:
            self._mark_step(plan, step.id, "failed")
            self._emit_event("step_failed", envelope, plan_id=plan.plan_id, step_id=step.id)
            if step.retryable:
                raise
            return {"status": "step_skipped"}

    def _resume(self, envelope) -> Dict[str, Any]:
        plan = self._load_plan(envelope.payload["plan_id"])
        ready = [s for s in plan.steps if s.status == "pending" and self._deps_met(plan, s)]
        for s in ready:
            self._dispatch_step(s, envelope, plan)
        return {"status": "resumed", "ready": [s.id for s in ready]}

    def _validate(self, envelope) -> Dict[str, Any]:
        plan = self._load_plan(envelope.payload["plan_id"])
        cycles = self._detect_cycles(plan)
        return {"status": "validated", "cycles": cycles, "step_count": len(plan.steps)}

    def _finalize(self, envelope) -> Dict[str, Any]:
        plan = self._load_plan(envelope.payload["plan_id"])
        plan.status = "complete" if all(s.status == "done" for s in plan.steps) else "failed"
        self.plan_store.put(plan)
        self._emit_event("plan_finalized", envelope, plan_id=plan.plan_id, status=plan.status)
        return {"status": plan.status}

    # -----  helpers  -----
    def _dispatch_step(self, step: PlanStep, envelope, plan: FulfillmentPlan) -> None:
        service, action = step.type.split(".", 1)
        idem_key = f"{plan.plan_id}:{step.id}"
        self.dispatch(
            service=service,
            action=action,
            payload={**step.payload, "step": step.__dict__, "plan_id": plan.plan_id, "parent_task_id": envelope.task_id},
            metadata={**envelope.metadata, "idempotency_key": idem_key},
        )

    def _unlock_dependents(self, plan: FulfillmentPlan, completed: PlanStep, envelope) -> None:
        for step in plan.steps:
            if completed.id in step.depends_on and self._deps_met(plan, step):
                self._dispatch_step(step, envelope, plan)

    def _deps_met(self, plan: FulfillmentPlan, step: PlanStep) -> bool:
        statuses = {s.id: s.status for s in plan.steps}
        return all(statuses.get(d) == "done" for d in step.depends_on)

    def _detect_cycles(self, plan: FulfillmentPlan) -> List[str]:
        # Kahn's algorithm tail
        in_deg = {s.id: len(s.depends_on) for s in plan.steps}
        queue = [sid for sid, d in in_deg.items() if d == 0]
        visited = 0
        while queue:
            sid = queue.pop()
            visited += 1
            for s in plan.steps:
                if sid in s.depends_on:
                    in_deg[s.id] -= 1
                    if in_deg[s.id] == 0:
                        queue.append(s.id)
        return [sid for sid, d in in_deg.items() if d > 0] if visited != len(plan.steps) else []

    def _store_plan(self, envelope, plan: FulfillmentPlan) -> None:
        self.plan_store.put(plan)
        self.crm.create_artifact({"type": "FULFILLMENT_PLAN", "task_id": envelope.task_id, "data": plan.__dict__})

    def _load_plan(self, plan_id: str) -> FulfillmentPlan:
        return self.plan_store.get(plan_id)

    def _mark_step(self, plan: FulfillmentPlan, step_id: str, status: str) -> None:
        for s in plan.steps:
            if s.id == step_id:
                s.status = status
        self.plan_store.put(plan)

    def _coerce_plan(self, task_id: str, raw: Dict[str, Any]) -> FulfillmentPlan:
        steps = [PlanStep(**s) if not isinstance(s, PlanStep) else s for s in raw.get("steps", [])]
        return FulfillmentPlan(plan_id=raw["plan_id"], task_id=task_id, steps=steps, risk=raw.get("risk", {}), artifacts=raw.get("artifacts", []))

    def _emit_event(self, name: str, envelope, **fields) -> None:
        logger.info(name, extra={"extra_fields": {"task_id": envelope.task_id, **fields}})
        if self.metrics:
            self.metrics.counter(f"fulfillment_{name}_total").inc()
