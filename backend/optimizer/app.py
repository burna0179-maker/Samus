"""Optimizer workcell FastAPI service."""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from backend.common.app_factory import create_base_app
from backend.common.capabilities import check_capability
from backend.common.models import TaskEnvelope

from .models import (
    OptimizePortfolioRequest,
    SelectArmRequest,
    UpdateArmRequest,
)
from .service import optimize_portfolio, select_arm, update_arm


def _parse(model_cls, payload: Any):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=f"expected_{model_cls.__name__}_object")
    try:
        return model_cls.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid_{model_cls.__name__}: {exc}")


def create_app():
    app = create_base_app(service_name="optimizer")

    @app.post("/work")
    async def work(request: Request) -> dict[str, Any]:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="expected_json_object")
        try:
            envelope = TaskEnvelope.model_validate(body)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid_envelope: {exc}")
        action = (envelope.metadata or {}).get("action") or "select_arm"
        if action == "select_arm":
            check_capability("optimizer", "select_arm")
            return select_arm(_parse(SelectArmRequest, envelope.payload)).model_dump()
        if action == "update_arm":
            check_capability("optimizer", "update_arm")
            return update_arm(_parse(UpdateArmRequest, envelope.payload)).model_dump()
        if action == "optimize_portfolio":
            check_capability("optimizer", "optimize_portfolio")
            return optimize_portfolio(_parse(OptimizePortfolioRequest, envelope.payload)).model_dump()
        raise HTTPException(status_code=400, detail=f"unknown_action: {action}")

    @app.post("/select_arm")
    async def select_endpoint(request: Request) -> dict[str, Any]:
        check_capability("optimizer", "select_arm")
        body = await request.json()
        return select_arm(_parse(SelectArmRequest, body)).model_dump()

    @app.post("/update_arm")
    async def update_endpoint(request: Request) -> dict[str, Any]:
        check_capability("optimizer", "update_arm")
        body = await request.json()
        return update_arm(_parse(UpdateArmRequest, body)).model_dump()

    @app.post("/optimize_portfolio")
    async def optimize_endpoint(request: Request) -> dict[str, Any]:
        check_capability("optimizer", "optimize_portfolio")
        body = await request.json()
        return optimize_portfolio(_parse(OptimizePortfolioRequest, body)).model_dump()

    return app


app = create_app()
