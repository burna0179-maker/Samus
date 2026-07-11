"""Pure-functional tests for the DAG executor helpers in backend.fulfillment.dag.

All tests are synchronous and do not require network access.  execute_plan
(async, requires mocked signed_post_json) is covered in the companion file
test_fulfillment_dag_executor_async.py.

Coverage targets
----------------
* validate_plan — 8 tests
* _topological_order — 1 test
* ingest_result — 5 tests
* resume_plan — 1 test
* finalize_plan — 3 tests

Total: 18 tests (>= required 15).
"""

from __future__ import annotations

import pytest

from backend.fulfillment.dag import (
    PLAN_STATUS_COMPLETE,
    PLAN_STATUS_FAILED,
    PLAN_STATUS_RUNNING,
    STEP_STATUS_DONE,
    STEP_STATUS_FAILED,
    STEP_STATUS_PENDING,
    STEP_STATUS_RUNNING,
    STEP_STATUS_SKIPPED,
    FulfillmentPlan,
    PlanStep,
    _topological_order,
    build_execution_graph_v2,
    finalize_plan,
    ingest_result,
    resume_plan,
    validate_plan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _simple_plan(plan_id: str = "p1", steps: int = 3) -> FulfillmentPlan:
    """Return a linear plan A -> B -> C with properly-prefixed IDs."""
    assert steps >= 1
    step_names = [chr(ord("A") + i) for i in range(steps)]
    plan_steps: list[PlanStep] = []
    for i, name in enumerate(step_names):
        step_id = f"{plan_id}:{name}"
        depends = [f"{plan_id}:{step_names[i - 1]}"] if i > 0 else []
        plan_steps.append(
            PlanStep(
                id=step_id,
                type=f"svc.{name.lower()}",
                depends_on=depends,
            )
        )
    return FulfillmentPlan(
        plan_id=plan_id,
        task_id="task-1",
        steps=plan_steps,
    )


# ---------------------------------------------------------------------------
# validate_plan tests
# ---------------------------------------------------------------------------


def test_validate_plan_empty_steps_returns_error():
    plan = FulfillmentPlan(plan_id="p1", task_id="t1", steps=[])
    errors = validate_plan(plan)
    assert any("steps must be non-empty" in e for e in errors)


def test_validate_plan_empty_plan_id_returns_error():
    plan = FulfillmentPlan(
        plan_id="",
        task_id="t1",
        steps=[PlanStep(id=":step_a", type="svc.a")],
    )
    errors = validate_plan(plan)
    assert any("plan_id" in e for e in errors)


def test_validate_plan_duplicate_step_ids_returns_error():
    plan = FulfillmentPlan(
        plan_id="p1",
        task_id="t1",
        steps=[
            PlanStep(id="p1:step_a", type="svc.a"),
            PlanStep(id="p1:step_a", type="svc.b"),  # duplicate
        ],
    )
    errors = validate_plan(plan)
    assert any("duplicate step id" in e for e in errors)


def test_validate_plan_unknown_depends_on_returns_error():
    plan = FulfillmentPlan(
        plan_id="p1",
        task_id="t1",
        steps=[
            PlanStep(id="p1:step_a", type="svc.a", depends_on=["p1:nonexistent"]),
        ],
    )
    errors = validate_plan(plan)
    assert any("depends_on unknown id" in e for e in errors)


def test_validate_plan_step_type_without_dot_returns_error():
    plan = FulfillmentPlan(
        plan_id="p1",
        task_id="t1",
        steps=[
            PlanStep(id="p1:step_a", type="nodot"),  # no dot
        ],
    )
    errors = validate_plan(plan)
    assert any("<service>.<action>" in e for e in errors)


def test_validate_plan_well_formed_plan_returns_empty_list():
    plan = _simple_plan("myplan", steps=3)
    errors = validate_plan(plan)
    assert errors == []


def test_validate_plan_cyclic_dependency_returns_error():
    """A -> B, B -> A is a cycle."""
    plan = FulfillmentPlan(
        plan_id="p1",
        task_id="t1",
        steps=[
            PlanStep(id="p1:step_a", type="svc.a", depends_on=["p1:step_b"]),
            PlanStep(id="p1:step_b", type="svc.b", depends_on=["p1:step_a"]),
        ],
    )
    errors = validate_plan(plan)
    assert any("circular" in e.lower() for e in errors)


def test_validate_plan_step_id_without_plan_prefix_returns_error():
    plan = FulfillmentPlan(
        plan_id="p1",
        task_id="t1",
        steps=[
            PlanStep(id="wrong:step_a", type="svc.a"),  # wrong prefix
        ],
    )
    errors = validate_plan(plan)
    assert any("does not start with plan prefix" in e for e in errors)


# ---------------------------------------------------------------------------
# _topological_order tests
# ---------------------------------------------------------------------------


def test_topological_order_yields_dependency_respecting_order():
    plan = _simple_plan("plan1", steps=3)
    ordered = _topological_order(plan)
    ids = [s.id for s in ordered]
    # A must come before B, B before C
    assert ids.index("plan1:A") < ids.index("plan1:B")
    assert ids.index("plan1:B") < ids.index("plan1:C")


# ---------------------------------------------------------------------------
# ingest_result tests
# ---------------------------------------------------------------------------


def test_ingest_result_done_status_propagates_to_dependents():
    """3-step linear plan A -> B -> C.

    Ingest A done => [B] becomes ready.
    Ingest B done => [C] becomes ready.
    """
    plan = _simple_plan("p1", steps=3)
    # A has no dependencies — should already be ready, but we start in PENDING.
    step_a_id = "p1:A"
    step_b_id = "p1:B"
    step_c_id = "p1:C"

    newly_after_a = ingest_result(plan, step_a_id, STEP_STATUS_DONE)
    assert [s.id for s in newly_after_a] == [step_b_id]

    newly_after_b = ingest_result(plan, step_b_id, STEP_STATUS_DONE)
    assert [s.id for s in newly_after_b] == [step_c_id]


def test_ingest_result_failed_status_does_not_propagate():
    """Failing A does not unlock B."""
    plan = _simple_plan("p1", steps=3)
    newly = ingest_result(plan, "p1:A", STEP_STATUS_FAILED)
    assert newly == []


def test_ingest_result_invalid_status_raises_value_error():
    plan = _simple_plan("p1", steps=2)
    with pytest.raises(ValueError, match="invalid status"):
        ingest_result(plan, "p1:A", STEP_STATUS_RUNNING)


def test_ingest_result_unknown_step_id_raises_value_error():
    plan = _simple_plan("p1", steps=2)
    with pytest.raises(ValueError, match="not found"):
        ingest_result(plan, "p1:NONEXISTENT", STEP_STATUS_DONE)


def test_ingest_result_stores_output_in_step_payload():
    plan = _simple_plan("p1", steps=2)
    output = {"result": "ok", "rows": 42}
    ingest_result(plan, "p1:A", STEP_STATUS_DONE, output=output)
    step_a = next(s for s in plan.steps if s.id == "p1:A")
    assert step_a.payload["_output"] == output


# ---------------------------------------------------------------------------
# resume_plan tests
# ---------------------------------------------------------------------------


def test_resume_plan_resets_running_steps_to_pending():
    plan = _simple_plan("p1", steps=3)
    # Manually mark two steps RUNNING.
    plan.steps[0].status = STEP_STATUS_RUNNING
    plan.steps[1].status = STEP_STATUS_RUNNING
    plan.steps[2].status = STEP_STATUS_DONE

    result = resume_plan(plan)
    assert result is plan  # mutated in-place and returned
    assert plan.steps[0].status == STEP_STATUS_PENDING
    assert plan.steps[1].status == STEP_STATUS_PENDING
    assert plan.steps[2].status == STEP_STATUS_DONE  # unchanged


# ---------------------------------------------------------------------------
# finalize_plan tests
# ---------------------------------------------------------------------------


def test_finalize_plan_all_done_marks_complete():
    plan = _simple_plan("p1", steps=3)
    for s in plan.steps:
        s.status = STEP_STATUS_DONE

    result = finalize_plan(plan)
    assert result.status == PLAN_STATUS_COMPLETE


def test_finalize_plan_any_failed_marks_failed():
    plan = _simple_plan("p1", steps=3)
    plan.steps[0].status = STEP_STATUS_DONE
    plan.steps[1].status = STEP_STATUS_FAILED
    plan.steps[2].status = STEP_STATUS_SKIPPED

    result = finalize_plan(plan)
    assert result.status == PLAN_STATUS_FAILED


def test_finalize_plan_still_running_stays_running():
    plan = _simple_plan("p1", steps=3)
    plan.steps[0].status = STEP_STATUS_DONE
    plan.steps[1].status = STEP_STATUS_RUNNING
    plan.steps[2].status = STEP_STATUS_PENDING

    result = finalize_plan(plan)
    assert result.status == PLAN_STATUS_RUNNING


# ---------------------------------------------------------------------------
# build_execution_graph_v2 round-trip sanity (bonus)
# ---------------------------------------------------------------------------


def test_build_execution_graph_v2_produces_valid_plan():
    plan = build_execution_graph_v2(
        "task-xyz",
        {
            "actions": [
                {"type": "seo.audit_site", "url": "https://example.com"},
                {"type": "scaffold.generate_assets"},
            ]
        },
        {},
    )
    errors = validate_plan(plan)
    assert errors == [], f"Expected no errors, got: {errors}"
    assert plan.plan_id  # auto-generated; just confirm non-empty
    assert plan.task_id == "task-xyz"
    # validate + prepare + 2 actions + verify = 5 steps
    assert len(plan.steps) == 5
