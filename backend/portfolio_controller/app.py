"""FastAPI application for the portfolio_controller workcell.

Request-driven by design: every rebalance is triggered by an inbound HTTP
call. No background tick-loop is wired — the contract (§5) explicitly offers
omitting the strategy ``start_tick_loop`` lifespan as the simplest acceptable
choice, and the rebalance here is cheap and LLM-free, so there is nothing a
periodic loop would buy that an event-driven call does not.

Endpoints:
  POST /work                       — TaskEnvelope wrapper, routes on
                                      metadata['action'] (cap: plan_execution)
  POST /portfolio_controller/rebalance — REST alias for the rebalance action
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request
from pydantic import ValidationError

from backend.common.app_factory import create_base_app
from backend.common.capabilities import check_capability
from backend.common.models import TaskEnvelope

from .models import RebalanceRequest
from .service import run_rebalance

_LOG = logging.getLogger("samus.portfolio_controller.app")

_SERVICE = "portfolio_controller"


def _parse_envelope(body: Any) -> TaskEnvelope:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected_json_object")
    try:
        return TaskEnvelope.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"invalid_envelope: {exc}") from exc


def _parse_rebalance(payload: Any) -> RebalanceRequest:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="expected_RebalanceRequest_object")
    try:
        return RebalanceRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc


def create_app():
    """Build and return the portfolio_controller FastAPI application."""
    app = create_base_app(service_name=_SERVICE)

    @app.post("/work")
    async def work(request: Request) -> dict[str, Any]:
        """Canonical envelope route — gateway HTTP fallback posts here."""
        body = await request.json()
        envelope = _parse_envelope(body)
        action = (envelope.metadata or {}).get("action") or "rebalance"
        if action == "rebalance":
            check_capability(_SERVICE, "plan_execution")
            req = _parse_rebalance(envelope.payload)
            # Honour an envelope-level task_id when the payload omits one.
            if not req.task_id and envelope.task_id:
                req = req.model_copy(update={"task_id": envelope.task_id})
            return run_rebalance(req).model_dump()
        raise HTTPException(status_code=400, detail=f"unknown_action: {action}")

    @app.post("/portfolio_controller/rebalance")
    async def rebalance_endpoint(request: Request) -> dict[str, Any]:
        """REST alias — rebalance token quotas + priority weights."""
        check_capability(_SERVICE, "plan_execution")
        body = await request.json()
        req = _parse_rebalance(body)
        return run_rebalance(req).model_dump()

    return app


app = create_app()
