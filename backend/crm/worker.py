"""CRM workcell SQS worker.

Consumes the ``samus-crm-jobs`` SQS queue. Producers (seo, proposal,
finance webhook handler, and any future workcell that needs to mutate
CRM state) dispatch envelopes to the gateway's ``POST /dispatch/crm``;
the gateway enqueues to SQS; this worker drains the queue and applies
each action to the underlying DDB tables.

Why SQS instead of direct HTTP dispatch from the producer:
  - Cloud Run scale-to-zero terminates daemon threads spawned outside
    a tracked request. SQS makes the dispatch durable across instance
    restarts — the message lives in AWS until acked.
  - DLQ on persistent failure (after the redrive policy's max receives)
    surfaces problems via the existing ``gateway /dlq/crm`` endpoint.
  - Producer stays sync + fast: enqueue is a single HMAC-signed POST
    to the gateway (~tens of ms), the actual CRM work happens
    asynchronously on this worker.

Action router mirrors :mod:`backend.crm.app`'s ``/work`` endpoint —
every action that route accepts is also handled here, plus one extra
collapsed action (:func:`close_payment_to_opportunity`) that combines
finance's two-step flow into a single message so the producer enqueues
once instead of twice.
"""
from __future__ import annotations

import logging
from typing import Any

from .models import (
    AdvanceOpportunityRequest,
    ConvertLeadRequest,
    CreateArtifactRequest,
    CreateOperatorTaskRequest,
    CreateOpportunityRequest,
    UpdateOperatorTaskRequest,
)
from .service import (
    advance_opportunity,
    close_opportunity_from_payment,
    convert_lead_to_prospect,
    create_artifact,
    create_operator_task,
    create_opportunity,
    find_opportunity_for_email,
    update_operator_task,
    upsert_call_state,
    upsert_conversation,
)
from .models import CallState, Conversation


_LOG = logging.getLogger("samus.crm.worker")


def _handle_action(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Pure action router — kept module-level so it's importable from tests
    without spinning up the BaseSqsWorker infrastructure.

    Every action returns a JSON-serializable dict the worker base will
    publish as the success-result SNS event.
    """
    if action == "convert_lead":
        return convert_lead_to_prospect(
            ConvertLeadRequest.model_validate(payload),
        ).model_dump()

    if action == "create_opportunity":
        return create_opportunity(
            CreateOpportunityRequest.model_validate(payload),
        ).model_dump()

    if action == "advance_opportunity":
        return advance_opportunity(
            AdvanceOpportunityRequest.model_validate(payload),
        ).model_dump()

    if action == "upsert_conversation":
        conv = Conversation.model_validate(payload)
        if not conv.conversation_id:
            raise ValueError("conversation_id required")
        ok = upsert_conversation(conv)
        return {"persisted": ok, "id": conv.conversation_id,
                "error": None if ok else "ddb_put_failed"}

    if action == "upsert_call_state":
        state = CallState.model_validate(payload)
        if not state.prospect_id:
            raise ValueError("prospect_id required")
        ok = upsert_call_state(state)
        return {"persisted": ok, "id": state.prospect_id,
                "error": None if ok else "ddb_put_failed"}

    if action == "create_task":
        return create_operator_task(
            CreateOperatorTaskRequest.model_validate(payload),
        ).model_dump()

    if action == "update_task":
        return update_operator_task(
            UpdateOperatorTaskRequest.model_validate(payload),
        ).model_dump()

    if action == "create_artifact":
        return create_artifact(
            CreateArtifactRequest.model_validate(payload),
        ).model_dump()

    if action == "find_opportunity_for_email":
        email = str(payload.get("email") or "").strip()
        opp_id = find_opportunity_for_email(email) if email else None
        return {"opportunity_id": opp_id, "email_tail": email[-12:]}

    if action == "close_opportunity_from_payment":
        opp_id = str(payload.get("opportunity_id") or "")
        amount = float(payload.get("won_amount_usd") or 0.0)
        payment_ref = str(payload.get("payment_ref") or "")
        customer_email = str(payload.get("customer_email") or "").strip()
        if not opp_id:
            raise ValueError("opportunity_id required")
        return close_opportunity_from_payment(
            opp_id, amount, payment_ref, customer_email=customer_email,
        ).model_dump()

    if action == "close_payment_to_opportunity":
        # Collapsed two-step action used by the finance Stripe webhook:
        # lookup + advance in a single message. Saves the producer from
        # enqueueing twice and keeps the close-the-loop sequence atomic
        # from the operator's perspective.
        email = str(payload.get("email") or "").strip()
        amount = float(payload.get("amount_usd") or payload.get("won_amount_usd") or 0.0)
        payment_ref = str(payload.get("payment_ref") or payload.get("event_id") or "")
        if not email:
            return {"status": "skipped", "reason": "email_required",
                    "email_tail": "", "opportunity_id": None}
        opp_id = find_opportunity_for_email(email)
        if not opp_id:
            return {"status": "no_open_opportunity",
                    "email_tail": email[-12:], "opportunity_id": None}
        # RT FIN-02: forward the paying customer's email so the ownership
        # re-check inside close_opportunity_from_payment runs (it no-ops when
        # customer_email is empty). opp_id was just looked up BY this email, so
        # the check passes — but it must not be silently skipped.
        advance = close_opportunity_from_payment(
            opp_id, amount, payment_ref, customer_email=email,
        )
        return {
            "status": "advanced" if advance.status == "advanced" else advance.status,
            "email_tail": email[-12:],
            "opportunity_id": opp_id,
            "advance_result": advance.model_dump(),
        }

    raise ValueError(f"unknown_action: {action}")


_IMPORT_ERROR: Exception | None = None
try:
    from backend.common.aws_runtime import AwsRuntime, AwsWorkerSettings
    from backend.common.worker_base import BaseSqsWorker, serve_worker

    class CrmWorker(BaseSqsWorker):
        service = "crm"

        def handle(self, envelope: Any) -> dict[str, Any]:
            action = getattr(envelope, "action", "") or "convert_lead"
            payload = getattr(envelope, "payload", None) or {}
            return _handle_action(action, payload)

except Exception as exc:  # pragma: no cover
    _IMPORT_ERROR = exc
    _LOG.warning("BaseSqsWorker import failed (%s); exposing placeholder", exc)
    AwsRuntime = None  # type: ignore[assignment]
    AwsWorkerSettings = None  # type: ignore[assignment]
    serve_worker = None  # type: ignore[assignment]

    class CrmWorker:  # type: ignore[no-redef]
        service = "crm"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                f"CrmWorker placeholder; worker_base import failed: {_IMPORT_ERROR!r}"
            )

        def handle(self, envelope: Any) -> dict[str, Any]:  # pragma: no cover
            raise NotImplementedError


def main() -> None:
    if _IMPORT_ERROR is not None or serve_worker is None:
        raise NotImplementedError(
            f"worker_base unavailable; import failed: {_IMPORT_ERROR!r}",
        )
    settings = AwsWorkerSettings.from_env("crm", "SQS_CRM_QUEUE_URL")  # type: ignore[union-attr]
    serve_worker(CrmWorker(AwsRuntime(settings)))  # type: ignore[misc]


if __name__ == "__main__":
    main()
