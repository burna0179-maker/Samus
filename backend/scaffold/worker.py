"""Scaffold workcell SQS worker.

Routes ``envelope.action == 'generate_assets'`` -> :func:`generate_scaffold`.
Wraps ``backend.common.worker_base`` in try/except so the module imports cleanly
even if the base class is unavailable.
"""
from __future__ import annotations

import logging
from typing import Any

from .logic import generate_scaffold
from .models import ScaffoldRequest

_LOG = logging.getLogger("samus.scaffold.worker")

_IMPORT_ERROR: Exception | None = None
try:
    from backend.common.aws_runtime import AwsRuntime, AwsWorkerSettings
    from backend.common.worker_base import BaseSqsWorker, serve_worker

    class ScaffoldWorker(BaseSqsWorker):
        service = "scaffold"

        def handle(self, envelope: Any) -> dict[str, Any]:
            action = getattr(envelope, "action", "") or "generate_assets"
            payload = getattr(envelope, "payload", None) or {}
            if action == "generate_assets":
                req = ScaffoldRequest.model_validate(payload)
                return generate_scaffold(req)
            raise ValueError(f"unknown_action: {action}")

except Exception as exc:  # pragma: no cover
    _IMPORT_ERROR = exc
    _LOG.warning("BaseSqsWorker import failed (%s); exposing placeholder", exc)
    AwsRuntime = None  # type: ignore[assignment]
    AwsWorkerSettings = None  # type: ignore[assignment]
    serve_worker = None  # type: ignore[assignment]

    class ScaffoldWorker:  # type: ignore[no-redef]
        service = "scaffold"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                f"ScaffoldWorker placeholder; worker_base import failed: {_IMPORT_ERROR!r}"
            )

        def handle(self, envelope: Any) -> dict[str, Any]:  # pragma: no cover
            raise NotImplementedError


def main() -> None:
    if _IMPORT_ERROR is not None or serve_worker is None:
        raise NotImplementedError(
            f"worker_base unavailable; import failed: {_IMPORT_ERROR!r}"
        )
    settings = AwsWorkerSettings.from_env("scaffold", "SQS_SCAFFOLD_QUEUE_URL")  # type: ignore[union-attr]
    serve_worker(ScaffoldWorker(AwsRuntime(settings)))  # type: ignore[misc]


if __name__ == "__main__":
    main()
