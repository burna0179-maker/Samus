"""Async tests for dag.execute_plan with mocked signed_post_json.

Uses monkeypatch to replace backend.common.http_client.signed_post_json with a
configurable fake coroutine so no real HTTP calls are made.

Test coverage
-------------
* test_execute_plan_idempotent_when_already_complete
* test_execute_plan_invalid_plan_sets_failed_status_and_stamps_errors_into_risk
* test_execute_plan_sequential_steps_all_succeed
* test_execute_plan_step_failure_blocks_dependents
* test_execute_plan_dispatcher_exception_marks_step_failed

Total: 5 tests (>= required 5).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest  # noqa: F401  (kept for any test that still uses pytest.raises etc.)

from backend.fulfillment.dag import (
    PLAN_STATUS_COMPLETE,
    PLAN_STATUS_FAILED,
    STEP_STATUS_DONE,
    STEP_STATUS_FAILED,
    STEP_STATUS_PENDING,
    FulfillmentPlan,
    PlanStep,
    build_execution_graph_v2,
    execute_plan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(status_code: int, json_data: dict | None = None) -> MagicMock:
    """Return a mock httpx.Response-like object."""
    resp = MagicMock()
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("no json")
        resp.text = ""
    resp.text = str(json_data or "")
    return resp


def _two_step_plan() -> FulfillmentPlan:
    """Return a minimal 2-step plan (A -> B) suitable for async tests.

    We build this manually instead of using build_execution_graph_v2 so that
    the plan has exactly 2 steps and the test expectations are simple.
    """
    return FulfillmentPlan(
        plan_id="tp",
        task_id="t1",
        steps=[
            PlanStep(id="tp:step_a", type="svc.action_a", depends_on=[]),
            PlanStep(id="tp:step_b", type="svc.action_b", depends_on=["tp:step_a"]),
        ],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_execute_plan_idempotent_when_already_complete(monkeypatch):
    """execute_plan must return immediately if plan.status is already COMPLETE."""
    calls: list[Any] = []

    async def fake_signed_post(*args: Any, **kwargs: Any):
        calls.append((args, kwargs))
        return _make_response(200, {"ok": True})

    monkeypatch.setattr("backend.common.http_client.signed_post_json", fake_signed_post)

    plan = _two_step_plan()
    plan.status = PLAN_STATUS_COMPLETE

    result = asyncio.run(
        execute_plan(
            plan,
            gateway_url="http://test:8080",
            hmac_key="test-key",
        )
    )

    assert result.status == PLAN_STATUS_COMPLETE
    assert calls == []  # no dispatch calls were made


def test_execute_plan_invalid_plan_sets_failed_status_and_stamps_errors_into_risk(
    monkeypatch,
):
    """An invalid plan (empty plan_id) must be rejected without dispatching."""
    calls: list[Any] = []

    async def fake_signed_post(*args: Any, **kwargs: Any):
        calls.append(1)
        return _make_response(200, {})

    monkeypatch.setattr("backend.common.http_client.signed_post_json", fake_signed_post)

    # Invalid: empty plan_id and empty task_id
    bad_plan = FulfillmentPlan(
        plan_id="",
        task_id="",
        steps=[PlanStep(id=":s", type="svc.a")],
    )

    result = asyncio.run(
        execute_plan(
            bad_plan,
            gateway_url="http://test:8080",
            hmac_key="test-key",
        )
    )

    assert result.status == PLAN_STATUS_FAILED
    assert "validation_errors" in result.risk
    assert len(result.risk["validation_errors"]) > 0
    assert calls == []


def test_execute_plan_sequential_steps_all_succeed(monkeypatch):
    """All steps succeed => plan.status == COMPLETE after execute_plan."""
    dispatched: list[str] = []

    async def fake_signed_post(base_url: str, path: str, payload: dict, **kwargs: Any):
        # Extract step_id from the metadata embedded in the envelope.
        step_id = payload.get("metadata", {}).get("step_id", "?")
        dispatched.append(step_id)
        return _make_response(200, {"step_done": True})

    monkeypatch.setattr("backend.common.http_client.signed_post_json", fake_signed_post)

    plan = build_execution_graph_v2(
        "taskX",
        {"actions": [{"type": "seo.audit_site"}, {"type": "scaffold.generate_assets"}]},
        {},
    )

    result = asyncio.run(
        execute_plan(
            plan,
            gateway_url="http://test:8080",
            hmac_key="sec",
        )
    )

    assert result.status == PLAN_STATUS_COMPLETE
    # All steps should have been dispatched.
    assert len(dispatched) == len(plan.steps)
    for step in result.steps:
        assert step.status == STEP_STATUS_DONE


def test_execute_plan_step_failure_blocks_dependents(monkeypatch):
    """If step A fails, step B (which depends on A) must remain PENDING."""

    async def fake_signed_post(base_url: str, path: str, payload: dict, **kwargs: Any):
        step_id = payload.get("metadata", {}).get("step_id", "")
        if step_id == "tp:step_a":
            return _make_response(500, {"error": "internal"})
        return _make_response(200, {"ok": True})

    monkeypatch.setattr("backend.common.http_client.signed_post_json", fake_signed_post)

    plan = _two_step_plan()

    result = asyncio.run(
        execute_plan(
            plan,
            gateway_url="http://test:8080",
            hmac_key="sec",
        )
    )

    assert result.status == PLAN_STATUS_FAILED
    step_a = next(s for s in result.steps if s.id == "tp:step_a")
    step_b = next(s for s in result.steps if s.id == "tp:step_b")
    assert step_a.status == STEP_STATUS_FAILED
    assert step_b.status == STEP_STATUS_PENDING  # never dispatched


def test_execute_plan_dispatcher_exception_marks_step_failed(monkeypatch):
    """If signed_post_json raises an exception the step must be marked FAILED."""

    async def fake_signed_post(*args: Any, **kwargs: Any):
        raise ConnectionError("connection refused")

    monkeypatch.setattr("backend.common.http_client.signed_post_json", fake_signed_post)

    plan = _two_step_plan()

    result = asyncio.run(
        execute_plan(
            plan,
            gateway_url="http://test:8080",
            hmac_key="sec",
        )
    )

    assert result.status == PLAN_STATUS_FAILED
    step_a = next(s for s in result.steps if s.id == "tp:step_a")
    assert step_a.status == STEP_STATUS_FAILED
    assert "error" in step_a.payload.get("_output", {})
    assert "connection refused" in step_a.payload["_output"]["error"]
