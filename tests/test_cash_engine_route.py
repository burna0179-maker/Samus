"""Front door — POST /api/samus/review_opportunity (the HMAC-gated ingress)."""

from __future__ import annotations

import pytest

VALID_STAKE = (
    "Alex picked you because Acme Plumbing has the worst homepage I have "
    "seen and it is costing you real calls every single week."
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_SHARED_HMAC_KEY", "test-hmac-key")
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))
    from backend.common.settings import reload_settings

    reload_settings()

    from fastapi.testclient import TestClient
    from backend.gateway import sqs_dispatch
    from backend.gateway.app import create_app

    sqs_dispatch.reload_queue_urls()
    return TestClient(create_app())


def test_invalid_body_is_400(client):
    resp = client.post("/api/samus/review_opportunity", json={})
    assert resp.status_code == 400


def test_no_opportunity_returns_invalid_verdict(client, monkeypatch):
    # No CRM row exists (no AWS in tests) -> the gate blocks on "opportunity".
    # Lazy-boto3 made the DDB lookup raise ModuleNotFoundError instead of
    # returning None silently as before — short-circuit the lookup directly.
    monkeypatch.setattr("backend.crm.service.get_opportunity_for_prospect", lambda _pid: None)
    resp = client.post(
        "/api/samus/review_opportunity",
        json={"prospect_id": "pr-404", "trigger_source": "manual_review"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is False
    assert body["status"] == "invalid"
    assert body["required_protocol"] == "opportunity"


def test_staked_opportunity_enqueues_through_the_door(client, monkeypatch):
    from backend.crm.models import Opportunity

    staked = Opportunity(
        opportunity_id="op-1",
        prospect_id="pr-1",
        stage="proposal",
        stake_sentence=VALID_STAKE,
    )
    monkeypatch.setattr(
        "backend.crm.service.get_opportunity_for_prospect",
        lambda prospect_id: staked,
    )

    resp = client.post(
        "/api/samus/review_opportunity",
        json={
            "prospect_id": "pr-1",
            "trigger_source": "manual_review",
            "current_samus_state": "High Signal, Low Engagement Risk",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert body["status"] == "enqueued"
    assert body["opportunity_id"] == "op-1"
    assert body["queue"] == "mock:jsonl"  # no SQS configured -> mock
    assert body["task_id"].startswith("ce-")

    # The job is durably visible in the mock queue.
    from backend.cash_engine import queue as cash_queue

    jobs = cash_queue.read_mock_jobs()
    assert len(jobs) == 1
    assert jobs[0]["payload"]["prospect_id"] == "pr-1"
    assert jobs[0]["payload"]["stage"] == "audit"


def test_drain_route_is_wired(client):
    # Empty mock queue (fresh tmp state root) -> a clean zeroed summary.
    resp = client.post("/api/samus/cash_engine/drain", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["processed"] == 0
    assert set(body) >= {"processed", "dormant", "escalated", "parked", "running"}


def test_unstaked_opportunity_escalates_through_the_door(client, monkeypatch):
    from backend.crm.models import Opportunity

    unstaked = Opportunity(
        opportunity_id="op-2",
        prospect_id="pr-2",
        stage="proposal",
        stake_sentence="",
    )
    monkeypatch.setattr(
        "backend.crm.service.get_opportunity_for_prospect",
        lambda prospect_id: unstaked,
    )

    resp = client.post(
        "/api/samus/review_opportunity",
        json={"prospect_id": "pr-2", "trigger_source": "signal_decay"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is False
    assert body["status"] == "escalated"
    assert body["required_protocol"] == "stake_sentence"
