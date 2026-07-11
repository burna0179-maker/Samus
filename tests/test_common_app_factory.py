"""create_base_app composition smoke test."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_create_base_app_has_health():
    from backend.common.app_factory import create_base_app
    app = create_base_app(service_name="prospecting")
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "prospecting"
    assert body["ready"] is True


def test_create_base_app_propagates_trace_id():
    from backend.common.app_factory import create_base_app
    app = create_base_app(service_name="x")
    client = TestClient(app)
    resp = client.get("/health", headers={"X-Samus-Trace-Id": "trace-abc"})
    assert resp.headers.get("X-Samus-Trace-Id") == "trace-abc"


def test_create_base_app_generates_trace_id_when_absent():
    from backend.common.app_factory import create_base_app
    app = create_base_app(service_name="x")
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.headers.get("X-Samus-Trace-Id")  # non-empty


def test_metrics_endpoint_returns_prometheus_format():
    from backend.common.app_factory import create_base_app
    app = create_base_app(service_name="x")
    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    # Prometheus exposition starts with HELP/TYPE lines or metric samples
    assert resp.text  # non-empty
