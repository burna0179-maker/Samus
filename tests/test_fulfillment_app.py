"""TestClient smoke for backend.fulfillment.app."""

from __future__ import annotations


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.fulfillment.logic as logic_mod

    monkeypatch.setattr(logic_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)


def test_work_endpoint(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_FULFILLMENT_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from fastapi.testclient import TestClient
    from backend.fulfillment.app import app

    client = TestClient(app)
    r = client.post(
        "/work",
        json={
            "task_id": "fulfill-1",
            "payload": {"objective": "tidy the inbox", "actions": [{"action": "sort"}]},
            "metadata": {"approvals": []},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_id"] == "fulfill-1"
    assert body["status"] in ("approved", "blocked")
    assert body["execution_graph"]


def test_health(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_FULFILLMENT_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from fastapi.testclient import TestClient
    from backend.fulfillment.app import app

    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "fulfillment"
