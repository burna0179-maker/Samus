"""Voice workcell SQS worker.

Phase 1 status: HTTP-only. There is no ``samus-voice-jobs`` SQS queue yet —
the inbound surfaces are (1) operator-driven ``POST /voice/call`` and (2) the
Vapi-driven ``POST /vapi/webhook``. Neither benefits from queueing in Phase 1.
The ``VoiceWorker`` class and ``main()`` exist to match the canonical workcell
shape; ``main()`` raises NotImplementedError until the queue is provisioned.

When a queue is added (e.g. for scheduled outbound call campaigns), wire it
the same way as ``backend.optimizer.worker``: BaseSqsWorker subclass with a
single ``handle(envelope)`` routing by ``envelope.action``, plus a ``main()``
that calls ``serve_worker(VoiceWorker(AwsRuntime(settings)))``.
"""

from __future__ import annotations

import logging
from typing import Any

from .models import InitiateCallRequest
from .service import get_call_status, initiate_call, list_recent_calls


_LOG = logging.getLogger("samus.voice.worker")

_IMPORT_ERROR: Exception | None = None
try:
    from backend.common.aws_runtime import AwsRuntime, AwsWorkerSettings
    from backend.common.worker_base import BaseSqsWorker, serve_worker

    class VoiceWorker(BaseSqsWorker):
        service = "voice"

        def handle(self, envelope: Any) -> dict[str, Any]:
            action = getattr(envelope, "action", "") or "initiate_call"
            payload = getattr(envelope, "payload", None) or {}
            if action == "initiate_call":
                return initiate_call(InitiateCallRequest.model_validate(payload)).model_dump()
            if action == "fetch_call":
                call_id = str(payload.get("call_id") or "")
                if not call_id:
                    raise ValueError("call_id required")
                return get_call_status(call_id).model_dump()
            if action == "list_calls":
                limit = int(payload.get("limit") or 10)
                return list_recent_calls(limit=limit).model_dump()
            raise ValueError(f"unknown_action: {action}")

except Exception as exc:  # pragma: no cover
    _IMPORT_ERROR = exc
    _LOG.warning("BaseSqsWorker import failed (%s); exposing placeholder", exc)
    AwsRuntime = None  # type: ignore[assignment]
    AwsWorkerSettings = None  # type: ignore[assignment]
    serve_worker = None  # type: ignore[assignment]

    class VoiceWorker:  # type: ignore[no-redef]
        service = "voice"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                f"VoiceWorker placeholder; worker_base import failed: {_IMPORT_ERROR!r}"
            )

        def handle(self, envelope: Any) -> dict[str, Any]:  # pragma: no cover
            raise NotImplementedError


def main() -> None:
    """Module entrypoint — not yet usable.

    No SQS queue is provisioned for the voice workcell in Phase 1. When one
    is added, mirror backend.optimizer.worker.main() — build AwsWorkerSettings
    from env ('voice', 'SQS_VOICE_QUEUE_URL'), wrap in AwsRuntime, hand to
    serve_worker(VoiceWorker(...)).
    """
    raise NotImplementedError(
        "Voice workcell has no SQS queue in Phase 1 — wire SQS_VOICE_QUEUE_URL "
        "and a samus-voice-jobs queue before invoking this worker."
    )


if __name__ == "__main__":
    main()
