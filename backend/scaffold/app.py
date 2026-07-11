"""HTTP surface for the scaffold workcell (doc §6)."""
from __future__ import annotations

import logging

from fastapi import HTTPException
from pydantic import ValidationError

from backend.common.app_factory import create_base_app
from backend.common.capabilities import check_capability
from backend.common.models import TaskEnvelope

from .logic import generate_scaffold
from .models import ScaffoldRequest

_LOG = logging.getLogger("samus.scaffold.app")

app = create_base_app(service_name="scaffold")


@app.post("/work")
async def work_endpoint(envelope: TaskEnvelope) -> dict:
    """Capability: ``generate_assets``."""
    check_capability("scaffold", "generate_assets")
    try:
        req = ScaffoldRequest(**(envelope.payload or {}))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    return generate_scaffold(req)
