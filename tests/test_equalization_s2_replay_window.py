"""S2 — VerifyHMACMiddleware asymmetric replay window regression.

Before the fix the freshness check was symmetric (``abs(now - ts) > window``),
so a future-dated timestamp was accepted up to a full window ahead — extending
the replay window of a captured request. The fix rejects:
  * stale > ``hmac_window_seconds`` (unchanged), AND
  * future > ``_FUTURE_SKEW_SECONDS`` (new).
"""

from __future__ import annotations

import time

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from backend.common import security
from backend.common.middleware import (
    NonceStore,
    VerifyHMACMiddleware,
    _FUTURE_SKEW_SECONDS,
)

_SECRET = "s2-test-secret-key"
_WINDOW = 300


async def _ok(request):  # noqa: ANN001
    return JSONResponse({"ok": True})


def _client(monkeypatch) -> TestClient:
    # Pin the verifier's self-service to a NON-exempt name so it actually
    # verifies (gateway is exempt by name).
    monkeypatch.setenv("SAMUS_SERVICE", "crm")
    # This test isolates the HMAC replay-window from the caller-authz matrix.
    # HOTL Tranche 1 armed SAMUS_AUTHZ_MODE=enforce as the default; the
    # synthetic `caller=leadgen` header below is a signing fixture, not a real
    # cross-workcell grant assertion, so we stand authz down for this test.
    monkeypatch.setenv("SAMUS_AUTHZ_MODE", "off")
    from backend.common.settings import reload_settings

    reload_settings()
    app = Starlette(routes=[Route("/work", _ok, methods=["POST"])])
    app.add_middleware(
        VerifyHMACMiddleware,
        secret=_SECRET,
        window_seconds=_WINDOW,
        nonces=NonceStore(),
        exempt_services=("gateway",),
    )
    return TestClient(app)


def _signed_headers(ts: int, *, caller: str = "leadgen", nonce: str = "n-1") -> dict:
    body = b'{"x":1}'
    sig = security.sign_request(
        _SECRET,
        "POST",
        "/work",
        str(ts),
        nonce,
        body,
        caller=caller,
    )
    return {
        "X-Samus-Timestamp": str(ts),
        "X-Samus-Nonce": nonce,
        "X-Samus-Signature": sig,
        "X-Samus-Caller": caller,
        "Content-Type": "application/json",
    }


def test_current_timestamp_accepted(monkeypatch) -> None:
    client = _client(monkeypatch)
    now = int(time.time())
    resp = client.post("/work", content=b'{"x":1}', headers=_signed_headers(now))
    assert resp.status_code == 200


def test_stale_beyond_window_rejected(monkeypatch) -> None:
    client = _client(monkeypatch)
    stale = int(time.time()) - (_WINDOW + 60)
    resp = client.post("/work", content=b'{"x":1}', headers=_signed_headers(stale))
    assert resp.status_code == 401
    assert resp.json()["error"] == "hmac_timestamp_stale"


def test_future_beyond_skew_rejected(monkeypatch) -> None:
    """The core S2 fix: a far-future ts that the OLD symmetric check accepted."""
    client = _client(monkeypatch)
    # Inside the old symmetric window (well under +300s) but past the new
    # future-skew bound — old code returned 200, fixed code rejects.
    future = int(time.time()) + (_FUTURE_SKEW_SECONDS + 120)
    resp = client.post("/work", content=b'{"x":1}', headers=_signed_headers(future))
    assert resp.status_code == 401
    assert resp.json()["error"] == "hmac_timestamp_future"


def test_small_future_skew_tolerated(monkeypatch) -> None:
    """Sub-skew clock jitter between same-host containers still verifies."""
    client = _client(monkeypatch)
    future = int(time.time()) + max(0, _FUTURE_SKEW_SECONDS - 1)
    resp = client.post("/work", content=b'{"x":1}', headers=_signed_headers(future))
    assert resp.status_code == 200
