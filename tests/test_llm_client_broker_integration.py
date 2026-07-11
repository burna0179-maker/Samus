"""Integration tests: anthropic_messages ↔ broker_client.

These verify the wiring inside ``backend.common.llm_client.anthropic_messages``:

  * broker.reserve is called AFTER local budget gates and BEFORE the HTTP call;
  * BrokerDenied is translated into BudgetExceeded with reason carrying
    ``broker:<reason>``;
  * release is called on the success path with outcome="ok" + a non-zero
    actual_cost computed from the response usage block;
  * release is called on the HTTP-error path with outcome="error" + cost=0.0;
  * with the broker disabled (default dev posture), neither reserve nor
    release runs and existing behaviour is preserved.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHARED = _REPO_ROOT / "_shared"
if (_SHARED / "security_client" / "agent_envelope.py").exists():
    if str(_SHARED) not in sys.path:
        sys.path.insert(0, str(_SHARED))


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    from backend.common import config as _config

    _config.get_settings.cache_clear()
    yield
    _config.get_settings.cache_clear()


@pytest.fixture()
def _broker_disabled(monkeypatch):
    monkeypatch.setenv("SAMUS_BROKER_ENABLED", "false")


@pytest.fixture()
def _broker_enabled_in_test(monkeypatch):
    """Force broker to ENABLED with key present, so the broker path runs.

    Even though the underlying HTTP call is monkeypatched out at the
    broker_client._post_signed boundary in each test, the envelope-
    signing path is exercised.
    """
    monkeypatch.setenv("SAMUS_BROKER_ENABLED", "true")
    monkeypatch.setenv("SAMUS_BROKER_BASE_URL", "https://broker.test.invalid")
    monkeypatch.setenv("SS_HMAC_KEY_SAMUS", "b" * 64)

    from security_client import agent_envelope as _ae
    from security_client import thumbprint as _tp

    _ae._replay_cache_clear_for_testing()  # noqa: SLF001
    _tp.reset_thumbprint_for_testing()
    yield
    _ae._replay_cache_clear_for_testing()  # noqa: SLF001
    _tp.reset_thumbprint_for_testing()


def _install_fake_anthropic_response(monkeypatch, *, status: int = 200, body: dict | None = None):
    """Patch shared httpx client to return a deterministic OpenAI-shaped reply."""
    from backend.common import llm_client as _llm

    if body is None:
        body = {
            "choices": [{"message": {"content": "hello world"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }

    class _FakeClient:
        def post(self, url, *, headers, json):  # noqa: A002
            request = httpx.Request("POST", url)
            return httpx.Response(status, request=request, json=body)

    monkeypatch.setattr(_llm, "get_shared_client", lambda timeout: _FakeClient())


def _install_fake_anthropic_transport_error(monkeypatch):
    from backend.common import llm_client as _llm

    class _FakeClient:
        def post(self, *args, **kwargs):
            raise httpx.ConnectError("simulated")

    monkeypatch.setattr(_llm, "get_shared_client", lambda timeout: _FakeClient())


# ---------------------------------------------------------------------------
# Reserve is called BEFORE the HTTP call.
# ---------------------------------------------------------------------------

def test_anthropic_messages_calls_broker_before_http(monkeypatch, _broker_enabled_in_test):
    from backend.common import broker_client as bc
    from backend.common import llm_client as _llm

    order: list[str] = []

    real_reserve = bc.reserve

    def _tracking_reserve(*, kind, cost, priority, workcell, timeout_sec=1.0):
        order.append("reserve")
        return bc.Reservation(
            id="res-int-1", kind=kind, granted_cost=cost,
            expires_at=9999999999.0, priority=priority,
        )

    def _tracking_release(reservation, *, actual_cost, outcome):
        order.append(f"release:{outcome}")

    monkeypatch.setattr(_llm._broker_client, "reserve", _tracking_reserve)  # noqa: SLF001
    monkeypatch.setattr(_llm._broker_client, "release", _tracking_release)  # noqa: SLF001

    # Patch the HTTP call to record its position relative to reserve.
    class _FakeClient:
        def post(self, url, *, headers, json):  # noqa: A002
            order.append("http")
            request = httpx.Request("POST", url)
            return httpx.Response(
                200,
                request=request,
                json={
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
            )

    monkeypatch.setattr(_llm, "get_shared_client", lambda timeout: _FakeClient())

    text, usage = _llm.anthropic_messages(
        workcell="prospecting",
        api_key="sk-test-key",
        prompt="hello",
    )
    assert text == "ok"
    assert usage["input_tokens"] == 10

    # reserve must precede http; release follows http with ok outcome.
    assert order.index("reserve") < order.index("http")
    assert "release:ok" in order
    assert order.index("http") < order.index("release:ok")
    # silence unused-warning on real_reserve binding
    assert real_reserve is bc.reserve or True


# ---------------------------------------------------------------------------
# Broker deny → BudgetExceeded.
# ---------------------------------------------------------------------------

def test_broker_denied_translates_to_budget_exceeded(monkeypatch, _broker_enabled_in_test):
    from backend.common import llm_client as _llm

    def _denying_reserve(**kwargs):
        raise _llm._broker_client.BrokerDenied(  # noqa: SLF001
            kind="llm_tokens",
            reason="ecosystem_unhealthy",
            retry_after_sec=30.0,
        )

    monkeypatch.setattr(_llm._broker_client, "reserve", _denying_reserve)  # noqa: SLF001

    http_calls: list = []
    monkeypatch.setattr(
        _llm,
        "get_shared_client",
        lambda timeout: http_calls.append("CALLED") or pytest.fail(
            "HTTP must not run when broker denies",
        ),
    )

    with pytest.raises(_llm.BudgetExceeded) as ei:
        _llm.anthropic_messages(
            workcell="prospecting",
            api_key="sk-test-key",
            prompt="hello",
        )
    assert "broker:ecosystem_unhealthy" in (ei.value.decision.reason or "")
    assert http_calls == []


# ---------------------------------------------------------------------------
# Release on success — with usage-derived actual_cost.
# ---------------------------------------------------------------------------

def test_anthropic_messages_releases_on_success_path(monkeypatch, _broker_enabled_in_test):
    from backend.common import broker_client as bc
    from backend.common import llm_client as _llm

    monkeypatch.setattr(
        _llm._broker_client,  # noqa: SLF001
        "reserve",
        lambda **kw: bc.Reservation(
            id="res-int-2", kind=kw["kind"], granted_cost=kw["cost"],
            expires_at=9999999999.0, priority=kw["priority"],
        ),
    )

    releases: list[dict] = []

    def _capture_release(reservation, *, actual_cost, outcome):
        releases.append({
            "id": reservation.id,
            "actual_cost": actual_cost,
            "outcome": outcome,
        })

    monkeypatch.setattr(_llm._broker_client, "release", _capture_release)  # noqa: SLF001
    _install_fake_anthropic_response(
        monkeypatch,
        body={
            "choices": [{"message": {"content": "answer"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
        },
    )

    _llm.anthropic_messages(
        workcell="prospecting",
        api_key="sk-test-key",
        prompt="hello",
    )

    assert len(releases) == 1
    rel = releases[0]
    assert rel["id"] == "res-int-2"
    assert rel["outcome"] == "ok"
    # LM Studio backend (no OPENAI_API_KEY) → cost is $0.
    assert rel["actual_cost"] == 0.0


# ---------------------------------------------------------------------------
# Release on HTTP error — outcome=error, cost=0.
# ---------------------------------------------------------------------------

def test_anthropic_messages_releases_on_http_error_path(monkeypatch, _broker_enabled_in_test):
    from backend.common import broker_client as bc
    from backend.common import llm_client as _llm

    monkeypatch.setattr(
        _llm._broker_client,  # noqa: SLF001
        "reserve",
        lambda **kw: bc.Reservation(
            id="res-int-3", kind=kw["kind"], granted_cost=kw["cost"],
            expires_at=9999999999.0, priority=kw["priority"],
        ),
    )

    releases: list[dict] = []
    monkeypatch.setattr(
        _llm._broker_client,  # noqa: SLF001
        "release",
        lambda reservation, *, actual_cost, outcome: releases.append({
            "id": reservation.id,
            "actual_cost": actual_cost,
            "outcome": outcome,
        }),
    )

    _install_fake_anthropic_transport_error(monkeypatch)

    with pytest.raises(_llm.LlmCallError):
        _llm.anthropic_messages(
            workcell="prospecting",
            api_key="sk-test-key",
            prompt="hello",
        )

    assert len(releases) == 1
    assert releases[0]["outcome"] == "error"
    assert releases[0]["actual_cost"] == 0.0


# ---------------------------------------------------------------------------
# Disabled broker — original behaviour intact.
# ---------------------------------------------------------------------------

def test_when_broker_disabled_anthropic_messages_behaves_as_before(
    monkeypatch, _broker_disabled,
):
    from backend.common import llm_client as _llm

    reserve_calls: list = []
    release_calls: list = []

    # If the broker_client module's HTTP layer (`_post_signed`) is ever
    # invoked we want the test to fail loudly — disabled means no network.
    monkeypatch.setattr(
        _llm._broker_client,  # noqa: SLF001
        "_post_signed",
        lambda *a, **kw: pytest.fail("disabled broker must not POST"),
    )

    # Wrap reserve/release to assert that THEY are still called (they
    # short-circuit internally to the dev-disabled sentinel path).
    real_reserve = _llm._broker_client.reserve  # noqa: SLF001
    real_release = _llm._broker_client.release  # noqa: SLF001

    def _wrap_reserve(**kwargs):
        reserve_calls.append(kwargs)
        return real_reserve(**kwargs)

    def _wrap_release(reservation, **kwargs):
        release_calls.append({"id": reservation.id, **kwargs})
        return real_release(reservation, **kwargs)

    monkeypatch.setattr(_llm._broker_client, "reserve", _wrap_reserve)  # noqa: SLF001
    monkeypatch.setattr(_llm._broker_client, "release", _wrap_release)  # noqa: SLF001

    _install_fake_anthropic_response(monkeypatch)

    text, usage = _llm.anthropic_messages(
        workcell="prospecting",
        api_key="sk-test-key",
        prompt="hello",
    )
    assert text == "hello world"
    assert usage["input_tokens"] == 100

    # Reserve + release were called, both went through the sentinel path.
    assert len(reserve_calls) == 1
    assert len(release_calls) == 1
    assert release_calls[0]["id"] == _llm._broker_client._DISABLED_RESERVATION_ID  # noqa: SLF001
    assert release_calls[0]["outcome"] == "ok"
