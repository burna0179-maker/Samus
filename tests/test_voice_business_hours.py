"""Tests for backend.voice.business_hours.is_open_now.

Covers: open-now True, after-hours False, Closed-day False, the exact unicode
CSV format, missing/garbage/empty -> None (fail-open), and a row that is open
today vs. a row closed today. All datetimes are fixed + tz-aware.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from backend.voice.business_hours import is_open_now

LA = ZoneInfo("America/Los_Angeles")

# Exact bytes from call_list_2026-06-30.csv (column business_hours):
#   narrow no-break space (U+202F) before AM/PM, thin space (U+2009) flanking
#   the range separator. The real export stored the dash as U+FFFD; we keep a
#   clean en-dash variant too so both code paths are exercised.
CSV_REPLACEMENT_ROW = (
    "Monday: 8:00 AM � 4:00 PM | "
    "Tuesday: 8:00 AM � 4:00 PM | "
    "Wednesday: 8:00 AM � 4:00 PM | "
    "Thursday: 8:00 AM � 4:00 PM | "
    "Friday: 8:00 AM � 4:00 PM | "
    "Saturday: Closed | Sunday: Closed"
)

CSV_ENDASH_ROW = (
    "Monday: 8:00 AM – 8:00 PM | "
    "Tuesday: 8:00 AM – 8:00 PM | "
    "Wednesday: 8:00 AM – 8:00 PM | "
    "Thursday: 8:00 AM – 8:00 PM | "
    "Friday: 8:00 AM – 8:00 PM | "
    "Saturday: 8:00 AM – 8:00 PM | "
    "Sunday: 8:00 AM – 8:00 PM"
)

# Mon 2026-06-29, Wed 2026-07-01, Sat 2026-07-04 in LA.
MON_MIDDAY = datetime(2026, 6, 29, 12, 0, tzinfo=LA)   # weekday() == 0
MON_EARLY = datetime(2026, 6, 29, 6, 30, tzinfo=LA)    # before 8 AM
MON_LATE = datetime(2026, 6, 29, 19, 0, tzinfo=LA)     # after 4 PM
WED_MIDDAY = datetime(2026, 7, 1, 12, 0, tzinfo=LA)    # weekday() == 2
SAT_MIDDAY = datetime(2026, 7, 4, 12, 0, tzinfo=LA)    # weekday() == 5


def test_open_now_true():
    assert is_open_now(CSV_REPLACEMENT_ROW, now_local=MON_MIDDAY) is True


def test_after_hours_false():
    assert is_open_now(CSV_REPLACEMENT_ROW, now_local=MON_LATE) is False


def test_before_hours_false():
    assert is_open_now(CSV_REPLACEMENT_ROW, now_local=MON_EARLY) is False


def test_closed_day_false():
    # Saturday is "Closed" in the replacement-char row.
    assert is_open_now(CSV_REPLACEMENT_ROW, now_local=SAT_MIDDAY) is False


def test_unicode_csv_format_parses():
    # The exact en-dash + narrow/thin-space format parses on a weekday.
    assert is_open_now(CSV_ENDASH_ROW, now_local=WED_MIDDAY) is True


def test_row_open_today_vs_closed_today():
    # Same Saturday instant: en-dash row is open (8-8), replacement row closed.
    assert is_open_now(CSV_ENDASH_ROW, now_local=SAT_MIDDAY) is True
    assert is_open_now(CSV_REPLACEMENT_ROW, now_local=SAT_MIDDAY) is False


def test_boundary_inclusive():
    open_at = datetime(2026, 6, 29, 8, 0, tzinfo=LA)
    close_at = datetime(2026, 6, 29, 16, 0, tzinfo=LA)
    assert is_open_now(CSV_REPLACEMENT_ROW, now_local=open_at) is True
    assert is_open_now(CSV_REPLACEMENT_ROW, now_local=close_at) is True


def test_empty_returns_none():
    assert is_open_now("", now_local=MON_MIDDAY) is None


def test_missing_day_returns_none():
    # Row has no Wednesday entry -> fail-open None on a Wednesday.
    partial = "Monday: 8:00 AM - 5:00 PM | Tuesday: 8:00 AM - 5:00 PM"
    assert is_open_now(partial, now_local=WED_MIDDAY) is None


def test_garbage_returns_none():
    assert is_open_now("not hours at all", now_local=MON_MIDDAY) is None
    assert is_open_now(
        "Monday: banana - pancake", now_local=MON_MIDDAY,
    ) is None


def test_none_input_returns_none():
    assert is_open_now(None, now_local=MON_MIDDAY) is None  # type: ignore[arg-type]


def test_plain_ascii_hyphen_row():
    row = "Monday: 9:00 AM - 5:00 PM | Wednesday: 9:00 AM - 5:00 PM"
    assert is_open_now(row, now_local=MON_MIDDAY) is True
    assert is_open_now(row, now_local=WED_MIDDAY) is True


def test_overnight_window():
    row = "Monday: 8:00 PM - 2:00 AM | Wednesday: 8:00 PM - 2:00 AM"
    late = datetime(2026, 6, 29, 23, 0, tzinfo=LA)   # 11 PM Monday -> open
    midday = datetime(2026, 6, 29, 12, 0, tzinfo=LA)  # noon Monday -> closed
    assert is_open_now(row, now_local=late) is True
    assert is_open_now(row, now_local=midday) is False
