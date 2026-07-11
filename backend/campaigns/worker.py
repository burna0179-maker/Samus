"""Campaign Orchestrator SQS worker.

Routes ``envelope.action`` -> the orchestrator lifecycle operations, mirroring
``backend.seo.worker`` so a campaign run can be driven asynchronously off the
queue as well as synchronously over HTTP. Wrapped in try/except so the module
imports cleanly even when ``backend.common.worker_base`` is unavailable.
"""
from __future__ import annotations

import logging
from typing import Any

from .models import CampaignInstance
from .orchestrator import default_orchestrator

_LOG = logging.getLogger("samus.campaigns.worker")

_IMPORT_ERROR: Exception | None = None
try:
    from backend.common.aws_runtime import AwsRuntime, AwsWorkerSettings
    from backend.common.worker_base import BaseSqsWorker, serve_worker

    class CampaignWorker(BaseSqsWorker):
        """Campaign workcell worker — routes by envelope.action."""

        service = "campaigns"

        def handle(self, envelope: Any) -> dict[str, Any]:
            action = getattr(envelope, "action", "") or ""
            payload = getattr(envelope, "payload", None) or {}
            orch = default_orchestrator()
            if action == "create_campaign":
                instance = CampaignInstance.model_validate(
                    payload.get("instance", payload)
                )
                return orch.create_campaign(instance).model_dump()
            if action == "start_campaign":
                return orch.start(payload["campaign_id"]).model_dump()
            if action == "advance_campaign":
                return orch.advance(payload["campaign_id"]).model_dump()
            if action == "approve_node":
                return orch.approve_node(
                    payload["campaign_id"],
                    node_id=payload.get("node_id"),
                    approval_id=payload.get("approval_id"),
                    decision=payload.get("decision", "approved"),
                    decided_by=payload.get("decided_by", "operator"),
                ).model_dump()
            if action in ("ingest_kpi", "update_kpis"):
                from .kpi import apply_kpi_events

                run = orch.get_run(payload["campaign_id"])
                if run is None:
                    raise ValueError(f"unknown_campaign: {payload['campaign_id']}")
                changed = apply_kpi_events(run, payload.get("events", []))
                orch._store.save(run)  # noqa: SLF001 — orchestrator-owned store
                return {"campaign_id": run.campaign_id, "changed": changed}
            if action == "generate_report":
                from .report import generate_weekly_report

                run = orch.get_run(payload["campaign_id"])
                if run is None:
                    raise ValueError(f"unknown_campaign: {payload['campaign_id']}")
                artifact = generate_weekly_report(run, orch.ledger)
                run.artifacts_created.append(artifact)
                orch._store.save(run)  # noqa: SLF001
                return {"campaign_id": run.campaign_id, "artifact": artifact.model_dump()}
            raise ValueError(f"unknown_action: {action}")

except Exception as exc:  # pragma: no cover - import shim
    _IMPORT_ERROR = exc
    _LOG.warning("BaseSqsWorker import failed (%s); exposing placeholder", exc)

    AwsRuntime = None  # type: ignore[assignment]
    AwsWorkerSettings = None  # type: ignore[assignment]
    serve_worker = None  # type: ignore[assignment]

    class CampaignWorker:  # type: ignore[no-redef]
        """Placeholder until backend.common.worker_base is importable."""

        service = "campaigns"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                "CampaignWorker is a placeholder until backend.common.worker_base "
                f"is available; original import failed with: {_IMPORT_ERROR!r}"
            )

        def handle(self, envelope: Any) -> dict[str, Any]:  # pragma: no cover
            raise NotImplementedError


def main() -> None:
    """Module entrypoint — ``python -m backend.campaigns.worker``."""
    if _IMPORT_ERROR is not None or serve_worker is None:
        raise NotImplementedError(
            "backend.common.worker_base is not available yet; cannot serve. "
            f"Original import error: {_IMPORT_ERROR!r}"
        )
    settings = AwsWorkerSettings.from_env(  # type: ignore[union-attr]
        "campaigns", "SQS_CAMPAIGNS_QUEUE_URL"
    )
    runtime = AwsRuntime(settings)  # type: ignore[misc]
    serve_worker(CampaignWorker(runtime))  # type: ignore[misc]


if __name__ == "__main__":
    main()
