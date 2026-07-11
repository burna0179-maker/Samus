"""Inbound deal funnel — Unit L: the leadgen workcell honours its dispatch contract.

leadgen has no in-codebase producer — its consumer is the external Vapi sales
agent ("Morgan SDR" Node 4 in recovery/vapi_sales_agent_config.md), which POSTs
the gateway's /dispatch/leadgen. The gateway routes that to the samus-leadgen
SQS queue -> LeadgenWorker. These tests pin the two Samus-side entry points
against the literal Vapi Node 4 payload so the contract can't silently rot:

  * LeadgenWorker.handle  — the SQS path the gateway dispatch lands on
  * POST /work            — the HTTP-fallback path

Deploying the Vapi agent config itself is an operator step (external to this
repo); these tests prove the Samus side is ready to receive it.
"""

from __future__ import annotations

from types import SimpleNamespace


# The exact payload shape of recovery/vapi_sales_agent_config.md Node 4 — the
# Vapi tool hook fills estimated_size / estimated_revenue with the `|| default`
# fallbacks (5 / 0) when the call hasn't surfaced firmographics yet.
_VAPI_NODE4_PAYLOAD = {
    "company": "Riverside Plumbing",
    "domain": "unknown",
    "industry": "unknown",
    "employee_count": 5,
    "annual_revenue_usd": 0,
    "geo": "US",
    "signals": ["manual_ops", "slow_reporting", "fragmented_tooling"],
}


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.leadgen.service as svc_mod

    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)


def test_worker_handles_score_lead_envelope(tmp_path, monkeypatch):
    """The SQS path the gateway's /dispatch/leadgen lands on: a score_lead
    envelope routes to process_lead and returns a LeadScore dict."""
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_LEADGEN_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    from backend.leadgen.worker import LeadgenWorker

    envelope = SimpleNamespace(
        action="score_lead",
        payload=dict(_VAPI_NODE4_PAYLOAD),
        task_id="call_abc123",
    )
    # handle() does not touch self — exercise it without an AwsRuntime.
    result = LeadgenWorker.handle(object(), envelope)

    assert result["company"] == "Riverside Plumbing"
    assert result["tier"] in ("low", "medium", "high", "priority")
    assert 0 <= result["score"] <= 100
    assert result["segment"] == "micro"  # 5 employees / $0 revenue


def test_worker_default_action_is_score_lead(tmp_path, monkeypatch):
    """An envelope with no action falls back to score_lead (worker contract)."""
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_LEADGEN_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    from backend.leadgen.worker import LeadgenWorker

    envelope = SimpleNamespace(
        action="",
        payload=dict(_VAPI_NODE4_PAYLOAD),
        task_id="call_x",
    )
    result = LeadgenWorker.handle(object(), envelope)
    assert result["company"] == "Riverside Plumbing"


def test_worker_rejects_unknown_action(tmp_path, monkeypatch):
    """An unrecognised action is a hard error, not a silent no-op."""
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_LEADGEN_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    import pytest
    from backend.leadgen.worker import LeadgenWorker

    envelope = SimpleNamespace(
        action="frobnicate",
        payload=dict(_VAPI_NODE4_PAYLOAD),
        task_id="t",
    )
    with pytest.raises(ValueError, match="unknown_action"):
        LeadgenWorker.handle(object(), envelope)


def test_work_endpoint_accepts_vapi_node4_envelope(tmp_path, monkeypatch):
    """The HTTP-fallback path: POST /work with the literal Vapi Node 4 envelope
    scores cleanly — the call_id rides through as the task_id."""
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_LEADGEN_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    from fastapi.testclient import TestClient
    from backend.leadgen.app import app

    client = TestClient(app)
    r = client.post(
        "/work",
        json={
            "task_id": "call_abc123",
            "payload": dict(_VAPI_NODE4_PAYLOAD),
            "metadata": {"action": "score_lead"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["company"] == "Riverside Plumbing"
    assert body["tier"] in ("low", "medium", "high", "priority")
    # Node 5 of the Vapi config branches on tier — every score must map to one.


def test_vapi_node4_payload_is_a_valid_lead_request():
    """The Vapi Node 4 payload validates against leadgen's own LeadRequest —
    the external contract and the model stay pinned together."""
    from backend.leadgen.models import LeadRequest

    req = LeadRequest.model_validate(_VAPI_NODE4_PAYLOAD)
    assert req.company == "Riverside Plumbing"
    assert req.signals == ["manual_ops", "slow_reporting", "fragmented_tooling"]
