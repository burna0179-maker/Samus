"""Gateway ``GET /api/crm/stats`` — Samus HUD daily roll-up.

Composes two sources into one operator-facing snapshot:

  * CRM ``GET /crm/metrics/daily-stats`` — mocked here (no live CRM in
    test env); we assert the proxy is called with today's date and that
    the returned counts surface in the composed body.
  * Outreach audit JSONL — we write a tiny ledger to a temp path and
    point ``SAMUS_OUTREACH_AUDIT_PATH`` at it, then assert the
    ``send_message`` lines with ``status == "completed"`` are counted.

Mirrors the ``test_gateway_admin_tasks`` / ``test_gateway_conversion_funnel``
Phase-A skip / httpx-patch / TestClient harness so a missing dep skips the
whole module rather than red-X'ing the unrelated rewrite.
"""

from __future__ import annotations

import datetime as _dt
import json
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


def _today() -> str:
    return _dt.datetime.utcnow().date().isoformat()


def _canned(status_code: int, payload: Any) -> httpx.Response:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json = MagicMock(return_value=payload)
    resp.text = repr(payload)
    return resp


def _patch_httpx(monkeypatch, response_factory):
    """Patch the gateway's httpx.AsyncClient so tests control the CRM
    response. ``response_factory(url, params)`` -> ``httpx.Response`` or
    raises (for transport errors)."""
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
            last["params"] = params or {}
            last["headers"] = headers or {}
            return response_factory(url, params or {})

    monkeypatch.setattr(
        "backend.gateway.app.httpx.AsyncClient",
        lambda *a, **kw: _Client(),
    )
    return last


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_SHARED_HMAC_KEY", "test-hmac-key")
    monkeypatch.setenv("CRM_URL", "http://crm.internal:8000")
    # The CRM-proxy GET is HMAC-signed; the signer pulls the caller name
    # from settings.service_name (env SAMUS_SERVICE). In the gateway
    # container this is "gateway"; we set it here so the signed canonical
    # the test inspects matches production.
    monkeypatch.setenv("SAMUS_SERVICE", "gateway")
    # Point the outreach audit reader at a tmp file the tests write to
    # individually. Default unset -> the production /opt/samus path.
    monkeypatch.setenv(
        "SAMUS_OUTREACH_AUDIT_PATH",
        str(tmp_path / "outreach_audit.jsonl"),
    )
    from backend.common.settings import reload_settings

    reload_settings()
    # Reset the per-process /api/crm/stats cache so a prior test's body
    # cannot leak into this one. The cache is keyed by UTC date and the
    # whole suite runs inside one day, so the dict must be cleared by hand.
    from backend.gateway import app as gateway_app

    gateway_app._CRM_STATS_CACHE.clear()

    from fastapi.testclient import TestClient

    return TestClient(gateway_app.create_app())


def _write_outreach_jsonl(monkeypatch, lines: list[dict[str, Any]]) -> None:
    import os

    path = os.environ["SAMUS_OUTREACH_AUDIT_PATH"]
    with open(path, "w", encoding="utf-8") as fh:
        for ev in lines:
            fh.write(json.dumps(ev) + "\n")


def test_crm_stats_composes_crm_and_outreach(client, monkeypatch):
    today = _today()

    last = _patch_httpx(
        monkeypatch,
        lambda u, p: _canned(
            200,
            {
                "date": today,
                "calls_today": 5,
                "booked_today": 1,
                "followups_today": 2,
                "scan_truncated": False,
                "ddb_error": None,
            },
        ),
    )

    # 3 completed sends today, 1 failed (excluded), 1 from yesterday (excluded).
    yday = (_dt.date.fromisoformat(today) - _dt.timedelta(days=1)).isoformat()
    _write_outreach_jsonl(
        monkeypatch,
        [
            {
                "ts": f"{today}T09:00:00Z",
                "service": "outreach",
                "action": "send_message",
                "status": "completed",
            },
            {
                "ts": f"{today}T10:00:00Z",
                "service": "outreach",
                "action": "send_message",
                "status": "completed",
            },
            {
                "ts": f"{today}T11:00:00Z",
                "service": "outreach",
                "action": "send_message",
                "status": "completed",
            },
            {
                "ts": f"{today}T12:00:00Z",
                "service": "outreach",
                "action": "send_message",
                "status": "failed",
            },
            {
                "ts": f"{yday}T09:00:00Z",
                "service": "outreach",
                "action": "send_message",
                "status": "completed",
            },
            # Unrelated action — must not count.
            {
                "ts": f"{today}T13:00:00Z",
                "service": "outreach",
                "action": "advance_call",
                "status": "completed",
            },
        ],
    )

    resp = client.get("/api/crm/stats")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["date"] == today
    assert body["calls_today"] == 5
    assert body["booked_today"] == 1
    assert body["followups_today"] == 2
    assert body["emails_today"] == 3
    # 30 / 40 defaults match what the forge-ui fallback already shows.
    assert body["calls_goal"] == 30
    assert body["emails_goal"] == 40
    # connect_rate = booked / calls = 1/5 = 20%
    assert body["connect_rate"] == "20%"
    assert "errors" not in body  # neither source degraded
    # Proxy uses path-only HMAC signing; today rides on neither the URL nor
    # the params — the CRM workcell defaults to its own UTC today, which
    # matches the gateway (both containers are UTC). The cache key + body
    # ``date`` field are how the gateway pins the day.
    assert last["url"] == "http://crm.internal:8000/crm/metrics/daily-stats"


def test_crm_stats_degrades_on_crm_unreachable(client, monkeypatch):
    def _boom(u, p):
        raise httpx.ConnectError("connection refused")

    _patch_httpx(monkeypatch, _boom)
    _write_outreach_jsonl(monkeypatch, [])  # empty outreach ledger

    resp = client.get("/api/crm/stats")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["calls_today"] == 0
    assert body["booked_today"] == 0
    assert body["followups_today"] == 0
    assert body["emails_today"] == 0
    assert body["connect_rate"] == "0%"
    assert "crm_unreachable" in body["errors"]["crm"]


def test_crm_stats_zero_calls_no_div_by_zero(client, monkeypatch):
    today = _today()
    _patch_httpx(
        monkeypatch,
        lambda u, p: _canned(
            200,
            {
                "date": today,
                "calls_today": 0,
                "booked_today": 0,
                "followups_today": 0,
                "scan_truncated": False,
                "ddb_error": None,
            },
        ),
    )
    _write_outreach_jsonl(monkeypatch, [])
    resp = client.get("/api/crm/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["calls_today"] == 0
    assert body["connect_rate"] == "0%"


def test_crm_stats_missing_outreach_file_is_silent(client, monkeypatch):
    """A missing outreach audit ledger is not an error — outreach simply
    hasn't sent anything yet. We must NOT surface a misleading 'outreach'
    error in that case."""
    today = _today()
    _patch_httpx(
        monkeypatch,
        lambda u, p: _canned(
            200,
            {
                "date": today,
                "calls_today": 1,
                "booked_today": 0,
                "followups_today": 0,
                "scan_truncated": False,
                "ddb_error": None,
            },
        ),
    )
    # Intentionally do NOT write the outreach JSONL — the fixture's path
    # points at a tmp file that doesn't exist yet.
    resp = client.get("/api/crm/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["emails_today"] == 0
    assert "errors" not in body or "outreach" not in body.get("errors", {})


def test_crm_stats_respects_goal_env_overrides(client, monkeypatch):
    today = _today()
    monkeypatch.setenv("SAMUS_CRM_CALLS_GOAL", "12")
    monkeypatch.setenv("SAMUS_CRM_EMAILS_GOAL", "25")
    _patch_httpx(
        monkeypatch,
        lambda u, p: _canned(
            200,
            {
                "date": today,
                "calls_today": 2,
                "booked_today": 1,
                "followups_today": 0,
                "scan_truncated": False,
                "ddb_error": None,
            },
        ),
    )
    _write_outreach_jsonl(monkeypatch, [])
    resp = client.get("/api/crm/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["calls_goal"] == 12
    assert body["emails_goal"] == 25


def test_crm_stats_caches_within_ttl(client, monkeypatch):
    """A second hit within 30 s reuses the cached body — the proxy is
    called once, the cache serves the rest."""
    today = _today()
    calls = {"n": 0}

    def _factory(u, p):
        calls["n"] += 1
        return _canned(
            200,
            {
                "date": today,
                "calls_today": calls["n"],
                "booked_today": 0,
                "followups_today": 0,
                "scan_truncated": False,
                "ddb_error": None,
            },
        )

    _patch_httpx(monkeypatch, _factory)
    _write_outreach_jsonl(monkeypatch, [])

    first = client.get("/api/crm/stats").json()
    second = client.get("/api/crm/stats").json()
    assert calls["n"] == 1
    assert first["calls_today"] == 1
    assert second["calls_today"] == 1  # same body served from cache


def test_crm_stats_proxy_signs_call(client, monkeypatch):
    """The CRM workcell rejects unsigned reads with
    ``{"error":"hmac_headers_missing"}``. Confirm the proxy attaches all
    five Samus HMAC headers before sending."""
    today = _today()
    last = _patch_httpx(
        monkeypatch,
        lambda u, p: _canned(
            200,
            {
                "date": today,
                "calls_today": 2,
                "booked_today": 0,
                "followups_today": 0,
                "scan_truncated": False,
                "ddb_error": None,
            },
        ),
    )
    _write_outreach_jsonl(monkeypatch, [])
    resp = client.get("/api/crm/stats")
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


def test_crm_stats_503_when_crm_url_unset(monkeypatch, tmp_path):
    """The composed body still 200s when CRM_URL is unset — we don't 503
    the whole HUD just because one source can't be reached. Instead the
    crm counts are 0 and ``errors.crm`` carries the configuration tag."""
    monkeypatch.setenv("SAMUS_SHARED_HMAC_KEY", "test-hmac-key")
    monkeypatch.delenv("CRM_URL", raising=False)
    monkeypatch.setenv("SAMUS_GATEWAY_URLS", "")
    monkeypatch.setenv(
        "SAMUS_OUTREACH_AUDIT_PATH",
        str(tmp_path / "outreach_audit.jsonl"),
    )
    from backend.common.settings import reload_settings

    reload_settings()
    from backend.gateway import app as gateway_app

    gateway_app._CRM_STATS_CACHE.clear()
    from fastapi.testclient import TestClient

    c = TestClient(gateway_app.create_app())

    resp = c.get("/api/crm/stats")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["calls_today"] == 0
    assert body["errors"]["crm"] == "crm_url_not_configured"
