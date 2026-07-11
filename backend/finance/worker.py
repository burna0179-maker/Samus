"""Finance workcell SQS worker.

Phase 1 status: HTTP-only. There is no ``samus-finance-jobs`` SQS queue yet
because the only inbound surface is the operator-driven /snapshot pull. The
``FinanceWorker`` class and ``main()`` exist to match the canonical workcell
shape; ``main()`` raises NotImplementedError until the queue is provisioned.

When a queue is added (e.g. for webhook-driven Stripe events via SNS or for
scheduled snapshots from Cloud Scheduler), wire it the same way as
``backend.optimizer.worker``: BaseSqsWorker subclass with a single
``handle(envelope)`` routing by ``envelope.action``, plus a ``main()`` that
calls ``serve_worker(FinanceWorker(AwsRuntime(settings)))``.
"""

from __future__ import annotations

import logging
from typing import Any

from .models import DeclinesRequest, RunwayRequest, SnapshotRequest
from .service import (
    get_actions_summary,
    get_codb_summary,
    get_debt_portfolio,
    get_declines_summary,
    get_hardship_context,
    get_info_gaps_summary,
    get_liabilities_summary,
    get_payment_links,
    get_recent_payments,
    get_runway,
    get_snapshot,
)

_LOG = logging.getLogger("samus.finance.worker")

_IMPORT_ERROR: Exception | None = None
try:
    from backend.common.aws_runtime import AwsRuntime, AwsWorkerSettings
    from backend.common.worker_base import BaseSqsWorker, serve_worker

    class FinanceWorker(BaseSqsWorker):
        service = "finance"

        def handle(self, envelope: Any) -> dict[str, Any]:
            action = getattr(envelope, "action", "") or "snapshot"
            payload = getattr(envelope, "payload", None) or {}
            if action == "snapshot":
                return get_snapshot(SnapshotRequest.model_validate(payload)).model_dump()
            if action == "codb_summary":
                return get_codb_summary().model_dump()
            if action == "runway":
                req = RunwayRequest.model_validate(payload)
                return get_runway(req.override_balance_usd).model_dump()
            if action == "liabilities":
                return get_liabilities_summary().model_dump()
            if action == "declines":
                req = DeclinesRequest.model_validate(payload)
                return get_declines_summary(req.window_days).model_dump()
            if action == "debts":
                return get_debt_portfolio().model_dump()
            if action == "actions":
                return get_actions_summary().model_dump()
            if action == "info_gaps":
                return get_info_gaps_summary().model_dump()
            if action == "hardship":
                return get_hardship_context().model_dump()
            if action == "payment_links":
                active = bool((payload or {}).get("active", True))
                return get_payment_links(active=active).model_dump()
            if action == "recent_payments":
                window = int((payload or {}).get("window_days", 7))
                return get_recent_payments(window).model_dump()
            raise ValueError(f"unknown_action: {action}")

except Exception as exc:  # pragma: no cover
    _IMPORT_ERROR = exc
    _LOG.warning("BaseSqsWorker import failed (%s); exposing placeholder", exc)
    AwsRuntime = None  # type: ignore[assignment]
    AwsWorkerSettings = None  # type: ignore[assignment]
    serve_worker = None  # type: ignore[assignment]

    class FinanceWorker:  # type: ignore[no-redef]
        service = "finance"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                f"FinanceWorker placeholder; worker_base import failed: {_IMPORT_ERROR!r}"
            )

        def handle(self, envelope: Any) -> dict[str, Any]:  # pragma: no cover
            raise NotImplementedError


def main() -> None:
    """Module entrypoint -- not yet usable.

    No SQS queue is provisioned for the finance workcell in Phase 1. When one
    is added, mirror backend.optimizer.worker.main() — build AwsWorkerSettings
    from env ('finance', 'SQS_FINANCE_QUEUE_URL'), wrap in AwsRuntime, hand to
    serve_worker(FinanceWorker(...)).
    """
    raise NotImplementedError(
        "Finance workcell has no SQS queue in Phase 1 — wire SQS_FINANCE_QUEUE_URL "
        "and a samus-finance-jobs queue before invoking this worker."
    )


if __name__ == "__main__":
    main()
