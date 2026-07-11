"""Tests for backend.common.retry — CircuitState + retry_request.

Async tests are wrapped in synchronous defs via ``asyncio.run`` so the suite
doesn't need pytest-asyncio (not currently in requirements.lock).
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from backend.common import retry
from backend.common.retry import (
    CIRCUITS,
    CircuitState,
    _get_circuit,
    reset_circuits,
    retry_request,
)


# --- pytest fixtures --------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_circuits_and_fast_sleep(monkeypatch):
    """Reset circuit state per-test and short-circuit asyncio.sleep."""
    reset_circuits()

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(retry.asyncio, "sleep", _no_sleep)
    yield
    reset_circuits()


# --- CircuitState unit tests ------------------------------------------------


def test_circuit_state_closed_by_default():
    c = CircuitState()
    assert c.failures == 0
    assert c.open_until == 0.0
    assert c.can_call() is True


def test_record_failure_increments_until_threshold_opens_circuit():
    c = CircuitState(threshold=3, recovery_seconds=30.0)
    c.record_failure()
    c.record_failure()
    assert c.failures == 2
    assert c.open_until == 0.0
    assert c.can_call() is True
    c.record_failure()
    assert c.failures == 3
    assert c.open_until > time.monotonic()
    assert c.can_call() is False


def test_record_success_resets_failures_and_open_until():
    c = CircuitState(threshold=2)
    c.record_failure()
    c.record_failure()
    assert c.failures == 2
    assert c.open_until > 0.0
    c.record_success()
    assert c.failures == 0
    assert c.open_until == 0.0
    assert c.can_call() is True


def test_can_call_returns_false_when_open_until_in_future():
    c = CircuitState()
    c.open_until = time.monotonic() + 60.0
    assert c.can_call() is False


def test_can_call_returns_true_after_recovery_window():
    c = CircuitState()
    c.open_until = time.monotonic() - 1.0
    assert c.can_call() is True


# --- retry_request tests ----------------------------------------------------


def test_retry_request_succeeds_first_try_records_success():
    target = "https://upstream.test/work"

    async def fake_call():
        return {"ok": True}

    result = asyncio.run(retry_request(fake_call, target))
    assert result == {"ok": True}
    assert CIRCUITS[target].failures == 0
    assert CIRCUITS[target].open_until == 0.0


def test_retry_request_retries_on_timeout_then_succeeds():
    target = "https://flaky.test/work"
    calls = {"n": 0}

    async def flaky_call():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.TimeoutException("slow")
        return {"ok": True}

    result = asyncio.run(retry_request(flaky_call, target, max_retries=3))
    assert result == {"ok": True}
    assert calls["n"] == 3
    # Two failures were recorded, then a success reset the counter.
    assert CIRCUITS[target].failures == 0
    assert CIRCUITS[target].open_until == 0.0


def test_retry_request_raises_when_circuit_open():
    target = "https://broken.test/work"
    circuit = _get_circuit(target)
    circuit.open_until = time.monotonic() + 30.0

    async def never_called():
        raise AssertionError("client_call should not be invoked when circuit is open")

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        asyncio.run(retry_request(never_called, target))
    assert exc_info.value.response.status_code == 503


def test_retry_request_does_not_retry_when_idempotent_false():
    target = "https://no-retry.test/work"
    calls = {"n": 0}

    async def always_timeout():
        calls["n"] += 1
        raise httpx.TimeoutException("slow")

    with pytest.raises(httpx.TimeoutException):
        asyncio.run(
            retry_request(always_timeout, target, max_retries=3, idempotent=False)
        )
    # With idempotent=False the loop breaks after the first failure.
    assert calls["n"] == 1
    assert CIRCUITS[target].failures == 1


def test_retry_request_non_retryable_exception_records_failure_and_raises():
    """Non-timeout/connect exceptions bypass retry and propagate immediately."""
    target = "https://bad-request.test/work"
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise RuntimeError("non-retryable")

    with pytest.raises(RuntimeError):
        asyncio.run(retry_request(boom, target, max_retries=3))
    assert calls["n"] == 1
    assert CIRCUITS[target].failures == 1


def test_retry_request_exhausts_retries_then_raises_last_timeout():
    target = "https://always-slow.test/work"
    calls = {"n": 0}

    async def always_timeout():
        calls["n"] += 1
        raise httpx.TimeoutException("slow")

    with pytest.raises(httpx.TimeoutException):
        asyncio.run(retry_request(always_timeout, target, max_retries=2))
    # max_retries=2 → 3 attempts (0..2 inclusive).
    assert calls["n"] == 3
    assert CIRCUITS[target].failures == 3
