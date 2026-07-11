"""Gateway HTTP-fallback dispatcher.

When a target has no SQS queue configured, the gateway POSTs the envelope to
``{base_url}/work`` via the shared signed http client. Network / upstream
failures are routed to the DLQ ledger and a 502 envelope is returned.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from backend.common import dlq
from backend.common.http_client import signed_post_json

_LOG = logging.getLogger("samus.gateway.service")


async def dispatch_to_target(
    base_url: str,
    target: str,
    envelope: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """POST the envelope to the target's ``/work`` endpoint.

    On transport / HTTP failures, the request is recorded in the local DLQ
    ledger via ``dlq.enqueue_failure`` and a ``(502, {...})`` tuple is
    returned so the caller can surface a structured upstream-failure error.
    """
    task_id = str(envelope.get("task_id") or "")
    try:
        response = await signed_post_json(base_url, "/work", envelope, retries=3)
    except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPStatusError) as exc:
        event_id = dlq.enqueue_failure(
            service="gateway",
            task_id=task_id,
            target=target,
            payload=envelope,
            error=f"{exc.__class__.__name__}: {exc}",
            attempt=1,
        )
        _LOG.warning(
            "dispatch_to_target failed",
            extra={"target": target, "task_id": task_id, "dlq_id": event_id},
        )
        return 502, {"detail": "upstream failure", "dlq_id": event_id}

    status = response.status_code
    try:
        body = response.json()
    except ValueError:
        body = {"detail": "non-json response", "text": response.text[:512]}
    if not isinstance(body, dict):
        body = {"value": body}
    return status, body
