"""Leadgen workcell SQS worker.

Routes ``envelope.action == 'score_lead'`` -> :func:`process_lead`. Wraps
``backend.common.worker_base`` in try/except so the module imports cleanly even
if the base class is unavailable (CI without boto / Cloud Run cold-start race).
"""

from __future__ import annotations

import logging
from typing import Any

from .models import LeadRequest
from .service import process_lead

_LOG = logging.getLogger("samus.leadgen.worker")

_IMPORT_ERROR: Exception | None = None
try:
    from backend.common.aws_runtime import AwsRuntime, AwsWorkerSettings
    from backend.common.worker_base import BaseSqsWorker, serve_worker

    class LeadgenWorker(BaseSqsWorker):
        service = "leadgen"

        def handle(self, envelope: Any) -> dict[str, Any]:
            action = getattr(envelope, "action", "") or "score_lead"
            payload = getattr(envelope, "payload", None) or {}
            if action == "score_lead":
                req = LeadRequest.model_validate(payload)
                task_id = getattr(envelope, "task_id", None) or getattr(envelope, "id", None)
                return process_lead(req, task_id=task_id).model_dump()
            raise ValueError(f"unknown_action: {action}")

except Exception as exc:  # pragma: no cover
    _IMPORT_ERROR = exc
    _LOG.warning("BaseSqsWorker import failed (%s); exposing placeholder", exc)
    AwsRuntime = None  # type: ignore[assignment]
    AwsWorkerSettings = None  # type: ignore[assignment]
    serve_worker = None  # type: ignore[assignment]

    class LeadgenWorker:  # type: ignore[no-redef]
        service = "leadgen"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                f"LeadgenWorker placeholder; worker_base import failed: {_IMPORT_ERROR!r}"
            )

        def handle(self, envelope: Any) -> dict[str, Any]:  # pragma: no cover
            raise NotImplementedError


def main() -> None:
    if _IMPORT_ERROR is not None or serve_worker is None:
        raise NotImplementedError(f"worker_base unavailable; import failed: {_IMPORT_ERROR!r}")
    settings = AwsWorkerSettings.from_env("leadgen", "SQS_LEADGEN_QUEUE_URL")  # type: ignore[union-attr]
    serve_worker(LeadgenWorker(AwsRuntime(settings)))  # type: ignore[misc]


if __name__ == "__main__":
    main()
