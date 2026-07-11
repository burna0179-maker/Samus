"""Campaign Orchestrator FastAPI service.

Canonical Samus workcell (``create_base_app``): HMAC-verified inbound requests,
capability-gated handlers, correlation + metrics middleware, ``/health`` +
``/metrics``.

Dispatch surface:
  POST /work                        TaskEnvelope; routes by metadata['action']
  POST /campaigns                   create a campaign from an instance
  POST /campaigns/{id}/start        start + drive the run
  POST /campaigns/{id}/advance      drive an already-running run
  POST /campaigns/{id}/approve      decide an approval gate, then resume
  POST /campaigns/{id}/kpi          ingest KPI events

Read surface (deliverable §6):
  GET  /campaigns/{id}/status
  GET  /campaigns/{id}/timeline
  GET  /campaigns/{id}/artifacts
  GET  /campaigns/{id}/metrics
  GET  /campaigns/{id}/audit
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request

from backend.common.app_factory import create_base_app
from backend.common.capabilities import check_capability
from backend.common.models import TaskEnvelope

from .kpi import INITIAL_KPIS
from .models import CampaignInstance
from .orchestrator import CampaignError, CampaignOrchestrator, default_orchestrator
from .templates import TemplateError

_LOG = logging.getLogger("samus.campaigns.app")

# Capability names map 1:1 to the ``campaigns`` entry in SERVICE_CAPABILITIES.
_ACTION_CAPABILITY = {
    "create_campaign": "create_campaign",
    "start_campaign": "start_campaign",
    "advance_campaign": "advance_campaign",
    "approve_node": "approve_node",
    "update_kpis": "update_kpis",
    "ingest_kpi": "ingest_kpi",
    "generate_report": "generate_report",
    "read_status": "read_status",
}


def _orch(request: Request) -> CampaignOrchestrator:
    """Allow tests to inject an orchestrator via app.state; else the default."""
    injected = getattr(request.app.state, "orchestrator", None)
    return injected if injected is not None else default_orchestrator()


def _run_or_404(orch: CampaignOrchestrator, campaign_id: str):
    run = orch.get_run(campaign_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown_campaign: {campaign_id}")
    return run


def create_app():
    app = create_base_app(service_name="campaigns")

    # ---- dispatch surface --------------------------------------------

    @app.post("/work")
    async def work(request: Request) -> dict[str, Any]:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="expected_json_object")
        try:
            envelope = TaskEnvelope.model_validate(body)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid_envelope: {exc}")
        action = (envelope.metadata or {}).get("action") or ""
        capability = _ACTION_CAPABILITY.get(action)
        if capability is None:
            raise HTTPException(status_code=400, detail=f"unknown_action: {action}")
        check_capability("campaigns", capability)
        return _handle_action(_orch(request), action, envelope.payload or {})

    @app.post("/campaigns")
    async def create_campaign(request: Request) -> dict[str, Any]:
        check_capability("campaigns", "create_campaign")
        body = await request.json()
        payload = body if isinstance(body, dict) else {}
        return _handle_action(_orch(request), "create_campaign", payload)

    @app.post("/campaigns/{campaign_id}/start")
    async def start_campaign(campaign_id: str, request: Request) -> dict[str, Any]:
        check_capability("campaigns", "start_campaign")
        orch = _orch(request)
        try:
            return orch.start(campaign_id).model_dump()
        except CampaignError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.post("/campaigns/{campaign_id}/advance")
    async def advance_campaign(campaign_id: str, request: Request) -> dict[str, Any]:
        check_capability("campaigns", "advance_campaign")
        orch = _orch(request)
        try:
            return orch.advance(campaign_id).model_dump()
        except CampaignError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.post("/campaigns/{campaign_id}/approve")
    async def approve_node(campaign_id: str, request: Request) -> dict[str, Any]:
        check_capability("campaigns", "approve_node")
        body = await request.json()
        body = body if isinstance(body, dict) else {}
        orch = _orch(request)
        try:
            run = orch.approve_node(
                campaign_id,
                node_id=body.get("node_id"),
                approval_id=body.get("approval_id"),
                decision=body.get("decision", "approved"),
                decided_by=body.get("decided_by", "operator"),
            )
        except CampaignError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return run.model_dump()

    @app.post("/campaigns/{campaign_id}/kpi")
    async def ingest_kpi(campaign_id: str, request: Request) -> dict[str, Any]:
        check_capability("campaigns", "ingest_kpi")
        body = await request.json()
        body = body if isinstance(body, dict) else {}
        payload = {"campaign_id": campaign_id, "events": body.get("events", [])}
        return _handle_action(_orch(request), "ingest_kpi", payload)

    # ---- read surface -------------------------------------------------

    @app.get("/campaigns/{campaign_id}/status")
    async def status(campaign_id: str, request: Request) -> dict[str, Any]:
        check_capability("campaigns", "read_status")
        run = _run_or_404(_orch(request), campaign_id)
        return {
            "campaign_id": run.campaign_id,
            "client_id": run.client_id,
            "template_id": run.template_id,
            "vertical": run.vertical,
            "state": run.state.value,
            "current_node": run.current_node,
            "completed_nodes": run.completed_nodes,
            "blocked_nodes": run.blocked_nodes,
            "failed_nodes": run.failed_nodes,
            "needs_approval_nodes": run.needs_approval_nodes,
            "approvals_required": run.approvals_required,
            "kpis_updated": run.kpis_updated,
            "artifact_count": len(run.artifacts_created),
            "audit_event_count": len(run.audit_events),
            "created_at": run.created_at,
            "updated_at": run.updated_at,
        }

    @app.get("/campaigns/{campaign_id}/timeline")
    async def timeline(campaign_id: str, request: Request) -> dict[str, Any]:
        check_capability("campaigns", "read_status")
        orch = _orch(request)
        _run_or_404(orch, campaign_id)
        return {"campaign_id": campaign_id, "timeline": orch.timeline(campaign_id)}

    @app.get("/campaigns/{campaign_id}/artifacts")
    async def artifacts(campaign_id: str, request: Request) -> dict[str, Any]:
        check_capability("campaigns", "read_status")
        run = _run_or_404(_orch(request), campaign_id)
        return {
            "campaign_id": campaign_id,
            "artifacts": [a.model_dump() for a in run.artifacts_created],
        }

    @app.get("/campaigns/{campaign_id}/metrics")
    async def campaign_metrics(campaign_id: str, request: Request) -> dict[str, Any]:
        check_capability("campaigns", "read_status")
        run = _run_or_404(_orch(request), campaign_id)
        kpis = [
            {
                "kpi": key,
                "label": INITIAL_KPIS[key].label if key in INITIAL_KPIS else key,
                "value": value,
                "target": run.context.get("kpi_targets", {}).get(key),
            }
            for key, value in sorted(run.kpis_updated.items())
        ]
        return {
            "campaign_id": campaign_id,
            "client_id": run.client_id,
            "template_id": run.template_id,
            "state": run.state.value,
            "kpis": kpis,
            "counts": {
                "completed": len(run.completed_nodes),
                "blocked": len(run.blocked_nodes),
                "failed": len(run.failed_nodes),
                "needs_approval": len(run.needs_approval_nodes),
                "artifacts": len(run.artifacts_created),
            },
        }

    @app.get("/campaigns/{campaign_id}/audit")
    async def audit(campaign_id: str, request: Request) -> dict[str, Any]:
        check_capability("campaigns", "read_status")
        orch = _orch(request)
        _run_or_404(orch, campaign_id)
        events = orch.timeline(campaign_id)
        return {
            "campaign_id": campaign_id,
            "chain_intact": orch.ledger.verify(),
            "events": events,
        }

    return app


def _handle_action(
    orch: CampaignOrchestrator, action: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Shared action handler used by /work and the REST convenience routes."""
    try:
        if action == "create_campaign":
            instance = CampaignInstance.model_validate(payload.get("instance", payload))
            return orch.create_campaign(instance).model_dump()
        if action == "start_campaign":
            return orch.start(payload["campaign_id"]).model_dump()
        if action == "advance_campaign":
            return orch.advance(payload["campaign_id"]).model_dump()
        if action == "approve_node":
            return orch.approve_node(
                payload["campaign_id"],
                node_id=payload.get("node_id"),
                approval_id=payload.get("approval_id"),
                decision=payload.get("decision", "approved"),
                decided_by=payload.get("decided_by", "operator"),
            ).model_dump()
        if action in ("ingest_kpi", "update_kpis"):
            run = orch.get_run(payload.get("campaign_id", ""))
            if run is None:
                raise HTTPException(status_code=404, detail="unknown_campaign")
            from .kpi import apply_kpi_events

            changed = apply_kpi_events(run, payload.get("events", []))
            orch._store.save(run)  # noqa: SLF001 — orchestrator-owned store
            orch._refresh_approval_gauge(run)  # noqa: SLF001
            return {"campaign_id": run.campaign_id, "changed": changed}
        if action == "generate_report":
            run = orch.get_run(payload.get("campaign_id", ""))
            if run is None:
                raise HTTPException(status_code=404, detail="unknown_campaign")
            from .report import generate_weekly_report

            artifact = generate_weekly_report(run, orch.ledger)
            run.artifacts_created.append(artifact)
            orch._store.save(run)  # noqa: SLF001
            return {"campaign_id": run.campaign_id, "artifact": artifact.model_dump()}
        raise HTTPException(status_code=400, detail=f"unhandled_action: {action}")
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"missing_field: {exc}")
    except (CampaignError, TemplateError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


app = create_app()
