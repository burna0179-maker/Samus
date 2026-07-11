"""Operator guidance-triage routes — the drain for never-triaged recommendations.

GET  /api/samus/cognition/guidance                       -> list + summary
POST /api/samus/cognition/guidance/{id}/accept           -> ACCEPTED
POST /api/samus/cognition/guidance/{id}/reject           -> REJECTED

These are the manual triage seam that the OpenAI day-start / EOD / CODB-reasoner
/ gameplan recommendations previously had no HTTP path to. Capability-gated
(``control_tick``); wire-not-arm (accept/reject are deliberate transitions and
touch no effector).
"""

from __future__ import annotations

import pytest


def _seed(rid, recommendation, *, status="proposed"):
    from backend.cognitive.guidance import GuidanceLedger
    from backend.cognitive.guidance_models import GuidanceRecord

    ts = "2026-07-01T09:00:00+00:00"
    GuidanceLedger().append(
        GuidanceRecord(
            recommendation_id=rid,
            briefing_id="daystart-2026-07-01",
            ts=ts,
            updated_ts=ts,
            recommendation=recommendation,
            status=status,
        )
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_SHARED_HMAC_KEY", "test-hmac-key")
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "jsonl")
    from backend.common.settings import reload_settings

    reload_settings()

    from fastapi.testclient import TestClient
    from backend.gateway import sqs_dispatch
    from backend.gateway.app import create_app

    sqs_dispatch.reload_queue_urls()
    return TestClient(create_app())


def test_list_defaults_to_open_backlog(client):
    _seed("g-open", "scale sendgrid", status="proposed")
    _seed("g-done", "already rejected", status="rejected")

    resp = client.get("/api/samus/cognition/guidance")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status_filter"] == "open"
    ids = {i["recommendation_id"] for i in body["items"]}
    assert "g-open" in ids  # non-terminal shows in the backlog
    assert "g-done" not in ids  # terminal is filtered out of "open"
    assert "summary" in body and "by_status" in body["summary"]


def test_list_status_all_and_filter(client):
    _seed("g-open", "scale sendgrid", status="proposed")
    _seed("g-done", "already rejected", status="rejected")

    all_ids = {
        i["recommendation_id"]
        for i in client.get("/api/samus/cognition/guidance?status=all").json()["items"]
    }
    assert {"g-open", "g-done"} <= all_ids

    rejected = client.get("/api/samus/cognition/guidance?status=rejected").json()
    assert [i["recommendation_id"] for i in rejected["items"]] == ["g-done"]


def test_accept_transitions_and_refines_plan(client):
    _seed("g-1", "prioritize plumbing prospects")
    resp = client.post(
        "/api/samus/cognition/guidance/g-1/accept",
        json={"action_plan": ["pull plumbing list", "queue callsheet"]},
    )
    assert resp.status_code == 200, resp.text
    rec = resp.json()["record"]
    assert rec["status"] == "accepted"
    assert rec["action_plan"] == ["pull plumbing list", "queue callsheet"]


def test_reject_records_reason(client):
    _seed("g-2", "buy a superbowl ad")
    resp = client.post(
        "/api/samus/cognition/guidance/g-2/reject",
        json={"reason": "way out of budget"},
    )
    assert resp.status_code == 200, resp.text
    rec = resp.json()["record"]
    assert rec["status"] == "rejected"
    assert rec["outcome"] == "rejected: way out of budget"


def test_accept_unknown_id_is_404(client):
    resp = client.post("/api/samus/cognition/guidance/nope/accept", json={})
    assert resp.status_code == 404


def test_reject_unknown_id_is_404(client):
    resp = client.post("/api/samus/cognition/guidance/nope/reject", json={})
    assert resp.status_code == 404
