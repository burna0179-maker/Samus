"""Proposal workcell FastAPI service.

Endpoints:
  POST /work     - TaskEnvelope wrapper (gateway dispatch path)
  POST /generate - direct ProposalRequest body
  POST /validate - direct CompiledWorkflow body
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Depends, HTTPException, Request

from backend.common.app_factory import create_base_app
from backend.common.capabilities import check_capability
from backend.common.models import TaskEnvelope
from backend.common.rate_limit import rate_limit_dependency

from .models import CompiledWorkflow, ProposalRequest
from .service import generate_proposal, validate_proposal

_LOG = logging.getLogger("samus.proposal.app")


def _parse_envelope(body: Any) -> TaskEnvelope:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected_json_object")
    try:
        return TaskEnvelope.model_validate(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid_envelope: {exc}")


def _parse_proposal_req(payload: Any) -> ProposalRequest:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="expected_proposal_request_object")
    try:
        return ProposalRequest.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid_proposal_request: {exc}")


def _parse_workflow(payload: Any) -> CompiledWorkflow:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="expected_workflow_object")
    try:
        return CompiledWorkflow.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid_workflow: {exc}")


def create_app():
    app = create_base_app(service_name="proposal")

    @app.post("/work")
    async def work(request: Request) -> dict[str, Any]:
        body = await request.json()
        envelope = _parse_envelope(body)
        action = (envelope.metadata or {}).get("action") or "generate_proposal"
        if action == "generate_proposal":
            check_capability("proposal", "generate_proposal")
            req = _parse_proposal_req(envelope.payload)
            result = generate_proposal(req.model_copy(update={"task_id": envelope.task_id}))
            return result.model_dump()
        if action == "validate_proposal":
            check_capability("proposal", "validate_proposal")
            workflow = _parse_workflow(envelope.payload)
            return validate_proposal(workflow).model_dump()
        raise HTTPException(status_code=400, detail=f"unknown_action: {action}")

    # M7 — /generate runs LLM-backed proposal generation. A per-caller rate
    # limit caps a runaway loop / a compromised caller before it can burn the
    # Anthropic budget. Env-tunable via
    # SAMUS_RATE_LIMIT_PROPOSAL_GENERATE_PER_MINUTE.
    @app.post(
        "/generate",
        dependencies=[Depends(rate_limit_dependency("proposal_generate"))],
    )
    async def generate(request: Request) -> dict[str, Any]:
        check_capability("proposal", "generate_proposal")
        body = await request.json()
        req = _parse_proposal_req(body)
        return generate_proposal(req).model_dump()

    @app.post("/validate")
    async def validate(request: Request) -> dict[str, Any]:
        check_capability("proposal", "validate_proposal")
        body = await request.json()
        workflow = _parse_workflow(body)
        return validate_proposal(workflow).model_dump()

    return app


app = create_app()
