"""G11 daily reset — when UTC date rolls, today's bucket zeros out."""

from __future__ import annotations

import calendar

from backend.common.apollo_budget import ApolloBudgetStore


def _epoch(y: int, m: int, d: int, h: int = 12) -> float:
    return calendar.timegm((y, m, d, h, 0, 0, 0, 0, 0))


def _store(tmp_path, now):
    return ApolloBudgetStore(
        ddb_table=None,
        json_path=str(tmp_path / "apollo.json"),
        now_func=now,
        daily_cap_usd=lambda: 10.0,
    )


def test_date_roll_resets_today(tmp_path):
    holder = {"t": _epoch(2026, 5, 30, 12)}

    def now() -> float:
        return holder["t"]

    s = _store(tmp_path, now)
    s.record_spend(0.50, endpoint="people_search")
    assert s.current_spend_usd() == 0.50
    assert s.snapshot().bucket_day == "2026-05-30"

    # Roll the clock to the next UTC day. The 5s cache TTL would normally
    # mask it, so advance well past TTL.
    holder["t"] = _epoch(2026, 5, 31, 12)

    snap = s.snapshot()
    assert snap.bucket_day == "2026-05-31"
    assert snap.dollars_today == 0.0
    assert snap.call_count_today == 0
    assert snap.recent_calls == []


def test_same_day_within_cache_window_no_reset(tmp_path):
    holder = {"t": _epoch(2026, 5, 30, 12)}

    def now() -> float:
        return holder["t"]

    s = _store(tmp_path, now)
    s.record_spend(0.50, endpoint="people_search")
    holder["t"] += 1.0
    assert s.current_spend_usd() == 0.50
