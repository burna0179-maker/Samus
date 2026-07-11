"""TestClient smoke for backend.leadgen.app."""
from __future__ import annotations


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.leadgen.service as svc_mod
    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)


def test_health(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_LEADGEN_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from fastapi.testclient import TestClient
    from backend.leadgen.app import app

    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "leadgen"


def test_score_endpoint(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_LEADGEN_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from fastapi.testclient import TestClient
    from backend.leadgen.app import app

    client = TestClient(app)
    r = client.post("/score", json={
        "company": "Acme",
        "domain": "https://acme.com",
        "industry": "finance",
        "employee_count": 75,
        "annual_revenue_usd": 5_000_000,
        "geo": "US",
        "signals": ["manual_ops"],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["normalized_domain"] == "acme.com"
    assert body["tier"] in ("low", "medium", "high", "priority")


def test_work_endpoint_validation_error(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_LEADGEN_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from fastapi.testclient import TestClient
    from backend.leadgen.app import app

    client = TestClient(app)
    r = client.post("/work", json={
        "task_id": "t-1",
        "payload": {"company": "X"},  # missing fields
        "metadata": {},
    })
    assert r.status_code == 422


def test_work_endpoint_ok(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    monkeypatch.setenv("SAMUS_LEADGEN_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from fastapi.testclient import TestClient
    from backend.leadgen.app import app

    client = TestClient(app)
    r = client.post("/work", json={
        "task_id": "t-1",
        "payload": {
            "company": "Acme",
            "domain": "acme.com",
            "industry": "finance",
            "employee_count": 75,
            "annual_revenue_usd": 5_000_000,
            "geo": "US",
            "signals": ["manual_ops"],
        },
        "metadata": {},
    })
    assert r.status_code == 200, r.text
    assert r.json()["company"] == "Acme"
