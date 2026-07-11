"""Tests for the SAMUS_APOLLO_CREDIT_CEILING finite-resource guard."""
from __future__ import annotations

import pytest

from backend.common.apollo_budget import (
    ApolloBudgetExceeded,
    ApolloBudgetStore,
)


def _store(*, ceiling: int = 0, cap_usd: float = 100.0) -> ApolloBudgetStore:
    import os
    import tempfile

    d = tempfile.mkdtemp()
    path = os.path.join(d, "budget.json")
    env = {"SAMUS_APOLLO_CREDIT_CEILING": str(ceiling)} if ceiling else {}
    with pytest.MonkeyPatch.context() as mp:
        for k, v in env.items():
            mp.setenv(k, v)
    s = ApolloBudgetStore(
        json_path=path,
        daily_cap_usd=lambda: cap_usd,
    )
    return s


def test_ceiling_zero_means_disabled(monkeypatch):
    monkeypatch.delenv("SAMUS_APOLLO_CREDIT_CEILING", raising=False)
    s = _store(ceiling=0, cap_usd=100.0)
    for _ in range(50):
        s.record_spend(0.01, endpoint="people_search")
    s.assert_allows(0.01)


def test_ceiling_blocks_when_lifetime_reached(monkeypatch):
    monkeypatch.setenv("SAMUS_APOLLO_CREDIT_CEILING", "5")
    s = _store(ceiling=5, cap_usd=100.0)
    for _ in range(5):
        s.record_spend(0.01, endpoint="people_search")
    b = s.snapshot()
    assert b.lifetime_credits == 5
    with pytest.raises(ApolloBudgetExceeded, match="credit_ceiling_hit"):
        s.assert_allows(0.01)


def test_lifetime_survives_daily_reset(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_APOLLO_CREDIT_CEILING", "10")
    day = [1000000.0]
    s = ApolloBudgetStore(
        json_path=str(tmp_path / "budget.json"),
        daily_cap_usd=lambda: 100.0,
        now_func=lambda: day[0],
    )
    for _ in range(3):
        s.record_spend(0.01, endpoint="people_search")
    assert s.snapshot().lifetime_credits == 3
    day[0] += 86400
    s._cache = None  # force reload from disk after day-roll
    s.record_spend(0.01, endpoint="people_search")
    b = s.snapshot()
    assert b.call_count_today == 1
    assert b.lifetime_credits == 4


def test_ceiling_message_includes_instructions(monkeypatch):
    monkeypatch.setenv("SAMUS_APOLLO_CREDIT_CEILING", "1")
    s = _store(ceiling=1, cap_usd=100.0)
    s.record_spend(0.01, endpoint="people_search")
    with pytest.raises(ApolloBudgetExceeded, match="SAMUS_APOLLO_CREDIT_CEILING"):
        s.assert_allows(0.01)
