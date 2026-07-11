"""ApolloBudgetStore — record_spend accumulates, snapshot reflects state, reset wipes.

JSON backend only, deterministic time via injectable now_func.
"""
from __future__ import annotations

import pytest

from backend.common.apollo_budget import (
    ApolloBudgetExceeded,
    ApolloBudgetStore,
)


def _store(tmp_path, *, cap: float = 10.0, now=None) -> ApolloBudgetStore:
    return ApolloBudgetStore(
        ddb_table=None,
        json_path=str(tmp_path / "apollo.json"),
        now_func=now,
        daily_cap_usd=lambda: cap,
    )


def _fixed_now(ts: float):
    holder = {"t": ts}

    def _n() -> float:
        return holder["t"]

    _n.advance = lambda dt: holder.__setitem__("t", holder["t"] + dt)
    return _n


def test_initial_state_zero(tmp_path):
    s = _store(tmp_path)
    assert s.current_spend_usd() == 0.0
    assert s.remaining_usd() == 10.0


def test_record_spend_accumulates(tmp_path):
    s = _store(tmp_path, cap=10.0)
    s.record_spend(0.04, endpoint="people_search")
    s.record_spend(0.32, endpoint="phone_unlock", prospect_id="p1")
    assert s.current_spend_usd() == pytest.approx(0.36, rel=1e-9)
    assert s.remaining_usd() == pytest.approx(9.64, rel=1e-9)
    snap = s.snapshot()
    assert snap.call_count_today == 2
    assert len(snap.recent_calls) == 2
    assert snap.recent_calls[1].endpoint == "phone_unlock"
    assert snap.recent_calls[1].prospect_id == "p1"


def test_reset_today_wipes(tmp_path):
    s = _store(tmp_path, cap=10.0)
    s.record_spend(0.04, endpoint="people_search")
    assert s.current_spend_usd() > 0
    s.reset_today()
    assert s.current_spend_usd() == 0.0
    assert s.snapshot().call_count_today == 0
    assert s.snapshot().recent_calls == []


def test_recent_calls_rolling_window(tmp_path):
    s = _store(tmp_path, cap=100.0)
    for i in range(15):
        s.record_spend(0.04, endpoint="people_search", prospect_id=f"p{i}")
    snap = s.snapshot()
    # Bounded at 10 entries — oldest dropped.
    assert len(snap.recent_calls) == 10
    assert snap.recent_calls[-1].prospect_id == "p14"
    assert snap.recent_calls[0].prospect_id == "p5"


def test_remaining_floors_at_zero(tmp_path):
    s = _store(tmp_path, cap=0.10)
    s.record_spend(0.10, endpoint="people_search")
    assert s.remaining_usd() == 0.0
    # Over-cap attempt raises rather than going negative.
    with pytest.raises(ApolloBudgetExceeded):
        s.record_spend(0.01, endpoint="people_search")
