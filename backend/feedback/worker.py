"""SQS worker for the feedback workcell (doc §9).

Wraps the BaseSqsWorker / AwsRuntime imports in try/except — the Phase A
worker-base port may not yet be fully landed in this branch, in which case we
expose a placeholder class so the rest of the workcell stays importable.
"""
from __future__ import annotations

import logging
from typing import Any

from .models import FeedbackResult, SnsNotification
from .service import process_sns_notification

_LOG = logging.getLogger("samus.feedback.worker")


try:  # pragma: no cover - exercised only when worker_base is fully ported
    from backend.common.worker_base import BaseSqsWorker, serve_worker  # type: ignore
    from backend.common.aws_runtime import AwsRuntime, AwsWorkerSettings  # type: ignore

    class FeedbackWorker(BaseSqsWorker):
        """One-action SQS worker: ``ingest``."""

        service = "feedback"
        action = "ingest"

        def handle(self, envelope: Any) -> dict[str, Any]:
            payload = getattr(envelope, "payload", None) or {}
            sns = SnsNotification.model_validate(payload)
            task_id = getattr(envelope, "task_id", None) or getattr(envelope, "id", None)
            result: FeedbackResult = process_sns_notification(sns, task_id=task_id)
            return result.model_dump()

    if __name__ == "__main__":  # pragma: no cover - runtime entrypoint
        serve_worker(
            FeedbackWorker(
                AwsRuntime(
                    AwsWorkerSettings.from_env("feedback", "SQS_FEEDBACK_QUEUE_URL")
                )
            )
        )

except Exception as exc:  # pragma: no cover - placeholder branch
    _LOG.warning(
        "BaseSqsWorker / AwsRuntime import failed (%s); exposing placeholder FeedbackWorker",
        exc,
    )

    class FeedbackWorker:  # type: ignore[no-redef]
        """Placeholder until backend.common.worker_base + aws_runtime land."""

        service = "feedback"
        action = "ingest"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                "FeedbackWorker is a placeholder until backend.common.worker_base "
                "and backend.common.aws_runtime are ported."
            )
