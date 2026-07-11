"""signal_filter workcell FastAPI service.

Endpoints:
  POST /work               - TaskEnvelope wrapper, routes by metadata['action']
  POST /signal_filter/evaluate - ProspectInput directly (cap: plan_execution)

The workcell is a deterministic pre-qualification gate — zero LLM calls.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request

from backend.common.app_factory import create_base_app
from backend.common.capabilities import check_capability
from backend.common.models import TaskEnvelope

from .models import ProspectInput
from .service import evaluate_prospect

_LOG = logging.getLogger("samus.signal_filter.app")

_SERVICE = "signal_filter"


def _parse_envelope(body: Any) -> TaskEnvelope:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected_json_object")
    try:
        return TaskEnvelope.model_validate(body)
    except Exception as exc:  # noqa: BLE001 - surface a 400, not a 500
        raise HTTPException(status_code=400, detail=f"invalid_envelope: {exc}")


def _parse_prospect(payload: Any) -> ProspectInput:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="expected_ProspectInput_object")
    try:
        return ProspectInput.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - 422 on a malformed prospect
        raise HTTPException(status_code=422, detail=f"invalid_ProspectInput: {exc}")


def create_app():
    app = create_base_app(service_name=_SERVICE)

    @app.post("/work")
    async def work(request: Request) -> dict[str, Any]:
        body = await request.json()
        envelope = _parse_envelope(body)
        action = (envelope.metadata or {}).get("action") or "evaluate"
        if action in ("evaluate", "plan_execution"):
            check_capability(_SERVICE, "plan_execution")
            prospect = _parse_prospect(envelope.payload)
            return evaluate_prospect(prospect).model_dump()
        raise HTTPException(status_code=400, detail=f"unknown_action: {action}")

    @app.post("/signal_filter/evaluate")
    async def evaluate(request: Request) -> dict[str, Any]:
        check_capability(_SERVICE, "plan_execution")
        body = await request.json()
        prospect = _parse_prospect(body)
        return evaluate_prospect(prospect).model_dump()

    return app


app = create_app()
