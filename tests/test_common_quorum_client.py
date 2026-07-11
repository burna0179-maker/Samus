"""Tests for backend.common.quorum_client."""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import urllib.error
from io import BytesIO
from typing import Any

import pytest

from backend.common import quorum_client as qc


@pytest.fixture(autouse=True)
def _enable_publish(monkeypatch):
    """Default tests to publish-enabled so each test asserts behaviour, not gating."""
    monkeypatch.setenv("SAMUS_QUORUM_PUBLISH_ENABLED", "1")
    qc._reset_for_tests()
    yield
    qc._reset_for_tests()


def _ok_response(payload: dict[str, Any]):
    body = json.dumps(payload).encode()
    resp = BytesIO(body)
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda *a: None
    return resp


def test_publish_when_disabled_returns_false(monkeypatch):
    monkeypatch.setenv("SAMUS_QUORUM_PUBLISH_ENABLED", "0")
    qc._reset_for_tests()
    client = qc.QuorumHubClient(base_url="http://x")
    assert (
        client.publish(
            caller="samus",
            action="t",
            risk_score=0.0,
            approved=True,
            approval_score=1.0,
            threshold=0.5,
            votes=[],
            reason="r",
        )
        is False
    )


def test_publish_success(monkeypatch):
    calls: list[Any] = []

    def fake_urlopen(req, timeout):  # noqa: ARG001
        calls.append(req)
        return _ok_response({"result": {"content": [{"text": "{}"}]}})

    monkeypatch.setattr(qc.urllib.request, "urlopen", fake_urlopen)
    client = qc.QuorumHubClient(base_url="http://hub.test")
    ok = client.publish(
        caller="samus",
        action="efh_veto",
        risk_score=1.0,
        approved=False,
        approval_score=0.0,
        threshold=1.0,
        votes=[{"voter": "efh", "vote": "VETO", "weight": 1.0}],
        reason="test",
    )
    assert ok is True
    assert calls[0].full_url == "http://hub.test/mcp"
    body = json.loads(calls[0].data)
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "governance_publish"
    assert body["params"]["arguments"]["caller"] == "samus"


def test_publish_signs_body_when_hmac_key_set(monkeypatch):
    monkeypatch.setenv("SAMUS_QUORUM_HUB_HMAC_KEY", "deadbeef" * 8)  # 32-byte hex
    captured = {}

    def fake_urlopen(req, timeout):  # noqa: ARG001
        captured["headers"] = dict(req.header_items())
        captured["data"] = req.data
        return _ok_response({"result": {"content": [{"text": "{}"}]}})

    monkeypatch.setattr(qc.urllib.request, "urlopen", fake_urlopen)
    qc._reset_for_tests()
    client = qc.get_quorum_client()
    client.publish(
        caller="samus",
        action="probe",
        risk_score=0.0,
        approved=True,
        approval_score=1.0,
        threshold=0.5,
        votes=[],
        reason="x",
    )
    # urllib normalizes header keys to title case.
    hdr = {k.lower(): v for k, v in captured["headers"].items()}
    assert "x-hub-hmac" in hdr
    key = bytes.fromhex("deadbeef" * 8)
    expected = _hmac.new(key, captured["data"], hashlib.sha256).hexdigest()
    assert hdr["x-hub-hmac"] == expected


def test_publish_fails_open_on_url_error(monkeypatch):
    def boom(req, timeout):  # noqa: ARG001
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(qc.urllib.request, "urlopen", boom)
    client = qc.QuorumHubClient(base_url="http://hub.test")
    assert (
        client.publish(
            caller="samus",
            action="t",
            risk_score=0.0,
            approved=True,
            approval_score=1.0,
            threshold=0.5,
            votes=[],
            reason="r",
        )
        is False
    )


def test_publish_fails_open_on_hub_error_payload(monkeypatch):
    def err(req, timeout):  # noqa: ARG001
        return _ok_response({"error": {"code": -32000, "message": "denied"}})

    monkeypatch.setattr(qc.urllib.request, "urlopen", err)
    client = qc.QuorumHubClient(base_url="http://hub.test")
    assert (
        client.publish(
            caller="samus",
            action="t",
            risk_score=0.0,
            approved=True,
            approval_score=1.0,
            threshold=0.5,
            votes=[],
            reason="r",
        )
        is False
    )


def test_recent_returns_empty_on_failure(monkeypatch):
    monkeypatch.setattr(
        qc.urllib.request,
        "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("x")),
    )
    client = qc.QuorumHubClient(base_url="http://hub.test")
    assert client.recent() == []


def test_stats_returns_empty_dict_on_failure(monkeypatch):
    monkeypatch.setattr(
        qc.urllib.request,
        "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("x")),
    )
    client = qc.QuorumHubClient(base_url="http://hub.test")
    assert client.stats() == {}


def test_singleton_returns_same_instance():
    qc._reset_for_tests()
    a = qc.get_quorum_client()
    b = qc.get_quorum_client()
    assert a is b


def test_recent_parses_governance_log_payload(monkeypatch):
    events = [{"id": "e1", "caller": "darwin"}, {"id": "e2", "caller": "samus"}]
    payload = {"result": {"content": [{"text": json.dumps({"events": events})}]}}

    def fake(req, timeout):  # noqa: ARG001
        return _ok_response(payload)

    monkeypatch.setattr(qc.urllib.request, "urlopen", fake)
    client = qc.QuorumHubClient(base_url="http://hub.test")
    assert client.recent(limit=2) == events
