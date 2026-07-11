"""SEO workcell SQS worker.

Routes ``envelope.action`` -> :func:`audit_site` / :func:`optimize_page` /
:func:`generate_content`. Wraps ``backend.common.worker_base`` in try/except
so the module imports cleanly even if the base class is unavailable.
"""

from __future__ import annotations

import logging
from typing import Any

from .models import AuditRequest, ContentRequest, OptimizeRequest
from .service import audit_site, generate_content, optimize_page

_LOG = logging.getLogger("samus.seo.worker")

_IMPORT_ERROR: Exception | None = None
try:
    from backend.common.aws_runtime import AwsRuntime, AwsWorkerSettings
    from backend.common.worker_base import BaseSqsWorker, serve_worker

    class SeoWorker(BaseSqsWorker):
        """SEO workcell worker -- routes by envelope.action."""

        service = "seo"

        def handle(self, envelope: Any) -> dict[str, Any]:
            action = getattr(envelope, "action", "") or "audit_site"
            payload = getattr(envelope, "payload", None) or {}
            if action == "audit_site":
                req = AuditRequest.model_validate(payload)
                return audit_site(req).model_dump()
            if action == "optimize_page":
                req = OptimizeRequest.model_validate(payload)
                return optimize_page(req).model_dump()
            if action == "generate_content":
                req = ContentRequest.model_validate(payload)
                return generate_content(req).model_dump()
            raise ValueError(f"unknown_action: {action}")

except Exception as exc:  # pragma: no cover
    _IMPORT_ERROR = exc
    _LOG.warning("BaseSqsWorker import failed (%s); exposing placeholder SeoWorker", exc)

    AwsRuntime = None  # type: ignore[assignment]
    AwsWorkerSettings = None  # type: ignore[assignment]
    serve_worker = None  # type: ignore[assignment]

    class SeoWorker:  # type: ignore[no-redef]
        """Placeholder until backend.common.worker_base is importable."""

        service = "seo"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                "SeoWorker is a placeholder until backend.common.worker_base is "
                f"available; original import failed with: {_IMPORT_ERROR!r}"
            )

        def handle(self, envelope: Any) -> dict[str, Any]:  # pragma: no cover
            raise NotImplementedError


def main() -> None:
    """Module entrypoint -- ``python -m backend.seo.worker``."""
    if _IMPORT_ERROR is not None or serve_worker is None:
        raise NotImplementedError(
            "backend.common.worker_base is not available yet; cannot serve. "
            f"Original import error: {_IMPORT_ERROR!r}"
        )
    settings = AwsWorkerSettings.from_env("seo", "SQS_SEO_QUEUE_URL")  # type: ignore[union-attr]
    runtime = AwsRuntime(settings)  # type: ignore[misc]
    serve_worker(SeoWorker(runtime))  # type: ignore[misc]


if __name__ == "__main__":
    main()
