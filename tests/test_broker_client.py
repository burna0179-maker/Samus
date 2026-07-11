"""Unit tests for :mod:`backend.common.broker_client`.

These tests exercise the client surface in isolation — no live broker,
no inter-agent key required. The signing path is exercised when a key
IS present (we set ``SS_HMAC_KEY_SAMUS`` to a 64-char hex via
monkeypatch on the tests that go end-to-end through the envelope).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHARED = _REPO_ROOT / "_shared"
if (_SHARED / "security_client" / "agent_envelope.py").exists():
    if str(_SHARED) not in sys.path:
        sys.path.insert(0, str(_SHARED))


# Deterministic 64-char hex key for tests that exercise envelope signing.
_FAKE_HMAC_KEY = "a" * 64


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Drop the LRU-cached Settings so per-test monkeypatches take effect."""
    from backend.common import config as _config

    _config.get_settings.cache_clear()
    yield
    _config.get_settings.cache_clear()


@pytest.fixture()
def _broker_enabled_with_key(monkeypatch):
    """Enable the broker AND provide an inter-agent HMAC key.

    Also resets the per-process thumbprint + envelope replay cache so
    each test starts clean. ``reset_thumbprint_for_testing`` is the
    documented test-only hook on the shared module.
    """
    monkeypatch.setenv("SAMUS_BROKER_ENABLED", "true")
    monkeypatch.setenv("SAMUS_BROKER_BASE_URL", "https://broker.test.invalid")
    monkeypatch.setenv("SAMUS_BROKER_RESERVE_TIMEOUT_SEC", "1.5")
    monkeypatch.setenv("SAMUS_BROKER_RELEASE_TIMEOUT_SEC", "1.5")
    monkeypatch.setenv("SS_HMAC_KEY_SAMUS", _FAKE_HMAC_KEY)

    # Reset shared state.
    from security_client import agent_envelope as _ae
    from security_client import thumbprint as _tp

    _ae._replay_cache_clear_for_testing()  # noqa: SLF001 — documented test hook
    _tp.reset_thumbprint_for_testing()

    yield

    _ae._replay_cache_clear_for_testing()  # noqa: SLF001
    _tp.reset_thumbprint_for_testing()


@pytest.fixture()
def _broker_dev_disabled(monkeypatch):
    """Enable the broker flag but leave NO inter-agent key set.

    The disable-in-dev-when-no-key path should activate and turn all
    broker calls into local no-ops.
    """
    monkeypatch.setenv("SAMUS_BROKER_ENABLED", "true")
    monkeypatch.setenv("SAMUS_BROKER_DISABLE_IN_DEV_WHEN_NO_KEY", "true")
    monkeypatch.setenv("SAMUS_BROKER_BASE_URL", "https://broker.test.invalid")
    # Explicitly clear inter-agent key envs to force the disable path.
    monkeypatch.delenv("SS_HMAC_KEY_SAMUS", raising=False)
    monkeypatch.delenv("SAMUS_AGENT_HMAC_SECRET", raising=False)


# ---------------------------------------------------------------------------
# Reserve — success path.
# ---------------------------------------------------------------------------

def test_reserve_returns_reservation_on_200(monkeypatch, _broker_enabled_with_key):
    """A 200 response with a valid grant body produces a Reservation."""
    from backend.common import broker_client as bc

    captured: dict = {}

    def _fake_post(url, envelope_wire, *, timeout_sec):
        captured["url"] = url
        captured["envelope"] = envelope_wire
        captured["timeout"] = timeout_sec
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json={
                "reservation_id": "res-abc-123",
                "kind": "llm_tokens",
                "granted_cost": 0.05,
                "expires_at": 9999999999.0,
                "priority": 3,
            },
        )

    monkeypatch.setattr(bc, "_post_signed", _fake_post)

    r = bc.reserve(
        kind="llm_tokens",
        cost=0.05,
        priority=3,
        workcell="prospecting",
        timeout_sec=1.0,
    )

    assert isinstance(r, bc.Reservation)
    assert r.id == "res-abc-123"
    assert r.kind == "llm_tokens"
    assert r.granted_cost == pytest.approx(0.05)
    assert r.priority == 3
    assert captured["url"].endswith("/broker/reserve")
    # Envelope sanity: signed by samus → anita, payload echoes our request.
    env = captured["envelope"]
    assert env["from_agent"] == "samus"
    assert env["to_agent"] == "anita"
    assert env["payload"]["kind"] == "llm_tokens"
    assert env["payload"]["workcell"] == "prospecting"
    assert env["payload"]["priority"] == 3
    assert isinstance(env.get("signature"), str) and env["signature"]


# ---------------------------------------------------------------------------
# Reserve — explicit deny (409).
# ---------------------------------------------------------------------------

def test_reserve_raises_broker_denied_on_409(monkeypatch, _broker_enabled_with_key):
    from backend.common import broker_client as bc

    def _fake_post(url, envelope_wire, *, timeout_sec):
        request = httpx.Request("POST", url)
        return httpx.Response(
            409,
            request=request,
            json={"detail": {"reason": "ecosystem_unhealthy", "retry_after_sec": 12.5}},
        )

    monkeypatch.setattr(bc, "_post_signed", _fake_post)

    with pytest.raises(bc.BrokerDenied) as ei:
        bc.reserve(
            kind="llm_tokens", cost=0.05, priority=5, workcell="prospecting",
        )
    assert ei.value.reason == "ecosystem_unhealthy"
    assert ei.value.retry_after_sec == pytest.approx(12.5)
    assert ei.value.kind == "llm_tokens"


# ---------------------------------------------------------------------------
# Reserve — network failure is fail-closed.
# ---------------------------------------------------------------------------

def test_reserve_treats_network_exception_as_denied(monkeypatch, _broker_enabled_with_key):
    from backend.common import broker_client as bc

    def _boom(url, envelope_wire, *, timeout_sec):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(bc, "_post_signed", _boom)

    with pytest.raises(bc.BrokerDenied) as ei:
        bc.reserve(
            kind="llm_tokens", cost=0.05, priority=5, workcell="prospecting",
        )
    assert ei.value.reason == "broker_unreachable"


def test_reserve_treats_timeout_as_denied(monkeypatch, _broker_enabled_with_key):
    from backend.common import broker_client as bc

    def _slow(url, envelope_wire, *, timeout_sec):
        raise httpx.ReadTimeout("read timed out")

    monkeypatch.setattr(bc, "_post_signed", _slow)

    with pytest.raises(bc.BrokerDenied) as ei:
        bc.reserve(
            kind="llm_tokens", cost=0.05, priority=5, workcell="prospecting",
        )
    assert ei.value.reason == "broker_unreachable"


# ---------------------------------------------------------------------------
# Release — idempotent on failure.
# ---------------------------------------------------------------------------

def test_release_is_idempotent_on_network_failure(monkeypatch, _broker_enabled_with_key):
    from backend.common import broker_client as bc

    def _boom(url, envelope_wire, *, timeout_sec):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(bc, "_post_signed", _boom)

    r = bc.Reservation(
        id="res-xyz", kind="llm_tokens", granted_cost=0.01,
        expires_at=9999999999.0, priority=5,
    )
    # Must NOT raise — release is best-effort, broker auto-reclaims at TTL.
    bc.release(r, actual_cost=0.005, outcome="ok")


def test_release_outcome_error_on_caller_exception(monkeypatch, _broker_enabled_with_key):
    """When the caller raises after grant, release is called with outcome=error.

    This is structurally what happens in llm_client.py's HTTP error path.
    """
    from backend.common import broker_client as bc

    captured: list[dict] = []

    def _fake_post(url, envelope_wire, *, timeout_sec):
        captured.append({"url": url, "payload": envelope_wire["payload"]})
        request = httpx.Request("POST", url)
        return httpx.Response(200, request=request, json={"ok": True})

    monkeypatch.setattr(bc, "_post_signed", _fake_post)

    r = bc.Reservation(
        id="res-err", kind="llm_tokens", granted_cost=0.01,
        expires_at=9999999999.0, priority=5,
    )
    bc.release(r, actual_cost=0.0, outcome="error")

    assert len(captured) == 1
    payload = captured[0]["payload"]
    assert payload["reservation_id"] == "res-err"
    assert payload["actual_cost"] == 0.0
    assert payload["outcome"] == "error"


# ---------------------------------------------------------------------------
# Disabled-broker dev path.
# ---------------------------------------------------------------------------

def test_when_disabled_reserve_returns_dummy_reservation_and_release_is_noop(
    monkeypatch, _broker_dev_disabled,
):
    from backend.common import broker_client as bc

    posts: list = []
    monkeypatch.setattr(
        bc, "_post_signed",
        lambda *a, **kw: posts.append((a, kw)) or pytest.fail("should not call HTTP"),
    )

    r = bc.reserve(
        kind="llm_tokens", cost=0.05, priority=5, workcell="prospecting",
    )
    assert r.id == bc._DISABLED_RESERVATION_ID  # noqa: SLF001
    assert r.granted_cost == pytest.approx(0.05)

    # Release on the sentinel must also be a silent no-op.
    bc.release(r, actual_cost=0.05, outcome="ok")
    assert posts == []


# ---------------------------------------------------------------------------
# Priority mapping.
# ---------------------------------------------------------------------------

def test_priority_for_unknown_workcell_falls_back_to_default():
    from backend.common import broker_client as bc

    # Unknown workcell → default priority (5).
    assert bc.workcell_priority_for("astrology_oracle") == 5
    # Empty string → default.
    assert bc.workcell_priority_for("") == 5
    # Known class match.
    assert bc.workcell_priority_for("prospecting") == 5
    assert bc.workcell_priority_for("fulfillment") == 3
    assert bc.workcell_priority_for("voice") == 4
    # Class-prefix match (e.g. workcell named after a class).
    assert bc.workcell_priority_for("fulfillment_worker") == 3


def test_priority_settings_override_wins(monkeypatch, _reset_settings_cache):
    """When SAMUS_BROKER_PRIORITY_JSON is set, its mapping beats the built-in."""
    from backend.common import broker_client as bc

    monkeypatch.setenv(
        "SAMUS_BROKER_PRIORITY_JSON",
        json.dumps({"fulfillment": 1, "custom_critical": 0}),
    )
    assert bc.workcell_priority_for("fulfillment") == 1
    assert bc.workcell_priority_for("custom_critical") == 0
    # Unknown still falls back to default.
    assert bc.workcell_priority_for("nobody") == 5


# ---------------------------------------------------------------------------
# Reserve malformed-body case (extra defensive coverage of fail-closed).
# ---------------------------------------------------------------------------

def test_reserve_treats_malformed_200_body_as_denied(
    monkeypatch, _broker_enabled_with_key,
):
    from backend.common import broker_client as bc

    def _fake_post(url, envelope_wire, *, timeout_sec):
        request = httpx.Request("POST", url)
        # Missing reservation_id / expires_at -> malformed.
        return httpx.Response(200, request=request, json={"granted_cost": 0.05})

    monkeypatch.setattr(bc, "_post_signed", _fake_post)

    with pytest.raises(bc.BrokerDenied) as ei:
        bc.reserve(
            kind="llm_tokens", cost=0.05, priority=5, workcell="prospecting",
        )
    assert ei.value.reason == "server_error"
