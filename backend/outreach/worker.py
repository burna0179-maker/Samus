"""Outreach workcell SQS worker."""
from __future__ import annotations

import logging
from typing import Any

from .models import OutreachAdvanceRequest, OutreachLogRequest, OutreachMessageRequest
from .service import advance_call, log_outcome, send_message

_LOG = logging.getLogger("samus.outreach.worker")

_IMPORT_ERROR: Exception | None = None
try:
    from backend.common.aws_runtime import AwsRuntime, AwsWorkerSettings
    from backend.common.worker_base import BaseSqsWorker, serve_worker

    class OutreachWorker(BaseSqsWorker):
        service = "outreach"

        def handle(self, envelope: Any) -> dict[str, Any]:
            action = getattr(envelope, "action", "") or "advance_call"
            payload = getattr(envelope, "payload", None) or {}
            if action == "advance_call":
                return advance_call(OutreachAdvanceRequest.model_validate(payload)).model_dump()
            if action == "log_outcome":
                return log_outcome(OutreachLogRequest.model_validate(payload)).model_dump()
            if action == "send_message":
                return send_message(OutreachMessageRequest.model_validate(payload))
            raise ValueError(f"unknown_action: {action}")

except Exception as exc:  # pragma: no cover
    _IMPORT_ERROR = exc
    _LOG.warning("BaseSqsWorker import failed (%s); exposing placeholder", exc)
    AwsRuntime = None  # type: ignore[assignment]
    AwsWorkerSettings = None  # type: ignore[assignment]
    serve_worker = None  # type: ignore[assignment]

    class OutreachWorker:  # type: ignore[no-redef]
        service = "outreach"

        def __init__(self, *args, **kwargs):
            raise RuntimeError(
                f"OutreachWorker placeholder; worker_base import failed: {_IMPORT_ERROR!r}"
            )

        def handle(self, envelope):  # pragma: no cover
            raise NotImplementedError


def main() -> None:
    if _IMPORT_ERROR is not None or serve_worker is None:
        raise NotImplementedError(
            f"worker_base unavailable; import failed: {_IMPORT_ERROR!r}"
        )
    settings = AwsWorkerSettings.from_env("outreach", "SQS_OUTREACH_QUEUE_URL")  # type: ignore[union-attr]
    serve_worker(OutreachWorker(AwsRuntime(settings)))  # type: ignore[misc]


if __name__ == "__main__":
    main()
