"""FastAPI application for the path-optimizer workcell."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from pydantic import ValidationError

from backend.common.app_factory import create_base_app
from backend.common.capabilities import check_capability
from backend.common.models import TaskEnvelope

from .models import SelectPathRequest, SelectPathResponse
from .service import select_path

_LOG = logging.getLogger("samus.path_optimizer.app")

_SERVICE = "path_optimizer"
_CAPABILITY = "plan_execution"
_DEFAULT_ACTION = "select_path"


def create_app() -> object:
    """Build and return the path-optimizer FastAPI application."""
    _app = create_base_app(service_name=_SERVICE)

    @_app.post("/path_optimizer/select", response_model=SelectPathResponse)
    async def select_endpoint(req: SelectPathRequest) -> SelectPathResponse:
        """Select an execution route for a workcell. Capability: ``plan_execution``."""
        check_capability(_SERVICE, _CAPABILITY)
        return select_path(req)

    @_app.post("/work")
    async def work_endpoint(envelope: TaskEnvelope) -> dict:
        """Canonical envelope dispatcher.

        Supported action: ``select_path`` (also the default when none given).
        """
        action = (
            envelope.metadata.get("action") or envelope.payload.get("action") or _DEFAULT_ACTION
        )
        payload = dict(envelope.payload or {})
        payload.pop("action", None)

        if action == _DEFAULT_ACTION:
            check_capability(_SERVICE, _CAPABILITY)
            try:
                req = SelectPathRequest(**payload)
            except ValidationError as exc:
                raise HTTPException(status_code=422, detail=exc.errors()) from exc
            result = select_path(req, task_id=envelope.task_id)
            return result.model_dump()

        raise HTTPException(status_code=400, detail=f"unknown action: {action!r}")

    return _app


app = create_app()
