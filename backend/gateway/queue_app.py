"""Lightweight queue-backed dispatch gateway — doc §4 "Queue Gateway".

A pared-down FastAPI app that only enqueues to SQS. No governance gates, no
DLQ endpoints. Runs as a standalone container alongside the full gateway in
the worker compose stack on port 8080.

Use this for internal callers (the Agent system, local testing) that want to
go straight to the queue without the governance / DLQ surface. Production
traffic goes through ``backend.gateway.app`` instead.

Security (finding H4, 2026-05-20): ``/dispatch/{service}`` enqueues an
arbitrary SQS task envelope for any workcell. It used to be a bare
``FastAPI()`` with NO authentication — anyone who could reach the container
port could dispatch fulfillment, the voice autodialer, or outreach sends.
It now goes through ``create_base_app`` so it inherits the same
``VerifyHMACMiddleware`` every other workcell uses: every request must carry
a valid HMAC signature (``/health`` stays exempt, consistent with the rest
of the mesh). The middleware is default-on; the test suite opts out via the
conftest-set ``SAMUS_DISABLE_HMAC_MIDDLEWARE`` env var.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend.common.app_factory import create_base_app

from . import sqs_dispatch


class DispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None
    idempotency_key: str | None = None


def create_app():
    # ``service_name`` is deliberately NOT "gateway": the main edge gateway is
    # listed in VerifyHMACMiddleware's ``exempt_services`` (it is the signer
    # side and its inbound surface is the bearer-gated operator console). The
    # queue dispatch gateway is a *verifier* — it accepts dispatch requests —
    # so it must NOT inherit that exemption. Using a distinct name keeps the
    # HMAC middleware fully active on every route except /health and /metrics.
    #
    # DEPLOYMENT REQUIREMENT (H4): VerifyHMACMiddleware keys its by-name
    # service exemption off ``settings.service_name`` — i.e. the ``SAMUS_SERVICE``
    # environment variable of the container — NOT the ``service_name`` argument
    # passed here. The container running this app therefore MUST set
    # ``SAMUS_SERVICE`` to something other than ``gateway`` (e.g. ``gateway-queue``)
    # or the middleware would treat it as the signer-side gateway and skip HMAC
    # verification, re-opening this finding. The operator owns that env var in
    # the compose / Cloud Run config.
    #
    # create_base_app already registers GET /health and GET /metrics, so this
    # app no longer defines its own /health. /health stays HMAC-exempt
    # (EXEMPT_PATHS in the middleware), consistent with every other workcell.
    app = create_base_app(service_name="gateway-queue")

    @app.post("/dispatch/{service}")
    async def dispatch(service: str, req: DispatchRequest) -> dict[str, Any]:
        try:
            return sqs_dispatch.enqueue_dispatch(
                service,
                task_id=req.task_id,
                action=req.action,
                payload=req.payload,
                metadata=req.metadata,
                trace_id=req.trace_id,
                idempotency_key=req.idempotency_key,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    return app


app = create_app()
