"""G11 cap enforcement — fail-CLOSED when the cap is hit.

assert_allows must raise BEFORE any HTTP request is made; record_spend
must catch a concurrent racer that snuck past assert_allows.
"""

from __future__ import annotations

import pytest

from backend.common.apollo_budget import (
    ApolloBudgetExceeded,
    ApolloBudgetStore,
)


def _store(tmp_path, *, cap: float) -> ApolloBudgetStore:
    return ApolloBudgetStore(
        ddb_table=None,
        json_path=str(tmp_path / "apollo.json"),
        daily_cap_usd=lambda: cap,
    )


def test_assert_allows_passes_under_cap(tmp_path):
    s = _store(tmp_path, cap=1.00)
    # Nothing spent yet -> $0.50 is fine.
    s.assert_allows(0.50)
    s.record_spend(0.50, endpoint="people_search")
    # $0.49 more is fine (total 0.99 < 1.00).
    s.assert_allows(0.49)


def test_assert_allows_raises_over_cap(tmp_path):
    s = _store(tmp_path, cap=0.10)
    s.record_spend(0.08, endpoint="people_search")
    with pytest.raises(ApolloBudgetExceeded) as exc:
        s.assert_allows(0.05)  # 0.08 + 0.05 = 0.13 > 0.10
    assert "apollo_daily_cap_exceeded" in str(exc.value)


def test_assert_allows_raises_before_call_runs(tmp_path):
    """The whole point of pre-flight: caller never even gets to make the HTTP request."""
    s = _store(tmp_path, cap=0.10)
    s.record_spend(0.10, endpoint="people_search")  # exactly at cap

    calls_made = []

    def fake_apollo_call():
        calls_made.append(1)
        return "result"

    with pytest.raises(ApolloBudgetExceeded):
        s.assert_allows(0.01)
        fake_apollo_call()  # must NOT be reached

    assert calls_made == [], "assert_allows must raise before the call runs"


def test_record_spend_raises_when_breached(tmp_path):
    s = _store(tmp_path, cap=0.10)
    s.record_spend(0.10, endpoint="people_search")
    # Concurrent racer scenario — assert_allows passed but another caller
    # spent in between. record_spend must catch it.
    with pytest.raises(ApolloBudgetExceeded):
        s.record_spend(0.01, endpoint="people_search")
    # State unchanged from the racer's perspective.
    assert s.current_spend_usd() == pytest.approx(0.10, rel=1e-9)


def test_zero_cap_blocks_everything(tmp_path):
    s = _store(tmp_path, cap=0.0)
    with pytest.raises(ApolloBudgetExceeded):
        s.assert_allows(0.01)
