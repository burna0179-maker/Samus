"""Optimizer workcell SQS worker."""
from __future__ import annotations

import logging
from typing import Any

from .models import OptimizePortfolioRequest, SelectArmRequest, UpdateArmRequest
from .service import optimize_portfolio, select_arm, update_arm

_LOG = logging.getLogger("samus.optimizer.worker")

_IMPORT_ERROR: Exception | None = None
try:
    from backend.common.aws_runtime import AwsRuntime, AwsWorkerSettings
    from backend.common.worker_base import BaseSqsWorker, serve_worker

    class OptimizerWorker(BaseSqsWorker):
        service = "optimizer"

        def handle(self, envelope: Any) -> dict[str, Any]:
            action = getattr(envelope, "action", "") or "select_arm"
            payload = getattr(envelope, "payload", None) or {}
            if action == "select_arm":
                return select_arm(SelectArmRequest.model_validate(payload)).model_dump()
            if action == "update_arm":
                return update_arm(UpdateArmRequest.model_validate(payload)).model_dump()
            if action == "optimize_portfolio":
                return optimize_portfolio(OptimizePortfolioRequest.model_validate(payload)).model_dump()
            raise ValueError(f"unknown_action: {action}")

except Exception as exc:  # pragma: no cover
    _IMPORT_ERROR = exc
    _LOG.warning("BaseSqsWorker import failed (%s); exposing placeholder", exc)
    AwsRuntime = None  # type: ignore[assignment]
    AwsWorkerSettings = None  # type: ignore[assignment]
    serve_worker = None  # type: ignore[assignment]

    class OptimizerWorker:  # type: ignore[no-redef]
        service = "optimizer"

        def __init__(self, *args, **kwargs):
            raise RuntimeError(
                f"OptimizerWorker placeholder; worker_base import failed: {_IMPORT_ERROR!r}"
            )

        def handle(self, envelope):  # pragma: no cover
            raise NotImplementedError


def main() -> None:
    if _IMPORT_ERROR is not None or serve_worker is None:
        raise NotImplementedError(
            f"worker_base unavailable; import failed: {_IMPORT_ERROR!r}"
        )
    settings = AwsWorkerSettings.from_env("optimizer", "SQS_OPTIMIZER_QUEUE_URL")  # type: ignore[union-attr]
    serve_worker(OptimizerWorker(AwsRuntime(settings)))  # type: ignore[misc]


if __name__ == "__main__":
    main()
