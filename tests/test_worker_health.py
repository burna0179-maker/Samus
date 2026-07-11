"""W-3: the Cloud Run worker health server (backend.common.worker_base).

Workers run an SQS poll loop and otherwise never bind a port; Cloud Run
requires every service to listen on ``$PORT`` within the startup window.
``serve_worker`` starts a trivial health server when ``$PORT`` is set and
skips it otherwise, so the local Docker Compose stack is unchanged.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from backend.common import worker_base


def _get(port: int, path: str) -> tuple[int, str]:
    """GET http://127.0.0.1:<port><path>, returning (status, body)."""
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310 — loopback
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


# --- the health server itself ----------------------------------------------


def test_health_server_serves_health_ok():
    server = worker_base.start_health_server(0, "leadgen")
    try:
        status, body = _get(server.server_address[1], "/health")
        assert status == 200
        assert json.loads(body) == {"status": "ok", "service": "leadgen"}
    finally:
        server.shutdown()
        server.server_close()


def test_health_server_serves_root_path():
    # Cloud Run's default HTTP probe may hit "/"; treat it like /health.
    server = worker_base.start_health_server(0, "crm")
    try:
        status, _ = _get(server.server_address[1], "/")
        assert status == 200
    finally:
        server.shutdown()
        server.server_close()


def test_health_server_404_on_unknown_path():
    server = worker_base.start_health_server(0, "seo")
    try:
        status, _ = _get(server.server_address[1], "/not-a-route")
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()


# --- serve_worker integration ----------------------------------------------


class _FakeWorker:
    """Minimal stand-in — serve_worker only touches .service + run_forever()."""

    def __init__(self, service: str = "leadgen") -> None:
        self.service = service
        self.ran = False

    def run_forever(self) -> None:
        self.ran = True


def test_serve_worker_skips_health_server_without_port(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setattr(
        worker_base, "start_health_server",
        lambda *a, **kw: pytest.fail("health server must not start without $PORT"),
    )
    worker = _FakeWorker()
    worker_base.serve_worker(worker)
    assert worker.ran is True


def test_serve_worker_starts_and_stops_health_server_with_port(monkeypatch):
    monkeypatch.setenv("PORT", "8080")
    started: dict = {}

    class _FakeServer:
        def __init__(self) -> None:
            self.shutdown_called = False
            self.closed = False

        def shutdown(self) -> None:
            self.shutdown_called = True

        def server_close(self) -> None:
            self.closed = True

    fake_server = _FakeServer()

    def _fake_start(port, service):
        started["port"] = port
        started["service"] = service
        return fake_server

    monkeypatch.setattr(worker_base, "start_health_server", _fake_start)
    worker = _FakeWorker(service="crm")
    worker_base.serve_worker(worker)

    assert started == {"port": 8080, "service": "crm"}
    assert worker.ran is True
    # The health server is torn down once the poll loop returns.
    assert fake_server.shutdown_called is True
    assert fake_server.closed is True


def test_serve_worker_reraises_on_health_bind_failure(monkeypatch):
    monkeypatch.setenv("PORT", "8080")

    def _boom(port, service):
        raise OSError("address already in use")

    monkeypatch.setattr(worker_base, "start_health_server", _boom)
    worker = _FakeWorker()
    with pytest.raises(OSError, match="address already in use"):
        worker_base.serve_worker(worker)
    # The poll loop never started — the revision would be unhealthy anyway.
    assert worker.ran is False
