"""Prospecting workcell SQS worker.

Routes the SQS-queue path to the same action handlers the ``/work`` HTTP
endpoint exposes (``backend.prospecting.app``):

  - ``discover`` / ``build_call_sheet``      -> process_discovery pipeline
  - ``analyze_business``                     -> intelligence analysis + scoring
  - ``score_deal``                           -> deal probability + tier
  - ``generate_dynamic_script``              -> adaptive call script
  - ``generate_dynamic_script_with_pivot``   -> adaptive script + pivot

Keeping the worker action set in lock-step with ``/work`` means the gateway
SQS dispatch path and the HTTP fallback path resolve identically — before
this, the queue path dropped every intelligence action with a ValueError
while the HTTP path served it. Wraps ``backend.common.worker_base`` in
try/except so the module imports cleanly even if the base class is unavailable.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.common import feedback_store

from . import deal_scoring, dynamic_script, intelligence
from .models import DiscoveryRequest
from .service import process_discovery

_LOG = logging.getLogger("samus.prospecting.worker")


def _analyze_business(payload: dict[str, Any]) -> dict[str, Any]:
    """Full intelligence analysis — mirrors app._handle_analyze_business."""
    signals = intelligence.analyze_business(payload)
    scores = intelligence.score_opportunity(signals)
    products = intelligence.map_products(scores)
    angle = intelligence.determine_pitch_angle(
        signals,
        scores,
        learned_performance=feedback_store.get_angle_performance(),
    )
    return {
        "signals": signals,
        "scores": scores,
        "products": products,
        "pitch_angle": angle,
    }


def _score_deal(payload: dict[str, Any]) -> dict[str, Any]:
    """Deal probability + tier — mirrors app._handle_score_deal."""
    intel = payload.get("intel", payload)
    engagement = payload.get("engagement") or None
    return deal_scoring.score_deal(intel, engagement)


def _generate_dynamic_script(payload: dict[str, Any]) -> dict[str, Any]:
    """Adaptive call script — mirrors app._handle_generate_dynamic_script."""
    company_name = payload.get("company_name")
    if not company_name or not isinstance(company_name, str):
        raise ValueError("missing_required_field: company_name")
    intel = payload.get("intel")
    if not isinstance(intel, dict):
        raise ValueError("missing_required_field: intel")
    return dynamic_script.generate_script(company_name, intel)


def _generate_dynamic_script_with_pivot(payload: dict[str, Any]) -> dict[str, Any]:
    """Adaptive script + pivot — mirrors app._handle_generate_dynamic_script_with_pivot."""
    company_name = payload.get("company_name")
    if not company_name or not isinstance(company_name, str):
        raise ValueError("missing_required_field: company_name")
    intel = payload.get("intel")
    if not isinstance(intel, dict):
        raise ValueError("missing_required_field: intel")
    return dynamic_script.generate_script_with_pivot(company_name, intel)


_IMPORT_ERROR: Exception | None = None
try:
    from backend.common.aws_runtime import AwsRuntime, AwsWorkerSettings
    from backend.common.worker_base import BaseSqsWorker, serve_worker

    class ProspectingWorker(BaseSqsWorker):
        service = "prospecting"

        def handle(self, envelope: Any) -> dict[str, Any]:
            action = getattr(envelope, "action", "") or "discover"
            payload = getattr(envelope, "payload", None) or {}
            if not isinstance(payload, dict):
                payload = {}
            if action in ("discover", "build_call_sheet"):
                req = DiscoveryRequest.model_validate(payload)
                task_id = getattr(envelope, "task_id", None) or getattr(envelope, "id", None)
                return process_discovery(req, task_id=task_id).model_dump()
            if action == "analyze_business":
                return _analyze_business(payload)
            if action == "score_deal":
                return _score_deal(payload)
            if action == "generate_dynamic_script":
                return _generate_dynamic_script(payload)
            if action == "generate_dynamic_script_with_pivot":
                return _generate_dynamic_script_with_pivot(payload)
            raise ValueError(f"unknown_action: {action}")

except Exception as exc:  # pragma: no cover
    _IMPORT_ERROR = exc
    _LOG.warning("BaseSqsWorker import failed (%s); exposing placeholder", exc)
    AwsRuntime = None  # type: ignore[assignment]
    AwsWorkerSettings = None  # type: ignore[assignment]
    serve_worker = None  # type: ignore[assignment]

    class ProspectingWorker:  # type: ignore[no-redef]
        service = "prospecting"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                f"ProspectingWorker placeholder; worker_base import failed: {_IMPORT_ERROR!r}"
            )

        def handle(self, envelope: Any) -> dict[str, Any]:  # pragma: no cover
            raise NotImplementedError


def main() -> None:
    if _IMPORT_ERROR is not None or serve_worker is None:
        raise NotImplementedError(f"worker_base unavailable; import failed: {_IMPORT_ERROR!r}")
    settings = AwsWorkerSettings.from_env("prospecting", "SQS_PROSPECTING_QUEUE_URL")  # type: ignore[union-attr]
    serve_worker(ProspectingWorker(AwsRuntime(settings)))  # type: ignore[misc]


if __name__ == "__main__":
    main()
