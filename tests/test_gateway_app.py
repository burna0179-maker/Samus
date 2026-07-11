"""Smoke tests for the gateway FastAPI service.

The app imports modules that are partly rewritten by the main session
(``backend.common.governance``, ``backend.common.autonomy``,
``backend.common.dlq.enqueue_failure / read_pending / read_archive``).
If those interfaces aren't ready, the whole module is skipped.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

_phase_a_pending = False
_pending_reason = ""

try:
    from backend.common import dlq, governance, autonomy  # noqa: F401

    # governance must expose spec-shaped functions
    if not (hasattr(governance, "classify_risk") and hasattr(governance, "approval_decision")):
        _phase_a_pending = True
        _pending_reason = "governance interface incomplete"

    if not hasattr(autonomy, "run_cycle"):
        _phase_a_pending = True
        _pending_reason = "autonomy.run_cycle missing"

    for name in ("enqueue_failure", "read_pending", "read_archive"):
        if not hasattr(dlq, name):
            _phase_a_pending = True
            _pending_reason = f"dlq.{name} missing"
            break
except (ImportError, AttributeError) as exc:
    _phase_a_pending = True
    _pending_reason = f"common module missing: {exc}"

pytestmark = pytest.mark.skipif(
    _phase_a_pending, reason=f"depends on Phase A rewrite landing ({_pending_reason})"
)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SAMUS_SHARED_HMAC_KEY", "test-hmac-key")
    monkeypatch.setenv("LEADGEN_URL", "http://leadgen.internal:8000")
    from backend.common.settings import reload_settings

    reload_settings()

    from fastapi.testclient import TestClient

    from backend.gateway import sqs_dispatch
    from backend.gateway.app import create_app

    sqs_dispatch.reload_queue_urls()
    app = create_app()
    return TestClient(app)


def test_dispatch_unknown_capability_via_404_route(client):
    # ``/dispatch/leadgen`` requires the dispatch capability — gateway is allowed.
    # An unknown HTTP path should 404 (FastAPI default).
    resp = client.post("/dispatch_unknown", json={})
    assert resp.status_code == 404


def test_dispatch_sqs_path(client, monkeypatch):
    """When QUEUE_URLS has the target, the SQS path is used."""
    from backend.gateway import sqs_dispatch

    monkeypatch.setitem(sqs_dispatch.QUEUE_URLS, "leadgen", "https://sqs.example/leadgen")

    fake_sqs = MagicMock()
    fake_sqs.send_message = MagicMock(return_value={"MessageId": "msg-42"})
    monkeypatch.setattr(sqs_dispatch, "sqs_client", lambda: fake_sqs)

    envelope = {
        "task_id": "t-1",
        "payload": {"foo": "bar"},
        "metadata": {"action": "score", "trace_id": "tx"},
    }
    resp = client.post("/dispatch/leadgen", json=envelope)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["queued"] is True
    assert body["service"] == "leadgen"
    assert body["task_id"] == "t-1"
    assert body["message_id"] == "msg-42"


def test_dispatch_503_when_no_hmac_key(monkeypatch):
    from backend.common.settings import reload_settings

    monkeypatch.setenv("SAMUS_SHARED_HMAC_KEY", "")
    monkeypatch.setenv("LEADGEN_URL", "http://leadgen.internal:8000")
    reload_settings()

    from fastapi.testclient import TestClient

    from backend.gateway.app import create_app

    c = TestClient(create_app())
    resp = c.post("/dispatch/leadgen", json={"task_id": "t", "payload": {}, "metadata": {}})
    assert resp.status_code == 503


def test_autonomy_plan_blocks_high_risk_without_approvals(client, monkeypatch):
    """High-risk objective without approvals must return a blocked envelope."""
    from backend.common import governance

    monkeypatch.setattr(
        governance,
        "classify_risk",
        lambda objective, actions: ("high", ["bulk_destructive"]),
    )

    class _Decision:
        approved = False
        risk_level = "high"
        reasons = ["bulk_destructive"]
        required_approvals = ["operator"]

    monkeypatch.setattr(
        governance,
        "approval_decision",
        lambda objective, actions, approvals: _Decision(),
    )

    envelope = {
        "task_id": "plan-1",
        "payload": {"objective": "wipe data", "actions": ["delete_all"]},
        "metadata": {"approvals": []},
    }
    resp = client.post("/autonomy/plan", json=envelope)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("blocked") is True
    assert body["governance"]["approved"] is False
    assert body["governance"]["risk_level"] == "high"


def test_autonomy_plan_runs_cycle_when_approved(client, monkeypatch):
    from backend.common import autonomy, governance

    monkeypatch.setattr(
        governance,
        "classify_risk",
        lambda objective, actions: ("normal", []),
    )

    class _Decision:
        approved = True
        risk_level = "normal"
        reasons = []
        required_approvals = []

    monkeypatch.setattr(
        governance,
        "approval_decision",
        lambda objective, actions, approvals: _Decision(),
    )
    monkeypatch.setattr(
        autonomy,
        "run_cycle",
        lambda task_id, objective, inputs: {"plan": ["step1", "step2"]},
    )

    envelope = {
        "task_id": "plan-2",
        "payload": {"objective": "scan inbox", "actions": ["read"]},
        "metadata": {"approvals": []},
    }
    resp = client.post("/autonomy/plan", json=envelope)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("plan") == ["step1", "step2"]
    assert body["governance"]["approved"] is True


def test_dlq_pending(client, monkeypatch):
    from backend.common import dlq

    monkeypatch.setattr(
        dlq,
        "read_pending",
        lambda service, limit=50: [{"event_id": "e1", "service": service}],
    )

    resp = client.get("/dlq/leadgen?limit=5")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "leadgen"
    assert body["items"][0]["event_id"] == "e1"


def test_dlq_archive(client, monkeypatch):
    from backend.common import dlq

    monkeypatch.setattr(
        dlq,
        "read_archive",
        lambda limit=100: [{"event_id": "arc-1"}],
    )

    resp = client.get("/dlq/archive?limit=3")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"][0]["event_id"] == "arc-1"
