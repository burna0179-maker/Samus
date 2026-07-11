"""Proposal workcell SQS worker.

Routes ``envelope.action`` -> :func:`generate_proposal` /
:func:`validate_proposal`. Wraps ``backend.common.worker_base`` in try/except
so the module imports cleanly even if the base class is unavailable.
"""
from __future__ import annotations

import logging
from typing import Any

from .models import CompiledWorkflow, ProposalRequest
from .service import generate_proposal, validate_proposal

_LOG = logging.getLogger("samus.proposal.worker")

_IMPORT_ERROR: Exception | None = None
try:
    from backend.common.aws_runtime import AwsRuntime, AwsWorkerSettings
    from backend.common.worker_base import BaseSqsWorker, serve_worker

    class ProposalWorker(BaseSqsWorker):
        """Proposal workcell worker -- routes by envelope.action."""

        service = "proposal"

        def handle(self, envelope: Any) -> dict[str, Any]:
            action = getattr(envelope, "action", "") or "generate_proposal"
            payload = getattr(envelope, "payload", None) or {}
            task_id = getattr(envelope, "task_id", "") or ""
            if action == "generate_proposal":
                req = ProposalRequest.model_validate(
                    {"task_id": task_id, "intake": payload.get("intake", payload)}
                )
                return generate_proposal(req).model_dump()
            if action == "validate_proposal":
                workflow = CompiledWorkflow.model_validate(payload)
                return validate_proposal(workflow).model_dump()
            raise ValueError(f"unknown_action: {action}")

except Exception as exc:  # pragma: no cover - placeholder branch
    _IMPORT_ERROR = exc
    _LOG.warning("BaseSqsWorker import failed (%s); exposing placeholder ProposalWorker", exc)

    AwsRuntime = None  # type: ignore[assignment]
    AwsWorkerSettings = None  # type: ignore[assignment]
    serve_worker = None  # type: ignore[assignment]

    class ProposalWorker:  # type: ignore[no-redef]
        """Placeholder until backend.common.worker_base is importable."""

        service = "proposal"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                "ProposalWorker is a placeholder until backend.common.worker_base is "
                f"available; original import failed with: {_IMPORT_ERROR!r}"
            )

        def handle(self, envelope: Any) -> dict[str, Any]:  # pragma: no cover
            raise NotImplementedError


def main() -> None:
    """Module entrypoint -- ``python -m backend.proposal.worker``."""
    if _IMPORT_ERROR is not None or serve_worker is None:
        raise NotImplementedError(
            "backend.common.worker_base is not available yet; cannot serve. "
            f"Original import error: {_IMPORT_ERROR!r}"
        )
    settings = AwsWorkerSettings.from_env("proposal", "SQS_PROPOSAL_QUEUE_URL")  # type: ignore[union-attr]
    runtime = AwsRuntime(settings)  # type: ignore[misc]
    serve_worker(ProposalWorker(runtime))  # type: ignore[misc]


if __name__ == "__main__":
    main()
