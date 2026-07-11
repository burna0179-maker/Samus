"""Fulfillment workcell SQS worker.

Routes ``envelope.action`` to the appropriate handler:

  plan_execution  — backend.fulfillment.logic.plan_fulfillment
  validate_plan   — dag.validate_plan
  execute_plan    — dag.execute_plan  (async — wrapped with asyncio.run)
  ingest_result   — dag.ingest_result
  resume_plan     — dag.resume_plan
  finalize_plan   — dag.finalize_plan

Wraps ``backend.common.worker_base`` in try/except so the module imports
cleanly even if the base class is unavailable.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .dag import (
    execute_plan,
    finalize_plan,
    ingest_result,
    plan_from_dict,
    plan_to_dict,
    resume_plan,
    validate_plan,
)
from .logic import plan_fulfillment

_LOG = logging.getLogger("samus.fulfillment.worker")

_IMPORT_ERROR: Exception | None = None
try:
    from backend.common.aws_runtime import AwsRuntime, AwsWorkerSettings
    from backend.common.config import get_settings
    from backend.common.worker_base import BaseSqsWorker, serve_worker

    class FulfillmentWorker(BaseSqsWorker):
        service = "fulfillment"

        def handle(self, envelope: Any) -> dict[str, Any]:
            action = getattr(envelope, "action", "") or "plan_execution"
            payload = getattr(envelope, "payload", None) or {}

            # ------------------------------------------------------------------
            # plan_execution
            # ------------------------------------------------------------------
            if action == "plan_execution":
                task_id = getattr(envelope, "task_id", "") or getattr(envelope, "id", "")
                metadata = getattr(envelope, "metadata", None) or {}
                return plan_fulfillment(task_id, payload, metadata)

            # ------------------------------------------------------------------
            # validate_plan
            # ------------------------------------------------------------------
            if action == "validate_plan":
                plan = plan_from_dict(payload["plan"])
                errors = validate_plan(plan)
                return {"valid": len(errors) == 0, "errors": errors}

            # ------------------------------------------------------------------
            # execute_plan  (async — wrap with asyncio.run)
            # ------------------------------------------------------------------
            if action == "execute_plan":
                plan = plan_from_dict(payload["plan"])
                settings = get_settings()
                gateway = settings.gateway_urls.get("gateway", "http://samus-gateway:8080")
                result = asyncio.run(
                    execute_plan(
                        plan,
                        gateway_url=gateway,
                        hmac_key=settings.shared_hmac_key,
                    )
                )
                return plan_to_dict(result)

            # ------------------------------------------------------------------
            # ingest_result
            # ------------------------------------------------------------------
            if action == "ingest_result":
                plan = plan_from_dict(payload["plan"])
                step_id: str = payload["step_id"]
                result_status: str = payload["status"]
                output: dict | None = payload.get("output")
                newly_ready = ingest_result(plan, step_id, result_status, output)
                return {
                    "plan": plan_to_dict(plan),
                    "newly_ready_step_ids": [s.id for s in newly_ready],
                }

            # ------------------------------------------------------------------
            # resume_plan
            # ------------------------------------------------------------------
            if action == "resume_plan":
                plan = plan_from_dict(payload["plan"])
                return plan_to_dict(resume_plan(plan))

            # ------------------------------------------------------------------
            # finalize_plan
            # ------------------------------------------------------------------
            if action == "finalize_plan":
                plan = plan_from_dict(payload["plan"])
                return plan_to_dict(finalize_plan(plan))

            raise ValueError(f"unknown_action: {action}")

except Exception as exc:  # pragma: no cover
    _IMPORT_ERROR = exc
    _LOG.warning("BaseSqsWorker import failed (%s); exposing placeholder", exc)
    AwsRuntime = None  # type: ignore[assignment]
    AwsWorkerSettings = None  # type: ignore[assignment]
    serve_worker = None  # type: ignore[assignment]

    class FulfillmentWorker:  # type: ignore[no-redef]
        service = "fulfillment"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                f"FulfillmentWorker placeholder; worker_base import failed: {_IMPORT_ERROR!r}"
            )

        def handle(self, envelope: Any) -> dict[str, Any]:  # pragma: no cover
            raise NotImplementedError


def main() -> None:
    if _IMPORT_ERROR is not None or serve_worker is None:
        raise NotImplementedError(f"worker_base unavailable; import failed: {_IMPORT_ERROR!r}")
    settings = AwsWorkerSettings.from_env(  # type: ignore[union-attr]
        "fulfillment", "SQS_FULFILLMENT_QUEUE_URL"
    )
    serve_worker(FulfillmentWorker(AwsRuntime(settings)))  # type: ignore[misc]


if __name__ == "__main__":
    main()
