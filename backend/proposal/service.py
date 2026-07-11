"""``generate_proposal`` -- orchestrates the proposal pipeline.

OnboardingIntake -> plan_workflow -> select_templates -> compile_workflow ->
validate_workflow -> ProposalResult. In-process idempotency keyed on
``f"proposal.generate:{req.task_id}"``. Audit event appended on completion.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from backend.common import events, persistence
from backend.common.config import get_settings
from backend.common.http_client import signed_post_json_sync
from backend.common.idempotency import GLOBAL_IDEMPOTENCY_STORE

from .models import (
    CompiledWorkflow,
    PipelineStage,
    ProposalRequest,
    ProposalResult,
    ProposalValidation,
)
from .pipeline import (
    compile_workflow,
    plan_workflow,
    select_templates,
    validate_workflow,
)
from .templates import TEMPLATE_REGISTRY

_LOG = logging.getLogger("samus.proposal.service")

_AUDIT_PATH_DEFAULT = "/opt/samus/data/proposal/proposal_audit.jsonl"


def _audit_ledger() -> persistence.JsonlLedger:
    path = os.getenv("SAMUS_PROPOSAL_AUDIT_PATH", _AUDIT_PATH_DEFAULT)
    return persistence.JsonlLedger(path)


def _dispatch_artifact_to_crm(payload: dict[str, Any]) -> None:
    """Best-effort enqueue of a ``create_artifact`` job for samus-crm.

    Mirrors :func:`backend.seo.service._dispatch_artifact_to_crm`. Dispatches
    via the gateway's ``POST /dispatch/crm`` so the gateway can route to
    the ``samus-crm-jobs`` SQS queue. Sync (no daemon thread) — the
    producer's request stays alive for the duration of the call, so the
    dispatch survives Cloud Run scale-to-zero. Config gaps log + skip;
    network failures are swallowed.
    """
    settings = get_settings()
    gateway_url = settings.gateway_urls.get("gateway")
    if not gateway_url:
        _LOG.debug("proposal crm dispatch skipped: gateway_url_unset")
        return
    if not settings.shared_hmac_key:
        _LOG.debug("proposal crm dispatch skipped: shared_hmac_key_unset")
        return

    envelope = {
        "task_id": f"proposal-artifact-{payload.get('owner_entity_id') or 'unknown'}",
        "payload": payload,
        "metadata": {"action": "create_artifact"},
    }
    try:
        signed_post_json_sync(
            gateway_url, "/dispatch/crm", envelope, retries=2,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort, never blocks producer
        _LOG.warning("proposal crm artifact dispatch failed: %s", exc)


def _dispatch_proposal_pack_to_scaffold(
    req: ProposalRequest, result: ProposalResult,
) -> None:
    """Best-effort: an approved proposal -> render the client-facing pack.

    Inbound deal funnel, Unit S. Fires only for an approved (DELIVERED)
    proposal — a needs_review skeleton or an out_of_scope proposal has
    nothing worth packaging. Dispatches a ``generate_assets`` job through the
    gateway's ``POST /dispatch/scaffold`` (scaffold has an SQS sidecar, so
    rendering never blocks the proposal response). scaffold is zero-LLM /
    deterministic, so auto-firing carries no token cost. The owner linkage
    rides in ``ScaffoldRequest.inputs`` so scaffold can register the rendered
    pack back as a CRM artifact. Mirrors :func:`_dispatch_artifact_to_crm`'s
    best-effort contract — a config gap short-circuits, a transport failure
    is logged and swallowed; the proposal result has already been built.
    """
    settings = get_settings()
    gateway_url = settings.gateway_urls.get("gateway")
    if not gateway_url or not settings.shared_hmac_key:
        _LOG.debug("scaffold dispatch skipped: gateway_url / shared_hmac_key unset")
        return

    intake = req.intake
    # The compiled workflow's node descriptions are the proposal's concrete
    # build steps — they seed the proposal pack's goal list. Fall back to the
    # raw want-lists when the workflow carried no node descriptions.
    nodes = result.workflow.nodes if result.workflow else []
    goals = [n.description for n in nodes if n.description]
    if not goals:
        goals = list(intake.actions_wanted) or list(intake.triggers_wanted)

    envelope = {
        "task_id": f"scaffold-{req.task_id}",
        "payload": {
            "asset_type": "proposal_pack",
            "title": f"Proposal Pack: {intake.client_name}",
            "client": intake.client_name,
            "brand_voice": "professional and direct",
            "offer": intake.business_goal or "Automation buildout",
            "goals": goals,
            "inputs": {
                "opportunity_id": req.opportunity_id,
                "prospect_id": req.prospect_id,
                "proposal_task_id": req.task_id,
            },
        },
        "metadata": {"action": "generate_assets"},
    }
    try:
        signed_post_json_sync(
            gateway_url, "/dispatch/scaffold", envelope, retries=2,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort, never blocks producer
        _LOG.warning("scaffold proposal-pack dispatch failed: %s", exc)


def generate_proposal(req: ProposalRequest) -> ProposalResult:
    """End-to-end proposal generation."""
    cache_key = f"proposal.generate:{req.task_id}"
    cached = GLOBAL_IDEMPOTENCY_STORE.get(cache_key)
    if cached is not None and isinstance(cached, dict):
        _LOG.info("generate_proposal cache hit task_id=%s", req.task_id)
        result = ProposalResult.model_validate(cached)
        return result.model_copy(update={"cache_hit": True})

    plan = plan_workflow(req.intake)
    selected = select_templates(plan, TEMPLATE_REGISTRY)
    workflow = compile_workflow(plan, selected)
    validation = validate_workflow(workflow)

    if validation.passes:
        stage = PipelineStage.DELIVERED
        status: str = "approved"
        refund = False
    else:
        overflow_markers = (
            "workflow_steps_exceeded",
            "tools_exceeded",
            "templates_exceeded",
        )
        is_overflow = any(
            any(marker in r for marker in overflow_markers)
            for r in validation.reasons
        )
        if is_overflow:
            stage = PipelineStage.OUT_OF_SCOPE
            status = "out_of_scope"
            refund = True
        else:
            stage = PipelineStage.PENDING_INTAKE
            status = "needs_review"
            refund = False

    result = ProposalResult(
        task_id=req.task_id,
        stage=stage,
        plan=plan,
        workflow=workflow,
        validation=validation,
        refund_protocol=refund,
        status=status,  # type: ignore[arg-type]
        cache_hit=False,
    )

    audit_event = events.build_audit_event(
        service="proposal",
        task_id=req.task_id,
        action="generate_proposal",
        input_payload=req.model_dump(),
        output_payload=result.model_dump(),
        status="completed",
    )
    try:
        _audit_ledger().append(audit_event)
    except OSError as exc:
        _LOG.warning("proposal audit ledger append failed: %s", exc)

    GLOBAL_IDEMPOTENCY_STORE.set(cache_key, result.model_dump())
    _LOG.info(
        "generate_proposal complete",
        extra={"task_id": req.task_id, "status": status, "steps": workflow.total_steps},
    )

    # Phase 5 — best-effort CRM artifact registration. opportunity_id wins
    # over prospect_id (deal-level linkage is stronger than account-level).
    # Both blank -> no dispatch (proposal can run on a bare intake without
    # any CRM linkage, e.g. a sandbox/preview generation).
    if req.opportunity_id or req.prospect_id:
        if req.opportunity_id:
            owner_kind = "opportunity"
            owner_id = req.opportunity_id
        else:
            owner_kind = "prospect"
            owner_id = req.prospect_id
        _dispatch_artifact_to_crm({
            "kind": "proposal",
            "owner_entity_kind": owner_kind,
            "owner_entity_id": owner_id,
            "title": f"Proposal: {req.intake.client_name}",
            "inline_data": {
                "task_id": req.task_id,
                "stage": result.stage.value,
                "status": result.status,
                "total_steps": (
                    result.workflow.total_steps if result.workflow else 0
                ),
            },
            "source": "proposal",
            "created_by": "samus-proposal",
        })

    # Inbound deal funnel (Unit S): an approved proposal -> render the
    # client-facing proposal pack via the scaffold workcell.
    if result.status == "approved":
        _dispatch_proposal_pack_to_scaffold(req, result)

    return result


def validate_proposal(workflow: CompiledWorkflow) -> ProposalValidation:
    """Thin wrapper exposing :func:`validate_workflow`."""
    return validate_workflow(workflow)
