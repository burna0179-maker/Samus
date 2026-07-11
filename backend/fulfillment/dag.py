"""DAG-based fulfillment planner — stage 1+2 of the v2 roadmap.

Provides the ``FulfillmentPlan`` + ``PlanStep`` dataclasses and the
``build_execution_graph_v2()`` factory that emits a structured, dependency-
linked DAG from a raw task payload.  No Pydantic — pure stdlib so this module
remains importable in any environment without the web-layer deps.

Relationship to v1:
  - ``backend.fulfillment.logic.build_execution_graph`` (v1) returns a plain
    list-of-dicts for the existing callers.
  - This module exposes a richer typed representation that ``plan_fulfillment``
    attaches under the ``plan`` key when ``metadata["plan_format"] == "v2"``.
  - Stages 3-9 of the v2 roadmap will extend this with DAG execution,
    partial-failure handling, and circuit-breaker-per-step-type.

Public surface: see ``__all__``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import uuid4

_LOG = logging.getLogger("samus.fulfillment.dag")

__all__ = [
    # Batch 1 — plan model + builder
    "PlanStep",
    "FulfillmentPlan",
    "build_execution_graph_v2",
    "plan_to_dict",
    "plan_from_dict",
    # Batch 2 — status constants + executor + ingest loop + validator
    "STEP_STATUS_PENDING",
    "STEP_STATUS_RUNNING",
    "STEP_STATUS_DONE",
    "STEP_STATUS_FAILED",
    "STEP_STATUS_SKIPPED",
    "PLAN_STATUS_PLANNED",
    "PLAN_STATUS_RUNNING",
    "PLAN_STATUS_COMPLETE",
    "PLAN_STATUS_FAILED",
    "validate_plan",
    "ingest_result",
    "execute_plan",
    "resume_plan",
    "finalize_plan",
]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PlanStep:
    """A single node in the fulfillment DAG.

    Attributes:
        id: Unique step identifier within this plan, formatted as
            ``"{plan_id}:{step_name}"`` to keep idempotency keys globally
            unique across concurrent plans.
        type: Dotted ``"<service>.<action>"`` routing address used by the
            DAG executor to dispatch the step to the correct workcell.
        depends_on: Ordered list of ``PlanStep.id`` values that must reach
            ``status == "done"`` before this step may be dispatched.
        payload: Arbitrary step-specific input data forwarded verbatim to the
            target workcell.
        retryable: When ``True`` the executor may retry this step on transient
            failure; when ``False`` a failure causes the step to be skipped
            and downstream dependents unblocked (or the plan to fail,
            depending on the executor policy).
        timeout_sec: Optional hard ceiling on step execution time in seconds.
            ``None`` means no timeout enforced at the planner level.
        status: Current lifecycle state — one of
            ``pending | running | done | failed | skipped``.
    """

    id: str
    type: str                                    # "<service>.<action>"
    depends_on: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    retryable: bool = True
    timeout_sec: int | None = None
    status: str = "pending"                      # pending|running|done|failed|skipped


@dataclass
class FulfillmentPlan:
    """Top-level container for a fulfillment execution plan.

    Attributes:
        plan_id: Globally unique plan identifier, formatted as
            ``"plan_{task_id}_{8-hex-chars}"``.
        task_id: Parent task identifier that triggered this plan.
        steps: Ordered list of ``PlanStep`` nodes (topological order is not
            enforced here — the executor resolves order via ``depends_on``).
        risk: Risk metadata forwarded from the governance decision layer or
            from ``payload["risk"]``.
        artifacts: List of artifact descriptors written during plan execution;
            populated by the executor in later stages.
        status: Current lifecycle state — one of
            ``planned | running | complete | failed``.
    """

    plan_id: str
    task_id: str
    steps: list[PlanStep]
    risk: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    status: str = "planned"                      # planned|running|complete|failed


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_execution_graph_v2(
    task_id: str,
    payload: dict[str, Any],
    metadata: dict[str, Any],
) -> FulfillmentPlan:
    """Build a dependency-linked DAG plan from a raw task payload.

    Emits the canonical four-layer skeleton::

        validate_inputs → prepare_assets → action_1..N → verify_output

    When ``payload["actions"]`` is empty the skeleton collapses to three
    steps (validate_inputs → prepare_assets → verify_output) so the plan is
    never empty and the executor always has a complete, runnable graph.

    Step IDs are formatted as ``"{plan_id}:{step_name}"`` so step-level
    idempotency keys remain unique across concurrent plans for the same task.

    Dependency rules:
    - ``validate_inputs`` has no dependencies (root node).
    - ``prepare_assets`` depends on ``validate_inputs``.
    - Each action step depends on ``prepare_assets`` (fan-out from the
      prepare gate; actions are independent of each other by default).
    - ``verify_output`` depends on ALL action step IDs, or on
      ``prepare_assets`` alone when there are no actions.

    Args:
        task_id: Parent task identifier.
        payload: Task payload; may contain ``actions`` (list of dicts, each
            with at minimum ``type`` and ``payload`` keys) and ``risk`` (dict).
        metadata: Task metadata (not consumed here; reserved for future stages
            that may inject step-level metadata).

    Returns:
        A ``FulfillmentPlan`` in ``status="planned"`` with all steps in
        ``status="pending"``.
    """
    plan_id = f"plan_{task_id}_{uuid4().hex[:8]}"
    actions: list[dict[str, Any]] = list(payload.get("actions") or [])
    risk: dict[str, Any] = dict(payload.get("risk") or {})

    _LOG.debug(
        "build_execution_graph_v2 plan_id=%s task_id=%s action_count=%d",
        plan_id,
        task_id,
        len(actions),
    )

    # -- Fixed preamble steps ------------------------------------------------
    validate_id = f"{plan_id}:validate_inputs"
    prepare_id = f"{plan_id}:prepare_assets"

    steps: list[PlanStep] = [
        PlanStep(
            id=validate_id,
            type="fulfillment.validate_inputs",
            depends_on=[],
            payload={},
        ),
        PlanStep(
            id=prepare_id,
            type="fulfillment.prepare_assets",
            depends_on=[validate_id],
            payload={},
        ),
    ]

    # -- Action steps (fan-out from prepare_assets) --------------------------
    action_step_ids: list[str] = []
    for idx, action in enumerate(actions, start=1):
        step_name = f"action_{idx}"
        step_id = f"{plan_id}:{step_name}"
        action_step_ids.append(step_id)
        steps.append(
            PlanStep(
                id=step_id,
                type=str(action.get("type") or f"fulfillment.action_{idx}"),
                depends_on=[prepare_id],
                payload=dict(action.get("payload") or {}),
            )
        )

    # -- Fixed terminus step -------------------------------------------------
    verify_depends = action_step_ids if action_step_ids else [prepare_id]
    verify_id = f"{plan_id}:verify_output"
    steps.append(
        PlanStep(
            id=verify_id,
            type="fulfillment.verify_output",
            depends_on=verify_depends,
            payload={},
        )
    )

    plan = FulfillmentPlan(
        plan_id=plan_id,
        task_id=task_id,
        steps=steps,
        risk=risk,
        artifacts=[],
        status="planned",
    )

    _LOG.info(
        "plan built plan_id=%s task_id=%s steps=%d",
        plan_id,
        task_id,
        len(steps),
    )
    return plan


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def plan_to_dict(plan: FulfillmentPlan) -> dict[str, Any]:
    """JSON-safe serialization of a ``FulfillmentPlan`` via ``dataclasses.asdict``.

    The output is suitable for inclusion in API responses or SQS message
    bodies without further transformation.
    """
    return asdict(plan)


def plan_from_dict(d: dict[str, Any]) -> FulfillmentPlan:
    """Reconstruct a ``FulfillmentPlan`` from its dict representation.

    Supports full roundtrip with ``plan_to_dict``; also accepts dicts
    produced by external callers that include the same keys.
    """
    raw_steps: list[dict[str, Any]] = d.get("steps") or []
    steps: list[PlanStep] = [
        PlanStep(
            id=s["id"],
            type=s["type"],
            depends_on=list(s.get("depends_on") or []),
            payload=dict(s.get("payload") or {}),
            retryable=bool(s.get("retryable", True)),
            timeout_sec=s.get("timeout_sec"),
            status=str(s.get("status", "pending")),
        )
        for s in raw_steps
    ]
    return FulfillmentPlan(
        plan_id=str(d["plan_id"]),
        task_id=str(d["task_id"]),
        steps=steps,
        risk=dict(d.get("risk") or {}),
        artifacts=list(d.get("artifacts") or []),
        status=str(d.get("status", "planned")),
    )
STEP_STATUS_PENDING: str = "pending"
STEP_STATUS_RUNNING: str = "running"
STEP_STATUS_DONE: str = "done"
STEP_STATUS_FAILED: str = "failed"
STEP_STATUS_SKIPPED: str = "skipped"

PLAN_STATUS_PLANNED: str = "planned"
PLAN_STATUS_RUNNING: str = "running"
PLAN_STATUS_COMPLETE: str = "complete"
PLAN_STATUS_FAILED: str = "failed"

_VALID_INGEST_STATUSES: frozenset[str] = frozenset(
    {STEP_STATUS_DONE, STEP_STATUS_FAILED, STEP_STATUS_SKIPPED}
)
_TERMINAL_STEP_STATUSES: frozenset[str] = frozenset(
    {STEP_STATUS_DONE, STEP_STATUS_FAILED, STEP_STATUS_SKIPPED}
)


# ---------------------------------------------------------------------------
# Batch 2 — internal helpers
# ---------------------------------------------------------------------------


def _find_step(plan: FulfillmentPlan, step_id: str) -> PlanStep | None:
    """Return the PlanStep with the given id, or None."""
    for step in plan.steps:
        if step.id == step_id:
            return step
    return None


def _step_ready(step: PlanStep, plan: FulfillmentPlan) -> bool:
    """True if every step in step.depends_on is DONE or SKIPPED."""
    status_by_id: dict[str, str] = {s.id: s.status for s in plan.steps}
    return all(
        status_by_id.get(dep) in (STEP_STATUS_DONE, STEP_STATUS_SKIPPED)
        for dep in step.depends_on
    )


def _all_terminal(plan: FulfillmentPlan) -> bool:
    """True if every step's status is in {DONE, FAILED, SKIPPED}."""
    return all(s.status in _TERMINAL_STEP_STATUSES for s in plan.steps)


def _update_plan_status(plan: FulfillmentPlan) -> None:
    """Derive and set plan.status from its steps' current statuses."""
    statuses = {s.status for s in plan.steps}
    if STEP_STATUS_RUNNING in statuses:
        plan.status = PLAN_STATUS_RUNNING
        return
    if STEP_STATUS_FAILED in statuses:
        plan.status = PLAN_STATUS_FAILED
        return
    if all(s.status in (STEP_STATUS_DONE, STEP_STATUS_SKIPPED) for s in plan.steps):
        plan.status = PLAN_STATUS_COMPLETE
        return
    plan.status = PLAN_STATUS_PLANNED


def _topological_order(plan: FulfillmentPlan) -> list[PlanStep]:
    """Return steps in dependency-respecting (Kahn's algorithm) order.

    Raises ValueError if the graph contains a cycle.
    """
    step_by_id: dict[str, PlanStep] = {s.id: s for s in plan.steps}
    in_degree: dict[str, int] = {s.id: len(s.depends_on) for s in plan.steps}
    queue: list[str] = [sid for sid, deg in in_degree.items() if deg == 0]
    ordered: list[PlanStep] = []

    while queue:
        sid = queue.pop(0)
        ordered.append(step_by_id[sid])
        for s in plan.steps:
            if sid in s.depends_on:
                in_degree[s.id] -= 1
                if in_degree[s.id] == 0:
                    queue.append(s.id)

    if len(ordered) != len(plan.steps):
        remaining = [sid for sid, deg in in_degree.items() if deg > 0]
        raise ValueError(f"Cycle detected in plan {plan.plan_id!r}; remaining nodes: {remaining}")

    return ordered


# ---------------------------------------------------------------------------
# Batch 2 — DFS-based cycle detector (used by validate_plan)
# ---------------------------------------------------------------------------


def _has_cycle(plan: FulfillmentPlan) -> bool:
    """Return True if the dependency graph has a cycle (DFS white-grey-black)."""
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = {s.id: WHITE for s in plan.steps}
    step_by_id: dict[str, PlanStep] = {s.id: s for s in plan.steps}

    def dfs(node_id: str) -> bool:
        color[node_id] = GREY
        step = step_by_id.get(node_id)
        if step is None:
            color[node_id] = BLACK
            return False
        for dep in step.depends_on:
            if dep not in color:
                # Unknown node — validate_plan already checks referential
                # integrity; skip here to avoid false-positive cycle detection.
                continue
            if color[dep] == GREY:
                return True
            if color[dep] == WHITE and dfs(dep):
                return True
        color[node_id] = BLACK
        return False

    for s in plan.steps:
        if color[s.id] == WHITE and dfs(s.id):
            return True
    return False


# ---------------------------------------------------------------------------
# Batch 2 — validate_plan
# ---------------------------------------------------------------------------


def validate_plan(plan: FulfillmentPlan) -> list[str]:
    """Return a list of validation error strings.

    An empty list means the plan is valid.  Checks:

    * plan_id is non-empty
    * task_id is non-empty
    * steps list is non-empty
    * all step.id values are unique
    * all step.id values start with ``{plan.plan_id}:``
    * all depends_on entries reference existing step IDs
    * no circular dependencies
    * all step.type values are non-empty and in ``"<service>.<action>"`` format
    """
    errors: list[str] = []

    if not plan.plan_id:
        errors.append("plan_id must be non-empty")
    if not plan.task_id:
        errors.append("task_id must be non-empty")
    if not plan.steps:
        errors.append("steps must be non-empty")
        # Remaining checks need steps — bail early.
        return errors

    # Unique step IDs
    seen_ids: set[str] = set()
    for step in plan.steps:
        if step.id in seen_ids:
            errors.append(f"duplicate step id: {step.id!r}")
        seen_ids.add(step.id)

    # Step ID naming convention
    prefix = f"{plan.plan_id}:"
    for step in plan.steps:
        if not step.id.startswith(prefix):
            errors.append(
                f"step id {step.id!r} does not start with plan prefix {prefix!r}"
            )

    # Referential integrity of depends_on
    all_ids = {s.id for s in plan.steps}
    for step in plan.steps:
        for dep in step.depends_on:
            if dep not in all_ids:
                errors.append(
                    f"step {step.id!r} depends_on unknown id {dep!r}"
                )

    # Cycle detection — only meaningful if no referential-integrity errors
    ref_errors = [e for e in errors if "depends_on unknown id" in e]
    if not ref_errors and _has_cycle(plan):
        errors.append(f"plan {plan.plan_id!r} contains a circular dependency")

    # Step type format: "<service>.<action>" — exactly one dot
    for step in plan.steps:
        if not step.type:
            errors.append(f"step {step.id!r} has empty type")
        elif step.type.count(".") != 1:
            errors.append(
                f"step {step.id!r} type {step.type!r} must be in '<service>.<action>' format"
            )

    return errors


# ---------------------------------------------------------------------------
# Batch 2 — ingest_result
# ---------------------------------------------------------------------------


def ingest_result(
    plan: FulfillmentPlan,
    step_id: str,
    status: str,
    output: dict[str, Any] | None = None,
) -> list[PlanStep]:
    """Record the result of a dispatched step and return newly-ready dependents.

    Parameters
    ----------
    plan:
        The mutable FulfillmentPlan to update.
    step_id:
        ID of the step whose result is being ingested.
    status:
        Must be one of DONE / FAILED / SKIPPED.
    output:
        Optional result dict stored in ``step.payload["_output"]``.

    Returns
    -------
    list[PlanStep]
        Steps that became ready (PENDING + all depends_on now DONE/SKIPPED)
        as a direct consequence of this ingest.
    """
    if status not in _VALID_INGEST_STATUSES:
        raise ValueError(
            f"ingest_result: invalid status {status!r}; "
            f"must be one of {sorted(_VALID_INGEST_STATUSES)}"
        )

    step = _find_step(plan, step_id)
    if step is None:
        raise ValueError(
            f"ingest_result: step_id {step_id!r} not found in plan {plan.plan_id!r}"
        )

    step.status = status
    if output is not None:
        step.payload["_output"] = output

    _LOG.debug(
        "ingest_result plan=%s step=%s status=%s",
        plan.plan_id,
        step_id,
        status,
    )

    # Return dependents that just became ready.
    newly_ready: list[PlanStep] = []
    for candidate in plan.steps:
        if (
            candidate.status == STEP_STATUS_PENDING
            and step_id in candidate.depends_on
            and _step_ready(candidate, plan)
        ):
            newly_ready.append(candidate)

    return newly_ready


# ---------------------------------------------------------------------------
# Batch 2 — execute_plan (async)
# ---------------------------------------------------------------------------


async def execute_plan(
    plan: FulfillmentPlan,
    *,
    gateway_url: str,
    hmac_key: str,
    max_concurrent: int = 1,  # Phase 1: sequential; asyncio.gather batching deferred
) -> FulfillmentPlan:
    """Execute a FulfillmentPlan by dispatching each step via signed_post_json.

    Steps are executed sequentially (max_concurrent=1).  For each ready step:

    1. Mark it RUNNING.
    2. POST the step envelope to ``{gateway_url}/dispatch/{service}``.
    3. On 2xx: ingest_result(DONE, output=response_body).
    4. On non-2xx or exception: ingest_result(FAILED, output=error_dict).
    5. Repeat until no more steps are ready.

    The function is idempotent: if the plan is already COMPLETE it returns
    unchanged.

    Parameters
    ----------
    plan:
        Mutable FulfillmentPlan.  Modified in-place and returned.
    gateway_url:
        Base URL of the gateway service (e.g. "http://samus-gateway:8080").
    hmac_key:
        Shared HMAC secret passed to signed_post_json.
    max_concurrent:
        Kept for future use.  Currently ignored (always sequential).
    """
    # Deferred import to avoid circular imports at module load time.
    from backend.common.http_client import signed_post_json

    if plan.status == PLAN_STATUS_COMPLETE:
        _LOG.debug("execute_plan: plan %s already complete — skipping", plan.plan_id)
        return plan

    errors = validate_plan(plan)
    if errors:
        plan.status = PLAN_STATUS_FAILED
        plan.risk.setdefault("validation_errors", errors)
        _LOG.warning(
            "execute_plan: plan %s failed validation: %s", plan.plan_id, errors
        )
        return plan

    # Walk in topological order; keep looping until nothing new is ready.
    try:
        ordered = _topological_order(plan)
    except ValueError as exc:
        plan.status = PLAN_STATUS_FAILED
        plan.risk.setdefault("validation_errors", [str(exc)])
        return plan

    progress = True
    while progress:
        progress = False
        for step in ordered:
            if step.status != STEP_STATUS_PENDING:
                continue
            if not _step_ready(step, plan):
                continue

            # Dispatch this step.
            step.status = STEP_STATUS_RUNNING
            _update_plan_status(plan)
            progress = True

            service, action = step.type.split(".", 1)
            envelope = {
                "task_id": f"{plan.task_id}:{step.id}",
                "payload": step.payload,
                "metadata": {
                    "action": action,
                    "plan_id": plan.plan_id,
                    "step_id": step.id,
                },
            }

            _LOG.info(
                "execute_plan: dispatching step=%s service=%s action=%s",
                step.id,
                service,
                action,
            )

            try:
                response = await signed_post_json(
                    base_url=gateway_url,
                    path=f"/dispatch/{service}",
                    payload=envelope,
                    secret=hmac_key,
                )
                if response.status_code < 200 or response.status_code >= 300:
                    error_output: dict[str, Any] = {
                        "error": f"HTTP {response.status_code}",
                        "status_code": response.status_code,
                        "body": response.text[:512],
                    }
                    _LOG.warning(
                        "execute_plan: step=%s got HTTP %s",
                        step.id,
                        response.status_code,
                    )
                    ingest_result(plan, step.id, STEP_STATUS_FAILED, output=error_output)
                else:
                    try:
                        response_body: dict[str, Any] = response.json()
                    except Exception:
                        response_body = {"raw": response.text[:512]}
                    ingest_result(plan, step.id, STEP_STATUS_DONE, output=response_body)
                    _LOG.info("execute_plan: step=%s done", step.id)

            except Exception as exc:
                _LOG.exception("execute_plan: step=%s raised exception", step.id)
                ingest_result(
                    plan,
                    step.id,
                    STEP_STATUS_FAILED,
                    output={"error": str(exc), "status_code": None},
                )

    _update_plan_status(plan)
    _LOG.info(
        "execute_plan: plan=%s finished with status=%s",
        plan.plan_id,
        plan.status,
    )
    return plan


# ---------------------------------------------------------------------------
# Batch 2 — resume_plan / finalize_plan
# ---------------------------------------------------------------------------


def resume_plan(plan: FulfillmentPlan) -> FulfillmentPlan:
    """Reset every RUNNING step to PENDING so execute_plan can retry them.

    Does NOT itself re-dispatch.  Callers call ``execute_plan`` after this.
    """
    for step in plan.steps:
        if step.status == STEP_STATUS_RUNNING:
            step.status = STEP_STATUS_PENDING
            _LOG.debug("resume_plan: reset step=%s to pending", step.id)
    return plan


def finalize_plan(plan: FulfillmentPlan) -> FulfillmentPlan:
    """Derive a terminal plan status from current step statuses.

    If any step is still RUNNING the plan stays RUNNING (not forced terminal).
    Otherwise the plan is marked COMPLETE or FAILED per _update_plan_status.
    """
    _update_plan_status(plan)
    return plan
