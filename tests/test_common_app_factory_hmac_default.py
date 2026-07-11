"""Boundary-contract tests for the default-on HMAC posture in create_base_app.

The audit finding (2026-05-19) was that ``VerifyHMACMiddleware`` was opt-in,
so every workcell that forgot to wire it was reachable without verification.
The fix flipped the default to ON. These tests freeze that default in place:

  - default create_base_app(service_name="x") REJECTS an unsigned POST with 401
  - add_hmac_middleware=False explicitly opts out (warning logged, no 401)
  - hmac_exempt_paths carves out only the listed paths
  - non-development env + empty shared_hmac_key + HMAC on -> RuntimeError at boot

The full pytest session sets ``SAMUS_DISABLE_HMAC_MIDDLEWARE=1`` in
conftest.py so existing unsigned tests still pass. Each test here UN-sets
that env var to exercise the production code path explicitly.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def hmac_on_env(monkeypatch):
    """Un-set the conftest test escape hatch + provide a shared_hmac_key."""
    monkeypatch.delenv("SAMUS_DISABLE_HMAC_MIDDLEWARE", raising=False)
    monkeypatch.setenv("SAMUS_SHARED_HMAC_KEY", "x" * 32)
    monkeypatch.setenv("SAMUS_SERVICE", "prospecting")  # non-signer workcell
    from backend.common.settings import reload_settings

    reload_settings()
    yield
    monkeypatch.setenv("SAMUS_DISABLE_HMAC_MIDDLEWARE", "1")
    reload_settings()


def test_hmac_default_on_rejects_unsigned_request(hmac_on_env):
    """Default create_base_app rejects an unsigned POST with 401."""
    from backend.common.app_factory import create_base_app

    app = create_base_app(service_name="prospecting")
    client = TestClient(app)

    # Add a route the middleware will see (not /health or /metrics).
    @app.post("/probe")
    async def _probe():  # pragma: no cover - exercised via TestClient
        return {"ok": True}

    resp = client.post("/probe", json={})
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"] == "hmac_headers_missing"


def test_hmac_default_on_still_allows_health(hmac_on_env):
    """Health + metrics remain reachable without signature even with HMAC on."""
    from backend.common.app_factory import create_base_app

    app = create_base_app(service_name="prospecting")
    client = TestClient(app)

    resp_h = client.get("/health")
    assert resp_h.status_code == 200
    resp_m = client.get("/metrics")
    assert resp_m.status_code == 200


def test_explicit_opt_out_skips_middleware(hmac_on_env, caplog):
    """add_hmac_middleware=False mounts the app without verification."""
    import logging
    from backend.common.app_factory import create_base_app

    with caplog.at_level(logging.WARNING, logger="samus.app_factory"):
        app = create_base_app(service_name="public_only", add_hmac_middleware=False)
    client = TestClient(app)

    @app.post("/probe")
    async def _probe():  # pragma: no cover
        return {"ok": True}

    resp = client.post("/probe", json={})
    assert resp.status_code == 200
    # Operator must see the warning in boot logs.
    assert any("hmac_disabled" in rec.message for rec in caplog.records), [
        rec.message for rec in caplog.records
    ]


def test_hmac_exempt_paths_carve_out(hmac_on_env):
    """Listed paths bypass HMAC; siblings still require it."""
    from backend.common.app_factory import create_base_app

    app = create_base_app(
        service_name="prospecting",
        hmac_exempt_paths=("/stripe_webhook",),
    )
    client = TestClient(app)

    @app.post("/stripe_webhook")
    async def _stripe():  # pragma: no cover
        return {"ok": True}

    @app.post("/work")
    async def _work():  # pragma: no cover
        return {"ok": True}

    # Exempt path: unsigned 200
    resp_ok = client.post("/stripe_webhook", json={})
    assert resp_ok.status_code == 200
    # Non-exempt path: unsigned 401
    resp_blocked = client.post("/work", json={})
    assert resp_blocked.status_code == 401


def test_fail_closed_when_key_missing_in_production(monkeypatch):
    """Non-development + empty shared_hmac_key + HMAC on -> RuntimeError."""
    monkeypatch.delenv("SAMUS_DISABLE_HMAC_MIDDLEWARE", raising=False)
    monkeypatch.setenv("SAMUS_SHARED_HMAC_KEY", "")
    monkeypatch.setenv("SAMUS_ENV", "production")
    monkeypatch.setenv("SAMUS_SERVICE", "prospecting")
    from backend.common.settings import reload_settings

    reload_settings()

    from backend.common.app_factory import create_base_app

    with pytest.raises(RuntimeError) as ei:
        create_base_app(service_name="prospecting")
    assert "hmac_key_missing" in str(ei.value)

    # Cleanup: restore test-mode env so subsequent tests still see opt-out.
    monkeypatch.setenv("SAMUS_DISABLE_HMAC_MIDDLEWARE", "1")
    monkeypatch.setenv("SAMUS_ENV", "development")
    reload_settings()


def test_fail_closed_skips_gateway(monkeypatch):
    """Gateway workcell boots in production without shared_hmac_key.

    The gateway is the SIGNER-side of the mesh; its inbound surface is the
    operator console, gated by bearer auth (operator_console pack). It does
    not verify incoming HMAC the way other workcells do, so the fail-closed
    check exempts it.
    """
    monkeypatch.delenv("SAMUS_DISABLE_HMAC_MIDDLEWARE", raising=False)
    monkeypatch.setenv("SAMUS_SHARED_HMAC_KEY", "")
    monkeypatch.setenv("SAMUS_ENV", "production")
    monkeypatch.setenv("SAMUS_SERVICE", "gateway")
    from backend.common.settings import reload_settings

    reload_settings()

    from backend.common.app_factory import create_base_app

    # Should NOT raise; gateway is the signer side.
    app = create_base_app(service_name="gateway")
    assert app is not None

    # Cleanup
    monkeypatch.setenv("SAMUS_DISABLE_HMAC_MIDDLEWARE", "1")
    monkeypatch.setenv("SAMUS_ENV", "development")
    reload_settings()
