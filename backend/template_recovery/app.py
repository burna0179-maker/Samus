"""Template-recovery workcell FastAPI service.

Endpoints:
  POST /work                      - TaskEnvelope wrapper, routes by metadata['action']
  POST /template_recovery/recover - RecoveryRequest REST alias (cap: plan_execution)

The whole workcell is deterministic and consumes ZERO LLM calls — no LLM
client is imported anywhere in this package.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request
from pydantic import ValidationError

from backend.common.app_factory import create_base_app
from backend.common.capabilities import check_capability
from backend.common.models import TaskEnvelope

from .models import RecoveryRequest
from .service import recover

_LOG = logging.getLogger("samus.template_recovery.app")

_SERVICE = "template_recovery"
_CAPABILITY = "plan_execution"
_DEFAULT_ACTION = "recover"


def _parse_envelope(body: Any) -> TaskEnvelope:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected_json_object")
    try:
        return TaskEnvelope.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"invalid_envelope: {exc}") from exc


def _parse_request(payload: Any) -> RecoveryRequest:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="expected_RecoveryRequest_object")
    try:
        return RecoveryRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc


def create_app():
    """Build the template-recovery FastAPI app."""
    app = create_base_app(service_name=_SERVICE)

    @app.post("/work")
    async def work(request: Request) -> dict[str, Any]:
        """Canonical envelope route — routes on ``metadata['action']``."""
        body = await request.json()
        envelope = _parse_envelope(body)
        action = (envelope.metadata or {}).get("action") or _DEFAULT_ACTION
        if action == "recover":
            check_capability(_SERVICE, _CAPABILITY)
            req = _parse_request(envelope.payload)
            return recover(req, task_id=envelope.task_id).model_dump()
        raise HTTPException(status_code=400, detail=f"unknown_action: {action}")

    @app.post("/template_recovery/recover")
    async def recover_endpoint(request: Request) -> dict[str, Any]:
        """REST alias for deterministic recovery (cap: ``plan_execution``)."""
        check_capability(_SERVICE, _CAPABILITY)
        body = await request.json()
        req = _parse_request(body)
        return recover(req).model_dump()

    return app


app = create_app()
