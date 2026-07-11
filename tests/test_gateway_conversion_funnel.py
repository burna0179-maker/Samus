"""Gateway ``GET /admin/conversion_funnel`` — operator-facing funnel proxy.

Proxies ``GET /crm/metrics/funnel`` on the samus-crm workcell. Mirrors the
test_gateway_admin_tasks proxy-test pattern: the outbound httpx call is
mocked, and we confirm the proxy returns the raw funnel shape and degrades
to a structured error body on a transport failure.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest


_phase_a_pending = False
_pending_reason = ""

try:
    from backend.common import dlq, governance, autonomy  # noqa: F401

    if not (hasattr(governance, "classify_risk") and hasattr(governance, "approval_decision")):
        _phase_a_pending = True
        _pending_reason = "governance interface incomplete"
    if not hasattr(autonomy, "run_cycle"):
        _phase_a_pending = True
        _pending_reason = "autonomy.run_cycle missing"
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


def _patch_httpx(monkeypatch, response_factory):
    last: dict[str, Any] = {}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, *, params=None, headers=None):
            last["url"] = url
            last["headers"] = dict(headers or {})
            return response_factory(url)

    monkeypatch.setattr(
        "backend.gateway.app.httpx.AsyncClient",
        lambda *a, **kw: _Client(),
    )
    return last


def _canned(status_code: int, payload: Any) -> httpx.Response:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json = MagicMock(return_value=payload)
    resp.text = repr(payload)
    return resp


_FUNNEL_BODY = {
    "stages": {
        "lead": 10,
        "prospect": 6,
        "opportunity": 3,
        "proposal": 2,
        "closed_won": 1,
        "closed_lost": 1,
    },
    "stage_order": ["lead", "prospect", "opportunity", "proposal", "closed_won", "closed_lost"],
    "transitions": [
        {
            "from": "lead",
            "to": "prospect",
            "from_count": 10,
            "to_count": 6,
            "conversion_rate": 0.6,
            "leaked": 4,
        },
    ],
    "overall_conversion_rate": 0.1,
    "total_events": 23,
    "error": None,
}


def test_admin_conversion_funnel_returns_proxied_snapshot(client, monkeypatch):
    last = _patch_httpx(monkeypatch, lambda u: _canned(200, _FUNNEL_BODY))

    resp = client.get("/admin/conversion_funnel")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stages"]["lead"] == 10
    assert body["total_events"] == 23
    assert body["overall_conversion_rate"] == 0.1
    assert last["url"] == "http://crm.internal:8000/crm/metrics/funnel"


def test_admin_conversion_funnel_503_when_crm_url_unset(monkeypatch):
    monkeypatch.setenv("SAMUS_SHARED_HMAC_KEY", "test-hmac-key")
    monkeypatch.delenv("CRM_URL", raising=False)
    monkeypatch.setenv("SAMUS_GATEWAY_URLS", "")
    from backend.common.settings import reload_settings

    reload_settings()
    from fastapi.testclient import TestClient
    from backend.gateway.app import create_app

    c = TestClient(create_app())
    resp = c.get("/admin/conversion_funnel")
    assert resp.status_code == 503
    assert "crm_url_not_configured" in resp.text


def test_admin_conversion_funnel_degrades_on_crm_unreachable(client, monkeypatch):
    def _boom(url):
        raise httpx.ConnectError("connection refused")

    _patch_httpx(monkeypatch, _boom)
    resp = client.get("/admin/conversion_funnel")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stages"] == {}
    assert body["total_events"] == 0
    assert "crm_unreachable" in (body["error"] or "")


def test_admin_conversion_funnel_degrades_on_bad_response(client, monkeypatch):
    def _bad_json(url):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 502
        resp.json = MagicMock(side_effect=ValueError("not json"))
        resp.text = "garbled"
        return resp

    _patch_httpx(monkeypatch, _bad_json)
    resp = client.get("/admin/conversion_funnel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stages"] == {}
    assert "crm_bad_response" in (body["error"] or "")


def test_admin_conversion_funnel_proxy_signs_call(client, monkeypatch):
    """The CRM workcell rejects unsigned reads with
    ``{"error":"hmac_headers_missing"}``. Confirm the proxy attaches all
    five Samus HMAC headers before sending."""
    last = _patch_httpx(monkeypatch, lambda u: _canned(200, _FUNNEL_BODY))
    resp = client.get("/admin/conversion_funnel")
    assert resp.status_code == 200
    headers = last["headers"]
    for key in (
        "X-Samus-Timestamp",
        "X-Samus-Nonce",
        "X-Samus-Signature",
        "X-Samus-Caller",
        "X-Samus-Trace-Id",
    ):
        assert key in headers, f"missing {key} in {sorted(headers)}"
    assert headers["X-Samus-Caller"] == "gateway"
