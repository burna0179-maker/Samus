"""Intake workcell SQS worker.

Phase 1 status: HTTP-only. There is no ``samus-intake-jobs`` SQS queue —
the only inbound surface is the browser POST from the marketing site, which
is naturally synchronous. The ``IntakeWorker`` class and ``main()`` exist
to match the canonical workcell shape; ``main()`` raises NotImplementedError
until the queue is provisioned.

When a queue is added (e.g. for retry of failed DDB writes), mirror the
optimizer worker: BaseSqsWorker subclass routing by ``envelope.action``,
plus a ``main()`` that calls ``serve_worker(IntakeWorker(...))``.
"""

from __future__ import annotations

import logging
from typing import Any

from .models import OnboardingLeadRequest
from .service import list_recent_leads, submit_lead


_LOG = logging.getLogger("samus.intake.worker")

_IMPORT_ERROR: Exception | None = None
try:
    from backend.common.aws_runtime import AwsRuntime, AwsWorkerSettings
    from backend.common.worker_base import BaseSqsWorker, serve_worker

    class IntakeWorker(BaseSqsWorker):
        service = "intake"

        def handle(self, envelope: Any) -> dict[str, Any]:
            action = getattr(envelope, "action", "") or "submit_lead"
            payload = getattr(envelope, "payload", None) or {}
            if action == "submit_lead":
                req = OnboardingLeadRequest.model_validate(payload)
                return submit_lead(req).model_dump()
            if action == "list_leads":
                limit = int(payload.get("limit") or 50)
                return list_recent_leads(limit=limit).model_dump()
            raise ValueError(f"unknown_action: {action}")

except Exception as exc:  # pragma: no cover
    _IMPORT_ERROR = exc
    _LOG.warning("BaseSqsWorker import failed (%s); exposing placeholder", exc)
    AwsRuntime = None  # type: ignore[assignment]
    AwsWorkerSettings = None  # type: ignore[assignment]
    serve_worker = None  # type: ignore[assignment]

    class IntakeWorker:  # type: ignore[no-redef]
        service = "intake"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                f"IntakeWorker placeholder; worker_base import failed: {_IMPORT_ERROR!r}"
            )

        def handle(self, envelope: Any) -> dict[str, Any]:  # pragma: no cover
            raise NotImplementedError


def main() -> None:
    """Module entrypoint — not yet usable.

    No SQS queue is provisioned for the intake workcell in Phase 1. When
    one is added, mirror backend.optimizer.worker.main() — build
    AwsWorkerSettings from env ('intake', 'SQS_INTAKE_QUEUE_URL'), wrap in
    AwsRuntime, hand to serve_worker(IntakeWorker(...)).
    """
    raise NotImplementedError(
        "Intake workcell has no SQS queue in Phase 1 — wire SQS_INTAKE_QUEUE_URL "
        "and a samus-intake-jobs queue before invoking this worker."
    )


if __name__ == "__main__":
    main()
