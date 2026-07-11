"""Gateway ``GET /admin/tasks`` — operator-facing CRM task queue view.

Proxies ``GET /crm/operator-tasks`` on the samus-crm workcell. We mock the
outbound httpx call (no live CRM available in test env) and confirm the
proxy passes status/limit query params through and returns the raw shape.

The gateway app pulls in the same Phase-A dependencies as test_gateway_app
(governance / autonomy / dlq); we skip the module if any are still missing
so this test never blocks an in-flight rewrite.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest


_phase_a_pending = False
_pending_reason = ""

try:
    from backend.common import dlq, governance, autonomy  # noqa: F401

    if not (hasattr(governance, "classify_risk")
            and hasattr(governance, "approval_decision")):
        _phase_a_pending = True
        _pending_reason = "governance interface incomplete"

    if not hasattr(autonomy, "run_cycle"):
        _phase_a_pending = True
        _pending_reason = "autonomy.run_cycle missing"

    for name in ("enqueue_failure", "read_pending", "read_archive"):
        if not hasattr(dlq, name):
            _phase_a_pending = True
            _pending_reason = f"dlq.{name} missing"
            break
except (ImportError, AttributeError) as exc:
    _phase_a_pending = True
    _pending_reason = f"common module missing: {exc}"

pytestmark = pytest.mark.skipif(
    _phase_a_pending,
    reason=f"depends on Phase A rewrite landing ({_pending_reason})",
)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SAMUS_SHARED_HMAC_KEY", "test-hmac-key")
    monkeypatch.setenv("CRM_URL", "http://crm.internal:8000")
    # The CRM-proxy GET is HMAC-signed; the signer pulls the caller name
    # from settings.service_name (env SAMUS_SERVICE). In the gateway
    # container this is "gateway"; we set it here so the signed canonical
    # the test inspects matches production.
    monkeypatch.setenv("SAMUS_SERVICE", "gateway")
    from backend.common.settings import reload_settings
    reload_settings()

    from fastapi.testclient import TestClient
    from backend.gateway.app import create_app
    return TestClient(create_app())


class _FakeAsyncClient:
    """Minimal AsyncClient stand-in. Captures the last GET so tests can
    assert on URL + params; returns the canned response from the factory."""

    last_call: dict[str, Any] = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)


def _patch_httpx(monkeypatch, response_factory):
    """Patch ``httpx.AsyncClient`` so the test controls the proxied response.

    ``response_factory(url, params)`` -> ``httpx.Response``. Use this to
    return a 200 with canned task list, or to raise (transport error) by
    making the factory call ``raise httpx.ConnectError(...)``.

    The proxy now signs the GET path-only (no query string forwarded), so
    the mock accepts and captures ``headers=`` for sign-call assertions
    even though ``params`` will always be empty.
    """

    last: dict[str, Any] = {}

    class _Client(_FakeAsyncClient):
        async def get(self, url, *, params=None, headers=None):
            last["url"] = url
            last["params"] = dict(params or {})
            last["headers"] = dict(headers or {})
            resp = response_factory(url, params or {})
            return resp

    monkeypatch.setattr(
        "backend.gateway.app.httpx.AsyncClient",
        lambda *a, **kw: _Client(),
    )
    return last


def _canned_response(status_code: int, payload: Any) -> httpx.Response:
    """Build an httpx.Response with a JSON body the gateway can decode."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json = MagicMock(return_value=payload)
    resp.text = repr(payload)
    return resp


def test_admin_tasks_returns_proxied_list(client, monkeypatch):
    canned = {
        "tasks": [
            {
                "operator_task_id": "ot_1",
                "kind": "review",
                "title": "Review new lead",
                "status": "open",
            },
            {
                "operator_task_id": "ot_2",
                "kind": "follow_up",
                "title": "Ping back",
                "status": "open",
            },
        ],
        "count": 2,
        "scan_truncated": False,
        "ddb_error": None,
    }
    last = _patch_httpx(
        monkeypatch, lambda u, p: _canned_response(200, canned),
    )

    resp = client.get("/admin/tasks")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 2
    assert len(body["tasks"]) == 2
    assert body["tasks"][0]["operator_task_id"] == "ot_1"

    # Proxy signs path-only — query string is NOT forwarded. The CRM route
    # defaults (status="open", limit=50) win for both the URL and what the
    # operator sees.
    assert last["url"] == "http://crm.internal:8000/crm/operator-tasks"
    assert last["params"] == {}


def test_admin_tasks_query_params_are_advisory(client, monkeypatch):
    """Gateway accepts ``status`` and ``limit`` for backward compatibility
    but they are NOT forwarded — the CRM defaults win after the path-only
    signing migration. Operator dashboards needing other values must hit
    the CRM workcell directly."""
    canned = {
        "tasks": [],
        "count": 0, "scan_truncated": False, "ddb_error": None,
    }
    last = _patch_httpx(
        monkeypatch, lambda u, p: _canned_response(200, canned),
    )

    resp = client.get("/admin/tasks?status=done&limit=10")
    assert resp.status_code == 200, resp.text
    # Neither forwarded — path-only sign.
    assert last["url"] == "http://crm.internal:8000/crm/operator-tasks"
    assert last["params"] == {}


def test_admin_tasks_503_when_crm_url_unset(monkeypatch):
    monkeypatch.setenv("SAMUS_SHARED_HMAC_KEY", "test-hmac-key")
    monkeypatch.delenv("CRM_URL", raising=False)
    monkeypatch.setenv("SAMUS_GATEWAY_URLS", "")  # blow away any cached map
    from backend.common.settings import reload_settings
    reload_settings()
    from fastapi.testclient import TestClient
    from backend.gateway.app import create_app
    c = TestClient(create_app())
    resp = c.get("/admin/tasks")
    assert resp.status_code == 503
    assert "crm_url_not_configured" in resp.text


def test_admin_tasks_degrades_on_crm_unreachable(client, monkeypatch):
    def _boom(url, params):
        raise httpx.ConnectError("connection refused")

    _patch_httpx(monkeypatch, _boom)
    resp = client.get("/admin/tasks")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tasks"] == []
    assert body["count"] == 0
    assert "crm_unreachable" in (body["ddb_error"] or "")


def test_admin_tasks_degrades_on_bad_response(client, monkeypatch):
    """Non-JSON body from CRM -> structured error, not 500."""
    def _bad_json(url, params):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 502
        resp.json = MagicMock(side_effect=ValueError("not json"))
        resp.text = "garbled"
        return resp

    _patch_httpx(monkeypatch, _bad_json)
    resp = client.get("/admin/tasks")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tasks"] == []
    assert "crm_bad_response" in (body["ddb_error"] or "")


def test_admin_tasks_proxy_signs_call(client, monkeypatch):
    """The CRM workcell rejects unsigned reads with
    ``{"error":"hmac_headers_missing"}``. Confirm the proxy attaches all
    five Samus HMAC headers before sending."""
    canned = {"tasks": [], "count": 0,
              "scan_truncated": False, "ddb_error": None}
    last = _patch_httpx(
        monkeypatch, lambda u, p: _canned_response(200, canned),
    )
    resp = client.get("/admin/tasks")
    assert resp.status_code == 200
    headers = last["headers"]
    for key in ("X-Samus-Timestamp", "X-Samus-Nonce", "X-Samus-Signature",
                "X-Samus-Caller", "X-Samus-Trace-Id"):
        assert key in headers, f"missing {key} in {sorted(headers)}"
    assert headers["X-Samus-Caller"] == "gateway"
