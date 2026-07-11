"""QueueEnvelope — canonical SQS message contract per doc §3.25.

The prior shell stored a CloudEvents-1.0 envelope in cloudevents.py with different
field shape. This module now defines the doc's QueueEnvelope directly. The
cloudevents.py module is being deprecated.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class QueueEnvelope(BaseModel):
    """Standard SQS message shape for inter-service dispatch."""

    task_id: str = Field(min_length=1)
    service: str = Field(min_length=1)
    action: str = Field(min_length=1)
    trace_id: str | None = None
    idempotency_key: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # FIN-03: per-message HMAC over the envelope's stable content, set by the
    # producer (gateway) via ``queue_signing.sign_envelope`` and verified by
    # ``BaseSqsWorker._process_message``. Optional + default None so unsigned /
    # in-flight legacy messages still validate; enforcement of presence is
    # gated behind ``SAMUS_SQS_REQUIRE_HMAC`` on the consumer (default OFF).
    hmac: str | None = None
    # Optional per-job action-token allotment.  When set, overrides the
    # ACTION_BUDGET_REGISTRY default for this (service, action) pair so callers
    # can grant more or fewer tokens per job without touching the registry.
    # None → worker looks up the registry default.
    action_budget: int | None = None


class QueueDispatchError(RuntimeError):
    """Raised by the gateway when SQS send fails after retry exhaustion."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


__all__ = ["QueueEnvelope", "QueueDispatchError"]
