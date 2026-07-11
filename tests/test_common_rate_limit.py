"""Tests for the in-process inter-workcell rate limiter (finding M7).

The generic test suite runs with ``SAMUS_RATE_LIMIT_ENABLED=0`` (set in
conftest) so endpoint tests post unthrottled. These tests re-enable the
limiter per-test to exercise the limiter contract directly.
"""

from __future__ import annotations

import pytest

from backend.common import rate_limit
from backend.common.rate_limit import check_rate_limit, reset_counters


@pytest.fixture(autouse=True)
def _fresh_counters():
    reset_counters()
    yield
    reset_counters()


def _enable(monkeypatch, **scopes: int) -> None:
    monkeypatch.setenv("SAMUS_RATE_LIMIT_ENABLED", "1")
    for scope, limit in scopes.items():
        monkeypatch.setenv(
            f"SAMUS_RATE_LIMIT_{scope.upper()}_PER_MINUTE",
            str(limit),
        )


def test_disabled_process_wide_always_allows(monkeypatch):
    monkeypatch.setenv("SAMUS_RATE_LIMIT_ENABLED", "0")
    for _ in range(1000):
        assert check_rate_limit("voice_call", "1.2.3.4").allowed


def test_allows_up_to_limit_then_denies(monkeypatch):
    _enable(monkeypatch, voice_call=3)
    # now is pinned so every call lands in the same fixed window.
    now = 1_000_000.0
    decisions = [check_rate_limit("voice_call", "1.2.3.4", now=now) for _ in range(4)]
    assert [d.allowed for d in decisions] == [True, True, True, False]
    breach = decisions[-1]
    assert breach.scope == "voice_call"
    assert breach.limit == 3
    assert breach.retry_after_seconds >= 1


def test_separate_identifiers_have_separate_counters(monkeypatch):
    _enable(monkeypatch, seo_generate=1)
    now = 2_000_000.0
    assert check_rate_limit("seo_generate", "10.0.0.1", now=now).allowed
    # A different caller IP gets its own bucket — not throttled by the first.
    assert check_rate_limit("seo_generate", "10.0.0.2", now=now).allowed
    # The first caller is now over its limit.
    assert not check_rate_limit("seo_generate", "10.0.0.1", now=now).allowed


def test_separate_scopes_have_separate_counters(monkeypatch):
    _enable(monkeypatch, proposal_generate=1, finance_meter_event=1)
    now = 3_000_000.0
    assert check_rate_limit("proposal_generate", "ip", now=now).allowed
    # A different scope for the same caller is independent.
    assert check_rate_limit("finance_meter_event", "ip", now=now).allowed
    assert not check_rate_limit("proposal_generate", "ip", now=now).allowed


def test_window_rolls_over(monkeypatch):
    _enable(monkeypatch, voice_call=1)
    base = 4_000_000.0
    assert check_rate_limit("voice_call", "ip", now=base).allowed
    assert not check_rate_limit("voice_call", "ip", now=base).allowed
    # 60s later -> a new fixed window -> counter resets.
    assert check_rate_limit("voice_call", "ip", now=base + 60).allowed


def test_non_positive_limit_disables_scope(monkeypatch):
    _enable(monkeypatch, voice_call=0)
    for _ in range(100):
        assert check_rate_limit("voice_call", "ip").allowed


def test_default_limit_used_when_scope_unset(monkeypatch):
    monkeypatch.setenv("SAMUS_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("SAMUS_RATE_LIMIT_DEFAULT_PER_MINUTE", "2")
    now = 5_000_000.0
    d = [check_rate_limit("unconfigured_scope", "ip", now=now) for _ in range(3)]
    assert [x.allowed for x in d] == [True, True, False]
    assert d[0].limit == 2


def test_dependency_raises_429_on_breach(monkeypatch):
    """The FastAPI dependency raises HTTPException(429) with Retry-After."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from fastapi import Depends

    _enable(monkeypatch, dep_scope=1)

    app = FastAPI()

    @app.get("/x", dependencies=[Depends(rate_limit.rate_limit_dependency("dep_scope"))])
    async def _x() -> dict[str, str]:
        return {"ok": "yes"}

    client = TestClient(app)
    assert client.get("/x").status_code == 200
    r = client.get("/x")
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    body = r.json()
    assert body["detail"]["error"] == "rate_limited"
    assert body["detail"]["scope"] == "dep_scope"
