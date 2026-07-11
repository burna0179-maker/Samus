"""S4 + S5 — body-size limit + security headers on the shared app factory.

Both middlewares are wired in ``create_base_app`` so every workcell inherits
them. HMAC is disabled in the test process (conftest sets
``SAMUS_DISABLE_HMAC_MIDDLEWARE=1``) so these tests exercise the body cap +
header injection directly against a built app.
"""
from __future__ import annotations

import pytest
from fastapi import Request
from starlette.testclient import TestClient

from backend.common.app_factory import create_base_app
from backend.common.body_size_limit import BodySizeLimitMiddleware
from backend.common.security_headers import SECURITY_HEADERS, SecurityHeadersMiddleware


@pytest.fixture
def app():
    app = create_base_app(service_name="crm", add_hmac_middleware=False)

    @app.post("/echo")
    async def echo(request: Request):
        body = await request.body()
        return {"len": len(body)}

    return app


# ---------------- S5 security headers ----------------

def test_security_headers_present_on_health(app) -> None:
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    for name in SECURITY_HEADERS:
        assert name in resp.headers, f"missing {name}"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "max-age=" in resp.headers["Strict-Transport-Security"]


def test_security_headers_setdefault_preserves_handler_value() -> None:
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def handler(request):  # noqa: ANN001
        return JSONResponse({}, headers={"X-Frame-Options": "SAMEORIGIN"})

    a = Starlette(routes=[Route("/h", handler)])
    a.add_middleware(SecurityHeadersMiddleware)
    client = TestClient(a)
    resp = client.get("/h")
    # Handler-set value is preserved (setdefault), other headers filled in.
    assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


# ---------------- S4 body-size limit ----------------

def test_small_body_passes(app) -> None:
    client = TestClient(app)
    resp = client.post("/echo", content=b"x" * 100)
    assert resp.status_code == 200
    assert resp.json()["len"] == 100


def test_oversized_content_length_fast_rejected(app, monkeypatch) -> None:
    monkeypatch.setenv("SAMUS_MAX_REQUEST_BODY_BYTES", "256")
    client = TestClient(app)
    # Declared Content-Length over the cap -> 413 before the body is read.
    resp = client.post(
        "/echo",
        content=b"z" * 1024,
        headers={"content-length": "1024"},
    )
    assert resp.status_code == 413
    assert resp.json()["error"] == "request_body_too_large"


def test_zero_cap_opts_out(monkeypatch) -> None:
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def handler(request):  # noqa: ANN001
        body = await request.body()
        return JSONResponse({"len": len(body)})

    monkeypatch.setenv("SAMUS_MAX_REQUEST_BODY_BYTES", "0")
    a = Starlette(routes=[Route("/e", handler, methods=["POST"])])
    a.add_middleware(BodySizeLimitMiddleware)
    client = TestClient(a)
    resp = client.post("/e", content=b"q" * 5000)
    assert resp.status_code == 200
    assert resp.json()["len"] == 5000
