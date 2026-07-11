"""SQS dispatch path: builds a ``QueueEnvelope`` and sends to the per-service
queue URL from settings. Per doc §3.25 + §4.

``QUEUE_URLS`` is captured at module import time from
``get_settings().sqs_queue_urls``. Tests that mutate settings should call
``reload_queue_urls()`` (provided here) before exercising dispatch.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.common.aws import sqs_client
from backend.common.queue_contracts import QueueEnvelope
from backend.common.queue_signing import sign_envelope
from backend.common.settings import get_settings

_LOG = logging.getLogger("samus.gateway.sqs_dispatch")


def _load_queue_urls() -> dict[str, str]:
    return dict(get_settings().sqs_queue_urls or {})


# Module-level snapshot — loaded at import time per spec.
QUEUE_URLS: dict[str, str] = _load_queue_urls()


def reload_queue_urls() -> dict[str, str]:
    """Re-populate ``QUEUE_URLS`` from current settings. Used by tests."""
    QUEUE_URLS.clear()
    QUEUE_URLS.update(_load_queue_urls())
    return QUEUE_URLS


def enqueue_dispatch(
    service: str,
    *,
    task_id: str,
    action: str,
    payload: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    trace_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Build a ``QueueEnvelope`` and ``send_message`` it to the per-service SQS queue.

    Returns ``{"queued": True, "message_id": ..., "service": ..., "task_id": ...}``.
    Raises ``ValueError`` if no queue is configured for ``service``.
    """
    queue_url = QUEUE_URLS.get(service)
    if not queue_url:
        raise ValueError(f"no SQS queue configured for service={service!r}")

    envelope = QueueEnvelope(
        task_id=task_id,
        service=service,
        action=action,
        trace_id=trace_id,
        idempotency_key=idempotency_key,
        payload=payload or {},
        metadata=metadata or {},
    )
    # FIN-03: sign the envelope so the consuming worker can prove this message
    # came from a holder of the inter-service HMAC key, not merely from someone
    # with sqs:SendMessage IAM. Best-effort — a key-less local stack signs
    # nothing; the consumer's SAMUS_SQS_REQUIRE_HMAC gate is the fail-closed
    # control. Sign BEFORE serialization so the hmac field rides in the body.
    sign_envelope(envelope)
    body = envelope.model_dump_json()
    response = sqs_client().send_message(QueueUrl=queue_url, MessageBody=body)
    message_id = response.get("MessageId", "")

    _LOG.info(
        "sqs_dispatch_queued",
        extra={"service": service, "task_id": task_id, "action": action, "message_id": message_id},
    )
    return {
        "queued": True,
        "message_id": message_id,
        "service": service,
        "task_id": task_id,
    }
