"""TestClient smoke for backend.scaffold.app."""

from __future__ import annotations


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.scaffold.logic as logic_mod

    monkeypatch.setattr(logic_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)


def test_work_endpoint_generates_asset(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_SCAFFOLD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from fastapi.testclient import TestClient
    from backend.scaffold.app import app

    client = TestClient(app)
    r = client.post(
        "/work",
        json={
            "task_id": "t-scaffold-1",
            "payload": {
                "asset_type": "operating_brief",
                "title": "Q3 Operating Brief",
                "client": "Acme",
                "brand_voice": "direct",
                "offer": "Automation Pilot",
                "goals": ["reduce manual ops"],
                "inputs": {"industry": "finance"},
            },
            "metadata": {},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["asset_type"] == "operating_brief"
    assert "document" in body
    assert "Operating Brief" in body["document"]


def test_work_endpoint_invalid_asset_type(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_SCAFFOLD_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from fastapi.testclient import TestClient
    from backend.scaffold.app import app

    client = TestClient(app)
    r = client.post(
        "/work",
        json={
            "task_id": "t-scaffold-2",
            "payload": {
                "asset_type": "not_a_real_type",
                "title": "Bad",
                "client": "Acme",
                "brand_voice": "direct",
                "offer": "Pilot",
            },
            "metadata": {},
        },
    )
    assert r.status_code == 422
