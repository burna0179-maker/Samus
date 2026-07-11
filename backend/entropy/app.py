"""Entropy workcell FastAPI service.

Endpoints:
  POST /work          - TaskEnvelope wrapper, routes by metadata['action']
  POST /entropy/scan  - EntropyScanRequest REST alias  (cap: plan_execution)

Service name: ``entropy``. Capability: ``plan_execution``. Zero LLM calls.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request
from pydantic import ValidationError

from backend.common.app_factory import create_base_app
from backend.common.capabilities import check_capability
from backend.common.models import TaskEnvelope

from .diagnostics import run_diagnostics
from .models import EntropyScanRequest
from .service import scan

_LOG = logging.getLogger("samus.entropy.app")

_SERVICE = "entropy"
_CAPABILITY = "plan_execution"


def _parse_envelope(body: Any) -> TaskEnvelope:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected_json_object")
    try:
        return TaskEnvelope.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"invalid_envelope: {exc}") from exc


def _parse_request(payload: Any) -> EntropyScanRequest:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="expected_EntropyScanRequest_object")
    try:
        return EntropyScanRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc


def create_app():
    """Build and return the entropy FastAPI application."""
    app = create_base_app(service_name=_SERVICE)

    @app.post("/work")
    async def work(request: Request) -> dict[str, Any]:
        """Canonical envelope route — routes on ``metadata['action']``."""
        body = await request.json()
        envelope = _parse_envelope(body)
        action = (envelope.metadata or {}).get("action") or "entropy_scan"
        if action in ("entropy_scan", "scan"):
            check_capability(_SERVICE, _CAPABILITY)
            req = _parse_request(envelope.payload)
            return scan(envelope.task_id, req).model_dump()
        raise HTTPException(status_code=400, detail=f"unknown_action: {action}")

    @app.post("/entropy/scan")
    async def entropy_scan(request: Request) -> dict[str, Any]:
        """REST alias for an entropy scan. Capability: ``plan_execution``."""
        check_capability(_SERVICE, _CAPABILITY)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="expected_json_object")
        task_id = body.get("task_id")
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else body
        if not isinstance(task_id, str) or not task_id:
            # Allow flat bodies that omit a wrapper; default to a synthetic id.
            task_id = task_id if isinstance(task_id, str) and task_id else "entropy-scan"
        req = _parse_request(payload)
        return scan(task_id, req).model_dump()

    @app.post("/entropy/diagnostics")
    async def diagnostics(request: Request) -> dict[str, Any]:
        """Run the four self-healing detectors + safe auto-remediation.

        HOTL Tranche 5: stuck-loop, dead-worker, orphan-task, resource
        exhaustion. Each finding emits a decision.made diagnostic event; safe
        remediation (orphan requeue, dead-worker operator task + restart
        request) runs unless ``{"remediate": false}`` is posted. Capability:
        ``plan_execution``. Body optional. Never 500s (detectors are fail-soft).
        """
        check_capability(_SERVICE, _CAPABILITY)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — empty/non-JSON body -> defaults
            body = {}
        remediate = True
        if isinstance(body, dict) and "remediate" in body:
            remediate = bool(body.get("remediate"))
        return run_diagnostics(remediate=remediate)

    return app


app = create_app()
