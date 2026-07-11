"""Unit tests for backend.gateway.sqs_dispatch."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from backend.common.settings import reload_settings
from backend.gateway import sqs_dispatch


def _stub_sqs_client(monkeypatch, response: dict | None = None):
    fake = MagicMock()
    fake.send_message = MagicMock(return_value=response or {"MessageId": "msg-123"})
    monkeypatch.setattr(sqs_dispatch, "sqs_client", lambda: fake)
    return fake


def test_enqueue_dispatch_builds_envelope_and_sends(monkeypatch):
    monkeypatch.setenv("SQS_LEADGEN_QUEUE_URL", "https://sqs.us-west-1/123/leadgen-q")
    reload_settings()
    sqs_dispatch.reload_queue_urls()

    fake = _stub_sqs_client(monkeypatch)

    result = sqs_dispatch.enqueue_dispatch(
        "leadgen",
        task_id="task-abc",
        action="score",
        payload={"items": [1, 2, 3]},
        metadata={"trace_id": "trace-x"},
        trace_id="trace-x",
        idempotency_key="idem-1",
    )

    assert result == {
        "queued": True,
        "message_id": "msg-123",
        "service": "leadgen",
        "task_id": "task-abc",
    }

    fake.send_message.assert_called_once()
    kwargs = fake.send_message.call_args.kwargs
    assert kwargs["QueueUrl"] == "https://sqs.us-west-1/123/leadgen-q"
    body = json.loads(kwargs["MessageBody"])
    assert body["task_id"] == "task-abc"
    assert body["service"] == "leadgen"
    assert body["action"] == "score"
    assert body["trace_id"] == "trace-x"
    assert body["idempotency_key"] == "idem-1"
    assert body["payload"] == {"items": [1, 2, 3]}
    assert body["metadata"] == {"trace_id": "trace-x"}


def test_enqueue_dispatch_no_queue_raises_value_error(monkeypatch):
    monkeypatch.delenv("SQS_LEADGEN_QUEUE_URL", raising=False)
    monkeypatch.delenv("SQS_PROSPECTING_QUEUE_URL", raising=False)
    monkeypatch.delenv("SQS_SCAFFOLD_QUEUE_URL", raising=False)
    monkeypatch.delenv("SQS_FULFILLMENT_QUEUE_URL", raising=False)
    reload_settings()
    sqs_dispatch.reload_queue_urls()

    _stub_sqs_client(monkeypatch)

    with pytest.raises(ValueError):
        sqs_dispatch.enqueue_dispatch(
            "leadgen",
            task_id="task-x",
            action="score",
            payload={},
        )


def test_queue_urls_module_level_dict(monkeypatch):
    """QUEUE_URLS is a dict and reload_queue_urls() refreshes it from settings."""
    monkeypatch.setenv("SQS_PROSPECTING_QUEUE_URL", "https://sqs.example/p")
    reload_settings()
    sqs_dispatch.reload_queue_urls()
    assert "prospecting" in sqs_dispatch.QUEUE_URLS
    assert sqs_dispatch.QUEUE_URLS["prospecting"] == "https://sqs.example/p"
