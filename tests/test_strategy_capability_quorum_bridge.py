"""Tests for `backend.strategy.capability_quorum_bridge`.

Covers the ``governance_publish`` payload shape for capability publish /
withdraw, the HMAC-signing envelope inherited from
:mod:`backend.common.quorum_client`, and the fail-open contract when the
hub is unreachable or publishing is disabled.
"""

from __future__ import annotations

import pytest

from backend.common import quorum_client as qc_mod
from backend.strategy.capability_marketplace import CapabilityListing
from backend.strategy.capability_quorum_bridge import (
    ACTION_PUBLISHED,
    ACTION_WITHDRAWN,
    publish_capability_published,
    publish_capability_withdrawn,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _listing(**overrides) -> CapabilityListing:
    base = {
        "capability_id": "trend_forecasting",
        "provider_agent": "research_agent_7",
        "cost": 10,
        "performance_score": 0.91,
        "latency_ms": 800,
        "tags": ("realtime", "vetted"),
    }
    base.update(overrides)
    return CapabilityListing(**base)


class _CapturingClient:
    """Records every publish call for later assertion."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.return_value = True

    def publish(self, **kwargs) -> bool:
        self.calls.append(kwargs)
        return self.return_value


class _ExplodingClient:
    """Simulates a hub transport error inside publish."""

    def publish(self, **kwargs) -> bool:  # noqa: ARG002
        raise ConnectionError("hub unreachable")


@pytest.fixture(autouse=True)
def _reset_client():
    """Every test starts with a clean singleton and publishing enabled."""
    qc_mod._reset_for_tests()
    yield
    qc_mod._reset_for_tests()


@pytest.fixture
def _publish_enabled(monkeypatch):
    monkeypatch.setenv("SAMUS_QUORUM_PUBLISH_ENABLED", "1")


# ---------------------------------------------------------------------------
# publish_capability_published — payload shape
# ---------------------------------------------------------------------------
def test_publish_capability_published_payload_shape(monkeypatch):
    client = _CapturingClient()
    monkeypatch.setattr(
        "backend.strategy.capability_quorum_bridge.get_quorum_client",
        lambda: client,
    )

    listing = _listing()
    assert publish_capability_published(listing) is True

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["caller"] == "samus"
    assert call["action"] == ACTION_PUBLISHED
    assert call["risk_score"] == 0.0
    assert call["approved"] is True
    assert call["approval_score"] == pytest.approx(0.91)
    assert call["threshold"] == 0.5

    # Votes carry the provider agent as voter with performance-clamped weight.
    assert call["votes"] == [
        {"voter": "research_agent_7", "vote": "OFFER", "weight": pytest.approx(0.91)}
    ]

    # Reason string carries the listing's decisive fields.
    reason = call["reason"]
    assert "capability=trend_forecasting" in reason
    assert "provider=research_agent_7" in reason
    assert "cost=10" in reason
    assert "perf=0.91" in reason
    assert "latency_ms=800" in reason
    assert "tags=realtime,vetted" in reason


def test_publish_capability_published_clamps_performance_over_one(monkeypatch):
    client = _CapturingClient()
    monkeypatch.setattr(
        "backend.strategy.capability_quorum_bridge.get_quorum_client",
        lambda: client,
    )

    listing = _listing(performance_score=1.5)
    publish_capability_published(listing)

    call = client.calls[0]
    assert call["approval_score"] == 1.0
    assert call["votes"][0]["weight"] == 1.0


def test_publish_capability_published_accepts_custom_caller(monkeypatch):
    client = _CapturingClient()
    monkeypatch.setattr(
        "backend.strategy.capability_quorum_bridge.get_quorum_client",
        lambda: client,
    )
    publish_capability_published(_listing(), caller="darwin")
    assert client.calls[0]["caller"] == "darwin"


# ---------------------------------------------------------------------------
# publish_capability_withdrawn — payload shape
# ---------------------------------------------------------------------------
def test_publish_capability_withdrawn_payload_shape(monkeypatch):
    client = _CapturingClient()
    monkeypatch.setattr(
        "backend.strategy.capability_quorum_bridge.get_quorum_client",
        lambda: client,
    )

    assert publish_capability_withdrawn("trend_forecasting", "research_agent_7") is True

    call = client.calls[0]
    assert call["caller"] == "samus"
    assert call["action"] == ACTION_WITHDRAWN
    assert call["risk_score"] == 0.0
    assert call["approved"] is False
    assert call["approval_score"] == 0.0
    assert call["threshold"] == 0.5
    assert call["votes"] == [{"voter": "research_agent_7", "vote": "WITHDRAW", "weight": 1.0}]
    assert call["reason"] == ("capability=trend_forecasting; provider=research_agent_7; withdrawn")


# ---------------------------------------------------------------------------
# Fail-open behaviour
# ---------------------------------------------------------------------------
def test_publish_fails_open_on_hub_transport_error(monkeypatch):
    """A raising client must not propagate — bridge helpers return False."""
    monkeypatch.setattr(
        "backend.strategy.capability_quorum_bridge.get_quorum_client",
        lambda: _ExplodingClient(),
    )
    assert publish_capability_published(_listing()) is False
    assert publish_capability_withdrawn("cap", "p") is False


def test_publish_disabled_by_env_gate_returns_false(monkeypatch):
    """When SAMUS_QUORUM_PUBLISH_ENABLED is unset, the client's own gate
    short-circuits publish() → False. The bridge honours that verbatim."""
    monkeypatch.delenv("SAMUS_QUORUM_PUBLISH_ENABLED", raising=False)
    # Use the real client so we exercise the real gate.
    assert publish_capability_published(_listing()) is False
    assert publish_capability_withdrawn("cap", "p") is False


# ---------------------------------------------------------------------------
# HMAC signing envelope — inherited from quorum_client
# ---------------------------------------------------------------------------
def test_bridge_publish_uses_hmac_when_key_present(monkeypatch, _publish_enabled):
    """When SAMUS_QUORUM_HUB_HMAC_KEY is set, the underlying client signs
    the body with X-Hub-HMAC. The bridge must reach that signing path
    without stripping or bypassing the key."""
    monkeypatch.setenv("SAMUS_QUORUM_HUB_HMAC_KEY", "aa" * 32)  # 64 hex chars

    captured: dict = {}

    def _fake_urlopen(req, timeout):  # noqa: ARG001
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = req.data

        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def read(self_inner, n=-1):  # noqa: ARG002
                # Minimal JSON-RPC success envelope. Accept the cap-arg the
                # quorum client's read_capped() passes in.
                return b'{"result": {"content": [{"text": "{}"}]}}'

        return _Resp()

    monkeypatch.setattr("backend.common.quorum_client.urllib.request.urlopen", _fake_urlopen)

    assert publish_capability_published(_listing()) is True

    # Verify the HMAC header was attached (header keys are title-cased by urllib).
    headers_lower = {k.lower(): v for k, v in captured["headers"].items()}
    assert "x-hub-hmac" in headers_lower
    sig = headers_lower["x-hub-hmac"]
    assert isinstance(sig, str) and len(sig) == 64  # sha256 hex


def test_bridge_publish_omits_hmac_when_no_key(monkeypatch, _publish_enabled):
    """No key → no X-Hub-HMAC header (the underlying transport contract)."""
    monkeypatch.delenv("SAMUS_QUORUM_HUB_HMAC_KEY", raising=False)

    captured: dict = {}

    def _fake_urlopen(req, timeout):  # noqa: ARG001
        captured["headers"] = dict(req.header_items())

        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def read(self_inner, n=-1):  # noqa: ARG002
                return b'{"result": {"content": [{"text": "{}"}]}}'

        return _Resp()

    monkeypatch.setattr("backend.common.quorum_client.urllib.request.urlopen", _fake_urlopen)

    publish_capability_withdrawn("trend_forecasting", "research_agent_7")

    headers_lower = {k.lower(): v for k, v in captured["headers"].items()}
    assert "x-hub-hmac" not in headers_lower
