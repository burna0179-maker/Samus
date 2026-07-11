"""CRM workcell FastAPI service.

Read endpoints for all 7 entities + lead conversion + opportunity FSM + operator
tasks + artifacts + in-memory feedback engine. HMAC-signed (inherits from
create_base_app). No CORS — never called directly from a browser; other workcells
dispatch via signed_post_json.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request

from backend.common.app_factory import create_base_app
from backend.common.capabilities import check_capability
from backend.common.models import TaskEnvelope

from . import feedback_engine
from .models import (
    AdvanceOpportunityBody,
    AdvanceOpportunityRequest,
    AdvanceOpportunityResult,
    Artifact,
    ArtifactList,
    CallState,
    Contact,
    ContactList,
    Conversation,
    ConversationList,
    ConvertLeadRequest,
    ConvertLeadResult,
    CreateArtifactRequest,
    CreateArtifactResult,
    CreateOperatorTaskRequest,
    CreateOperatorTaskResult,
    CreateOpportunityRequest,
    CreateOpportunityResult,
    FeedbackLogRequest,
    OperatorTask,
    OperatorTaskList,
    Opportunity,
    OpportunityList,
    Prospect,
    UpdateOperatorTaskBody,
    UpdateOperatorTaskRequest,
    UpdateOperatorTaskResult,
    UpsertCallStateBody,
    UpsertResult,
)
from .service import (
    advance_opportunity,
    auto_create_opportunity_from_deal_scoring,
    auto_create_opportunity_from_lead,
    close_opportunity_from_payment,
    convert_lead_to_prospect,
    create_artifact,
    create_operator_task,
    create_opportunity,
    daily_stats,
    estimated_close_probability,
    find_opportunity_for_email,
    get_artifact,
    get_call_state,
    get_contact,
    get_conversation,
    get_funnel_snapshot,
    get_operator_task,
    get_opportunity,
    get_prospect,
    list_artifacts,
    list_contacts,
    list_conversations,
    list_operator_tasks,
    list_opportunities,
    log_feedback,
    token_cost_by_industry,
    update_operator_task,
    upsert_call_state,
    upsert_conversation,
)


_LOG = logging.getLogger("samus.crm.app")


def _parse(model_cls, payload: Any):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=f"expected_{model_cls.__name__}_object")
    try:
        return model_cls.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid_{model_cls.__name__}: {exc}")


def _require(entity: Any, kind: str, id_: str):
    if entity is None:
        raise HTTPException(status_code=404, detail=f"{kind}_not_found: {id_}")
    return entity


def create_app():
    app = create_base_app(service_name="crm")

    # --- Prospects -----------------------------------------------------

    @app.get("/crm/prospects/{prospect_id}", response_model=Prospect)
    async def get_prospect_route(prospect_id: str) -> Prospect:
        check_capability("crm", "read_prospects")
        return _require(get_prospect(prospect_id), "prospect", prospect_id)

    # --- Contacts ------------------------------------------------------

    @app.get("/crm/contacts/{contact_id}", response_model=Contact)
    async def get_contact_route(contact_id: str) -> Contact:
        check_capability("crm", "read_contacts")
        return _require(get_contact(contact_id), "contact", contact_id)

    @app.get("/crm/contacts", response_model=ContactList)
    async def list_contacts_route(
        prospect_id: str | None = None,
        limit: int = 50,
    ) -> ContactList:
        check_capability("crm", "read_contacts")
        return list_contacts(prospect_id=prospect_id, limit=limit)

    # --- Conversations -------------------------------------------------

    @app.get("/crm/conversations/{conversation_id}", response_model=Conversation)
    async def get_conversation_route(conversation_id: str) -> Conversation:
        check_capability("crm", "read_conversations")
        return _require(
            get_conversation(conversation_id),
            "conversation",
            conversation_id,
        )

    @app.get("/crm/conversations", response_model=ConversationList)
    async def list_conversations_route(
        prospect_id: str | None = None,
        limit: int = 50,
    ) -> ConversationList:
        check_capability("crm", "read_conversations")
        return list_conversations(prospect_id=prospect_id, limit=limit)

    @app.post("/crm/conversations", response_model=UpsertResult)
    async def upsert_conversation_route(request: Request) -> UpsertResult:
        """Phase 2 — voice (and other workcells) write Conversation rows here.

        Idempotent: same conversation_id overwrites. Returns persisted=False
        with the ddb error in ``error`` when DDB is degraded — caller decides
        whether to retry; the audit ledger captures every attempt.
        """
        check_capability("crm", "write_conversation")
        body = await request.json()
        conv = _parse(Conversation, body)
        if not conv.conversation_id:
            raise HTTPException(
                status_code=422,
                detail="invalid_Conversation: conversation_id required",
            )
        ok = upsert_conversation(conv)
        return UpsertResult(
            persisted=ok,
            id=conv.conversation_id,
            error=None if ok else "ddb_put_failed",
        )

    # --- CallState -----------------------------------------------------
    # PK is prospect_id (one current state per prospect, not per attempt).
    # Path uses prospect-id so the operator can hit it without remembering
    # a separate call-state id.

    @app.get("/crm/call-state/{prospect_id}", response_model=CallState)
    async def get_call_state_route(prospect_id: str) -> CallState:
        check_capability("crm", "read_call_state")
        return _require(get_call_state(prospect_id), "call_state", prospect_id)

    @app.post("/crm/call-state/{prospect_id}", response_model=UpsertResult)
    async def upsert_call_state_route(
        prospect_id: str,
        request: Request,
    ) -> UpsertResult:
        """Phase 2 — refresh the per-prospect dialer FSM row.

        Body matches ``UpsertCallStateBody`` (a path-id-free input shape);
        ``prospect_id`` is taken from the URL path — it is canonical and
        cannot be overridden via the body. The full ``CallState`` row is
        constructed server-side. PK collision = clean overwrite.
        """
        check_capability("crm", "write_call_state")
        body = await request.json()
        # Bind only the explicitly-intended fields (M3 — no raw dict spread
        # onto the domain model), then force the path id server-side.
        input_body = _parse(UpsertCallStateBody, body)
        state = CallState(prospect_id=prospect_id, **input_body.model_dump())
        ok = upsert_call_state(state)
        return UpsertResult(
            persisted=ok,
            id=state.prospect_id,
            error=None if ok else "ddb_put_failed",
        )

    # --- Opportunities -------------------------------------------------

    @app.get("/crm/opportunities/{opportunity_id}", response_model=Opportunity)
    async def get_opportunity_route(opportunity_id: str) -> Opportunity:
        check_capability("crm", "read_opportunities")
        return _require(
            get_opportunity(opportunity_id),
            "opportunity",
            opportunity_id,
        )

    @app.get("/crm/opportunities", response_model=OpportunityList)
    async def list_opportunities_route(
        stage: str | None = None,
        limit: int = 50,
    ) -> OpportunityList:
        check_capability("crm", "read_opportunities")
        return list_opportunities(stage=stage, limit=limit)

    @app.post("/crm/opportunities", response_model=CreateOpportunityResult)
    async def create_opportunity_route(request: Request) -> CreateOpportunityResult:
        check_capability("crm", "write_opportunity")
        body = await request.json()
        req = _parse(CreateOpportunityRequest, body)
        return create_opportunity(req)

    @app.post(
        "/crm/opportunities/{opportunity_id}/advance",
        response_model=AdvanceOpportunityResult,
    )
    async def advance_opportunity_route(
        opportunity_id: str,
        request: Request,
    ) -> AdvanceOpportunityResult:
        check_capability("crm", "advance_opportunity")
        body = await request.json()
        # M3 — bind the body to the narrow, path-id-free input model, then
        # construct the full domain request server-side with opportunity_id
        # forced from the URL path. A caller cannot smuggle a different
        # opportunity_id (or any other field) onto the domain model.
        input_body = _parse(AdvanceOpportunityBody, body)
        req = AdvanceOpportunityRequest(
            opportunity_id=opportunity_id,
            **input_body.model_dump(),
        )
        return advance_opportunity(req)

    # --- Operator tasks ------------------------------------------------

    @app.get("/crm/operator-tasks/{operator_task_id}", response_model=OperatorTask)
    async def get_operator_task_route(operator_task_id: str) -> OperatorTask:
        check_capability("crm", "read_tasks")
        return _require(
            get_operator_task(operator_task_id),
            "operator_task",
            operator_task_id,
        )

    @app.get("/crm/operator-tasks", response_model=OperatorTaskList)
    async def list_operator_tasks_route(
        status: str | None = "open",
        limit: int = 50,
    ) -> OperatorTaskList:
        check_capability("crm", "read_tasks")
        return list_operator_tasks(status=status, limit=limit)

    @app.post("/crm/operator-tasks", response_model=CreateOperatorTaskResult)
    async def create_operator_task_route(
        request: Request,
    ) -> CreateOperatorTaskResult:
        check_capability("crm", "write_task")
        body = await request.json()
        req = _parse(CreateOperatorTaskRequest, body)
        return create_operator_task(req)

    @app.put(
        "/crm/operator-tasks/{operator_task_id}",
        response_model=UpdateOperatorTaskResult,
    )
    async def update_operator_task_route(
        operator_task_id: str,
        request: Request,
    ) -> UpdateOperatorTaskResult:
        check_capability("crm", "update_task")
        body = await request.json()
        # M3 — bind the body to the narrow, path-id-free input model, then
        # construct the full domain request server-side with operator_task_id
        # forced from the URL path.
        input_body = _parse(UpdateOperatorTaskBody, body)
        req = UpdateOperatorTaskRequest(
            operator_task_id=operator_task_id,
            **input_body.model_dump(),
        )
        return update_operator_task(req)

    # --- Artifacts -----------------------------------------------------

    @app.get("/crm/artifacts/{artifact_id}", response_model=Artifact)
    async def get_artifact_route(artifact_id: str) -> Artifact:
        check_capability("crm", "read_artifacts")
        return _require(get_artifact(artifact_id), "artifact", artifact_id)

    @app.get("/crm/artifacts", response_model=ArtifactList)
    async def list_artifacts_route(
        owner_entity_id: str | None = None,
        limit: int = 50,
    ) -> ArtifactList:
        check_capability("crm", "read_artifacts")
        return list_artifacts(owner_entity_id=owner_entity_id, limit=limit)

    @app.post("/crm/artifacts", response_model=CreateArtifactResult)
    async def create_artifact_route(request: Request) -> CreateArtifactResult:
        """Phase 5 — producer workcells register a deliverable here.

        Called by seo/proposal/fulfillment via signed_post_json after a
        deliverable is produced. Returns ``status="failed"`` (200) with
        ``error`` set when DDB is degraded — callers treat this as a
        best-effort dispatch and never fail the producer pipeline on a
        CRM hiccup.
        """
        check_capability("crm", "write_artifact")
        body = await request.json()
        req = _parse(CreateArtifactRequest, body)
        return create_artifact(req)

    # --- Internal: find opportunity by email (Phase 5 close-the-loop) --
    # Used by the finance Stripe webhook to attribute an incoming payment
    # back to an open opportunity. Returns the most-recent open opportunity
    # id (or null) so the caller can then POST an advance to closed_won.
    # No PII echoed back — only the opportunity_id (or null) is returned.

    @app.get("/crm/_find_opportunity_for_email")
    async def find_opportunity_for_email_route(email: str) -> dict[str, Any]:
        check_capability("crm", "find_opportunity_for_email")
        opportunity_id = find_opportunity_for_email(email)
        return {"opportunity_id": opportunity_id}

    # --- Mutation: lead -> Prospect + Contact --------------------------

    @app.post("/crm/convert/lead", response_model=ConvertLeadResult)
    async def convert_lead_route(request: Request) -> ConvertLeadResult:
        check_capability("crm", "convert_lead")
        body = await request.json()
        req = _parse(ConvertLeadRequest, body)
        return convert_lead_to_prospect(req)

    # --- TaskEnvelope route (gateway / future SQS parity) --------------

    @app.post("/work")
    async def work(request: Request) -> dict[str, Any]:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="expected_json_object")
        try:
            envelope = TaskEnvelope.model_validate(body)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid_envelope: {exc}")
        action = (envelope.metadata or {}).get("action") or "convert_lead"
        payload = envelope.payload if isinstance(envelope.payload, dict) else {}
        if action == "convert_lead":
            check_capability("crm", "convert_lead")
            req = ConvertLeadRequest.model_validate(payload)
            return convert_lead_to_prospect(req).model_dump()
        if action == "create_opportunity":
            check_capability("crm", "write_opportunity")
            req = CreateOpportunityRequest.model_validate(payload)
            return create_opportunity(req).model_dump()
        if action == "advance_opportunity":
            check_capability("crm", "advance_opportunity")
            req = AdvanceOpportunityRequest.model_validate(payload)
            return advance_opportunity(req).model_dump()
        if action == "upsert_conversation":
            check_capability("crm", "write_conversation")
            conv = Conversation.model_validate(payload)
            ok = upsert_conversation(conv)
            return UpsertResult(
                persisted=ok,
                id=conv.conversation_id,
                error=None if ok else "ddb_put_failed",
            ).model_dump()
        if action == "upsert_call_state":
            check_capability("crm", "write_call_state")
            state = CallState.model_validate(payload)
            ok = upsert_call_state(state)
            return UpsertResult(
                persisted=ok,
                id=state.prospect_id,
                error=None if ok else "ddb_put_failed",
            ).model_dump()
        if action == "create_task":
            check_capability("crm", "write_task")
            req = CreateOperatorTaskRequest.model_validate(payload)
            return create_operator_task(req).model_dump()
        if action == "update_task":
            check_capability("crm", "update_task")
            req = UpdateOperatorTaskRequest.model_validate(payload)
            return update_operator_task(req).model_dump()
        if action == "create_artifact":
            check_capability("crm", "write_artifact")
            req = CreateArtifactRequest.model_validate(payload)
            return create_artifact(req).model_dump()
        if action == "find_opportunity_for_email":
            check_capability("crm", "find_opportunity_for_email")
            email = str(payload.get("email") or "")
            return {"opportunity_id": find_opportunity_for_email(email)}
        if action == "close_opportunity_from_payment":
            check_capability("crm", "advance_opportunity")
            opportunity_id = str(payload.get("opportunity_id") or "")
            won_amount = payload.get("won_amount_usd") or 0
            payment_ref = str(payload.get("payment_ref") or "")
            customer_email = str(payload.get("customer_email") or "").strip()
            return close_opportunity_from_payment(
                opportunity_id=opportunity_id,
                won_amount_usd=float(won_amount),
                payment_ref=payment_ref,
                customer_email=customer_email,
            ).model_dump()
        if action == "log_feedback":
            check_capability("crm", "log_feedback")
            req = FeedbackLogRequest.model_validate(payload)
            return log_feedback(req)
        if action == "get_feedback_snapshot":
            check_capability("crm", "get_feedback_snapshot")
            return feedback_engine.snapshot()
        if action == "auto_create_opportunity_from_deal_scoring":
            check_capability("crm", "auto_create_opportunity")
            return auto_create_opportunity_from_deal_scoring(
                prospect_id=str(payload.get("prospect_id") or ""),
                contact_id=str(payload.get("contact_id") or ""),
                intel=payload.get("intel") or {},
                engagement=payload.get("engagement"),
            ).model_dump()
        if action == "auto_create_opportunity_from_lead":
            check_capability("crm", "auto_create_opportunity")
            return auto_create_opportunity_from_lead(
                prospect_id=str(payload.get("prospect_id") or ""),
                contact_id=str(payload.get("contact_id") or ""),
                intent_score=payload.get("intent_score"),
                monthly_budget=str(payload.get("monthly_budget") or ""),
                service_interest=list(payload.get("service_interest") or []),
                assigned_to=str(payload.get("assigned_to") or ""),
            ).model_dump()
        # --- Growth-enrichment Phase F (proof + referral; generation /
        # local-ledger only — no outward send, no payout) --------------------
        if action == "generate_case_study":
            check_capability("crm", "generate_case_study")
            from backend.proof.generator import handle_generate_case_study

            return handle_generate_case_study(payload)
        if action == "build_proof_wall":
            check_capability("crm", "build_proof_wall")
            from backend.proof.generator import handle_build_proof_wall

            return handle_build_proof_wall(payload)
        if action == "referral_code":
            check_capability("crm", "referral_code")
            from backend.referral.engine import handle_referral_code

            return handle_referral_code(payload)
        if action == "referral_record":
            check_capability("crm", "referral_record")
            from backend.referral.engine import handle_referral_record

            return handle_referral_record(payload)
        if action == "referral_qualify":
            check_capability("crm", "referral_qualify")
            from backend.referral.engine import handle_referral_qualify

            return handle_referral_qualify(payload)
        raise HTTPException(status_code=400, detail=f"unknown_action: {action}")

    # --- Feedback engine convenience endpoints (Phase 6) --------------

    @app.post("/feedback/log")
    async def feedback_log_route(request: Request) -> dict[str, Any]:
        """Convenience endpoint — POST a FeedbackLogRequest directly."""
        check_capability("crm", "log_feedback")
        body = await request.json()
        req = _parse(FeedbackLogRequest, body)
        return log_feedback(req)

    @app.get("/feedback/snapshot")
    async def feedback_snapshot_route() -> dict[str, Any]:
        """Convenience endpoint — GET the current feedback snapshot."""
        check_capability("crm", "get_feedback_snapshot")
        return feedback_engine.snapshot()

    # --- Telemetry reads (additive, 2026-05-20) ------------------------

    @app.get("/crm/metrics/funnel")
    async def funnel_metrics_route() -> dict[str, Any]:
        """Conversion-funnel leak analysis.

        Aggregates the funnel ledger the CRM lifecycle feeds (convert_lead ->
        prospect, create_opportunity -> opportunity, advance_opportunity ->
        proposal / closed_won / closed_lost) into per-stage counts plus
        adjacent-stage conversion rates so the operator can see where deals
        leak. Pure read; degrades to an all-zero snapshot when the ledger is
        unreadable. Gated by ``read_opportunities`` — funnel telemetry is a
        read over opportunity-lifecycle data.
        """
        check_capability("crm", "read_opportunities")
        return get_funnel_snapshot()

    @app.get("/crm/metrics/close-probability")
    async def close_probability_metrics_route() -> dict[str, Any]:
        """Empirical close-probability telemetry.

        Aggregates ``samus_opportunities`` into per-industry and per-stage
        empirical conversion rates (how often a deal at vertical Y / stage X
        actually reached closed_won). Deterministic; degrades to empty maps
        with a ddb_error string on a backend failure.
        """
        check_capability("crm", "read_opportunities")
        return estimated_close_probability()

    @app.get("/crm/metrics/token-cost")
    async def token_cost_metrics_route() -> dict[str, Any]:
        """Per-vertical discovery-LLM cost roll-up.

        Aggregates Opportunity.token_cost_usd by industry into total + mean
        discovery LLM spend per vertical. Deterministic; degrades to empty
        maps with a ddb_error string on a backend failure.
        """
        check_capability("crm", "read_opportunities")
        return token_cost_by_industry()

    @app.get("/crm/metrics/daily-stats")
    async def daily_stats_route(today: str | None = None) -> dict[str, Any]:
        """CallStates touched today, bucketed by outcome.

        Powers the gateway's ``GET /api/crm/stats`` (the forge-ui Samus HUD).
        ``today`` is an optional ``YYYY-MM-DD`` date; defaults to the container's
        own "today" when omitted. Gated by ``read_call_state`` — a read over the
        CallState table.

        WIRED-DORMANT PACIFIC BUSINESS DAY (arm with SAMUS_CRM_STATS_BUSINESS_TZ=1
        on BOTH samus-crm and samus-gateway): the gateway→CRM proxy signs the
        PATH ONLY (no query string), so "today" can't be passed across — instead
        both containers independently compute the SAME day. When armed, both
        compute the PACIFIC business day (via backend.common.dates) and this
        route selects the matching UTC range, so evening-PT calls stop rolling
        onto the next UTC day and dropping off the HUD. Unarmed default is the
        original UTC calendar day — identical behavior to before.
        """
        import datetime as _dt  # noqa: PLC0415 — only used here
        import os as _os  # noqa: PLC0415

        check_capability("crm", "read_call_state")
        day = (today or "").strip()

        if _os.getenv("SAMUS_CRM_STATS_BUSINESS_TZ", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        ):
            from backend.common.dates import (  # noqa: PLC0415
                business_today,
                business_day_utc_bounds,
            )

            if not day:
                day = business_today()
            start, end = business_day_utc_bounds(day)
            return daily_stats(day, start=start, end=end)

        if not day:
            day = _dt.datetime.utcnow().date().isoformat()
        return daily_stats(day)

    return app


app = create_app()
