"""HTTP surface for the fulfillment workcell (doc §7).

Actions dispatched via POST /work:

  plan_execution  — original execution planning (logic.plan_fulfillment)
  validate_plan   — validate a FulfillmentPlan dict, return errors list
  execute_plan    — execute a FulfillmentPlan via signed inter-service dispatch
  ingest_result   — record a step result and return newly-ready step ids
  resume_plan     — reset RUNNING steps to PENDING (pre-retry)
  finalize_plan   — derive terminal plan status from step statuses
"""

from __future__ import annotations

import logging

from backend.common.app_factory import create_base_app
from backend.common.capabilities import check_capability
from backend.common.config import get_settings
from backend.common.models import TaskEnvelope

from .dag import (
    execute_plan,
    finalize_plan,
    ingest_result,
    plan_from_dict,
    plan_to_dict,
    resume_plan,
    validate_plan,
)
from .logic import plan_fulfillment

_LOG = logging.getLogger("samus.fulfillment.app")

app = create_base_app(service_name="fulfillment")


@app.post("/work")
async def work_endpoint(envelope: TaskEnvelope) -> dict:
    """Route fulfillment actions based on ``envelope.metadata.action``."""
    payload = envelope.payload or {}
    metadata = envelope.metadata or {}
    action = metadata.get("action") or "plan_execution"

    # ------------------------------------------------------------------
    # plan_execution  (original — keep backward-compatible)
    # ------------------------------------------------------------------
    if action == "plan_execution":
        check_capability("fulfillment", "plan_execution")
        _LOG.info("fulfillment action=plan_execution task_id=%s", envelope.task_id)
        return plan_fulfillment(envelope.task_id, payload, metadata)

    # ------------------------------------------------------------------
    # validate_plan
    # ------------------------------------------------------------------
    if action == "validate_plan":
        check_capability("fulfillment", "validate_plan")
        _LOG.info("fulfillment action=validate_plan task_id=%s", envelope.task_id)
        plan = plan_from_dict(payload["plan"])
        errors = validate_plan(plan)
        return {"valid": len(errors) == 0, "errors": errors}

    # ------------------------------------------------------------------
    # execute_plan
    # ------------------------------------------------------------------
    if action == "execute_plan":
        check_capability("fulfillment", "execute_plan")
        _LOG.info("fulfillment action=execute_plan task_id=%s", envelope.task_id)
        plan = plan_from_dict(payload["plan"])
        settings = get_settings()
        gateway = settings.gateway_urls.get("gateway", "http://samus-gateway:8080")
        result = await execute_plan(
            plan,
            gateway_url=gateway,
            hmac_key=settings.shared_hmac_key,
        )
        return plan_to_dict(result)

    # ------------------------------------------------------------------
    # ingest_result
    # ------------------------------------------------------------------
    if action == "ingest_result":
        check_capability("fulfillment", "ingest_result")
        _LOG.info("fulfillment action=ingest_result task_id=%s", envelope.task_id)
        plan = plan_from_dict(payload["plan"])
        step_id: str = payload["step_id"]
        result_status: str = payload["status"]
        output: dict | None = payload.get("output")
        newly_ready = ingest_result(plan, step_id, result_status, output)
        return {
            "plan": plan_to_dict(plan),
            "newly_ready_step_ids": [s.id for s in newly_ready],
        }

    # ------------------------------------------------------------------
    # resume_plan
    # ------------------------------------------------------------------
    if action == "resume_plan":
        check_capability("fulfillment", "resume_plan")
        _LOG.info("fulfillment action=resume_plan task_id=%s", envelope.task_id)
        plan = plan_from_dict(payload["plan"])
        return plan_to_dict(resume_plan(plan))

    # ------------------------------------------------------------------
    # finalize_plan
    # ------------------------------------------------------------------
    if action == "finalize_plan":
        check_capability("fulfillment", "finalize_plan")
        _LOG.info("fulfillment action=finalize_plan task_id=%s", envelope.task_id)
        plan = plan_from_dict(payload["plan"])
        return plan_to_dict(finalize_plan(plan))

    # ------------------------------------------------------------------
    # Unknown
    # ------------------------------------------------------------------
    _LOG.warning("fulfillment unknown action=%s task_id=%s", action, envelope.task_id)
    from fastapi import HTTPException

    raise HTTPException(status_code=400, detail=f"unknown fulfillment action: {action!r}")
