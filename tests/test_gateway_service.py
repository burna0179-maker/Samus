"""Unit tests for backend.gateway.service.dispatch_to_target.

The implementation depends on:
  - backend.common.http_client.signed_post_json (being rewritten by main session)
  - backend.common.dlq.enqueue_failure (being rewritten by main session)

If either name is missing at import time, the whole module is skipped.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

_phase_a_pending = False
_pending_reason = ""

try:
    from backend.common.http_client import signed_post_json  # type: ignore[attr-defined]  # noqa: F401
except (ImportError, AttributeError) as exc:
    _phase_a_pending = True
    _pending_reason = f"signed_post_json missing: {exc}"

try:
    from backend.common import dlq

    if not hasattr(dlq, "enqueue_failure"):
        _phase_a_pending = True
        _pending_reason = "dlq.enqueue_failure missing"
except (ImportError, AttributeError) as exc:
    _phase_a_pending = True
    _pending_reason = f"dlq missing: {exc}"

pytestmark = pytest.mark.skipif(
    _phase_a_pending, reason=f"depends on Phase A rewrite landing ({_pending_reason})"
)


def _run(coro):
    return asyncio.run(coro)


def test_dispatch_to_target_success(monkeypatch):
    from backend.gateway import service as svc

    fake_response = MagicMock(spec=httpx.Response)
    fake_response.status_code = 200
    fake_response.json = MagicMock(return_value={"ok": True, "received": True})

    mock_post = AsyncMock(return_value=fake_response)
    monkeypatch.setattr(svc, "signed_post_json", mock_post)

    status, body = _run(
        svc.dispatch_to_target(
            "http://leadgen.internal:8000",
            "leadgen",
            {"task_id": "t1", "payload": {}, "metadata": {}},
        )
    )

    assert status == 200
    assert body == {"ok": True, "received": True}
    mock_post.assert_awaited_once()


def test_dispatch_to_target_routes_failures_to_dlq(monkeypatch):
    from backend.gateway import service as svc

    async def _boom(*_a, **_k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(svc, "signed_post_json", _boom)
    monkeypatch.setattr(svc.dlq, "enqueue_failure", MagicMock(return_value="dlq-event-1"))

    status, body = _run(
        svc.dispatch_to_target(
            "http://leadgen.internal:8000",
            "leadgen",
            {"task_id": "t2", "payload": {}, "metadata": {}},
        )
    )

    assert status == 502
    assert body["detail"] == "upstream failure"
    assert body["dlq_id"] == "dlq-event-1"
    svc.dlq.enqueue_failure.assert_called_once()
    kwargs = svc.dlq.enqueue_failure.call_args.kwargs
    assert kwargs["service"] == "gateway"
    assert kwargs["task_id"] == "t2"
    assert kwargs["target"] == "leadgen"
