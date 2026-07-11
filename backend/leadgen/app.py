"""HTTP surface for the leadgen workcell (doc §5)."""
from __future__ import annotations

import logging

from fastapi import HTTPException
from pydantic import ValidationError

from backend.common.app_factory import create_base_app
from backend.common.capabilities import check_capability
from backend.common.models import TaskEnvelope

from .models import LeadRequest, LeadScore
from .service import process_lead

_LOG = logging.getLogger("samus.leadgen.app")

app = create_base_app(service_name="leadgen")


@app.post("/score", response_model=LeadScore)
async def score_endpoint(req: LeadRequest) -> LeadScore:
    """Score a lead directly. Capability: ``score``."""
    check_capability("leadgen", "score")
    return process_lead(req)


@app.post("/work", response_model=LeadScore)
async def work_endpoint(envelope: TaskEnvelope) -> LeadScore:
    """SQS-style envelope entry. Capability: ``qualify``."""
    check_capability("leadgen", "qualify")
    try:
        req = LeadRequest(**(envelope.payload or {}))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    return process_lead(req, task_id=envelope.task_id)
