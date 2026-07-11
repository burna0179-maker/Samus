"""US state -> tz map + TCPA hours gate."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.common.us_timezones import (
    DEFAULT_CALL_HOURS,
    is_within_call_hours,
    state_to_timezone,
)


def test_state_to_timezone_known_states():
    assert state_to_timezone("CA").key == "America/Los_Angeles"
    assert state_to_timezone("NY").key == "America/New_York"
    assert state_to_timezone("TX").key == "America/Chicago"
    assert state_to_timezone("CO").key == "America/Denver"
    assert state_to_timezone("AZ").key == "America/Phoenix"
    assert state_to_timezone("HI").key == "Pacific/Honolulu"


def test_state_to_timezone_case_insensitive_and_whitespace():
    assert state_to_timezone("  ca ").key == "America/Los_Angeles"
    assert state_to_timezone("Ny").key == "America/New_York"


def test_state_to_timezone_unknown_falls_back_to_pt():
    assert state_to_timezone("").key == "America/Los_Angeles"
    assert state_to_timezone(None).key == "America/Los_Angeles"
    assert state_to_timezone("XX").key == "America/Los_Angeles"
    assert state_to_timezone("not-a-state").key == "America/Los_Angeles"


def test_is_within_call_hours_midday_pt():
    # 19:00 UTC = 12:00 PT
    now = datetime(2026, 5, 15, 19, 0, tzinfo=timezone.utc)
    assert is_within_call_hours(state="CA", now=now)


def test_is_within_call_hours_too_early_pt():
    # 13:00 UTC = 06:00 PT (before 8am)
    now = datetime(2026, 5, 15, 13, 0, tzinfo=timezone.utc)
    assert is_within_call_hours(state="CA", now=now) is False


def test_is_within_call_hours_too_late_pt():
    # 06:00 UTC = 23:00 PT prev day (after 9pm)
    now = datetime(2026, 5, 15, 6, 0, tzinfo=timezone.utc)
    assert is_within_call_hours(state="CA", now=now) is False


def test_is_within_call_hours_per_prospect_tz():
    """Same UTC instant — accepted for CA prospect, rejected for NY prospect."""
    # 03:00 UTC = 20:00 PT (inside) BUT = 23:00 ET (outside)
    now = datetime(2026, 5, 16, 3, 0, tzinfo=timezone.utc)
    assert is_within_call_hours(state="CA", now=now) is True
    assert is_within_call_hours(state="NY", now=now) is False


def test_is_within_call_hours_naive_datetime_treated_as_utc():
    naive = datetime(2026, 5, 15, 19, 0)  # no tzinfo
    # Treated as UTC -> 12:00 PT -> inside
    assert is_within_call_hours(state="CA", now=naive)


def test_is_within_call_hours_custom_hours():
    now = datetime(2026, 5, 15, 14, 0, tzinfo=timezone.utc)  # 07:00 PT
    # Default 8-21 -> outside
    assert is_within_call_hours(state="CA", now=now) is False
    # Custom 6-22 -> inside
    assert is_within_call_hours(state="CA", now=now, hours=(6, 22))


def test_default_call_hours_are_tcpa_8_to_21():
    assert DEFAULT_CALL_HOURS == (8, 21)
