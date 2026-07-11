"""Doc §3.17 — utc_now, iso_now, hours_from_now."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from backend.common.dates import (
    business_date,
    business_today,
    hours_from_now,
    iso_now,
    utc_now,
)


_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_utc_now_is_tz_aware_utc():
    now = utc_now()
    assert isinstance(now, datetime)
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_iso_now_format():
    s = iso_now()
    assert _ISO_RE.match(s), f"unexpected ISO shape: {s}"
    assert len(s) == 20 and s.endswith("Z")
    assert s[4] == "-" and s[10] == "T"


def test_hours_from_now_offsets_future():
    base = utc_now()
    future = hours_from_now(2)
    parsed = datetime.strptime(future, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    delta = parsed - base
    assert timedelta(hours=1, minutes=58) <= delta <= timedelta(hours=2, minutes=2)


def test_hours_from_now_zero_close_to_iso_now():
    a = hours_from_now(0)
    b = iso_now()
    assert a[:13] == b[:13]  # same hour


def test_hours_from_now_accepts_float():
    out = hours_from_now(0.5)
    assert _ISO_RE.match(out)


def test_business_date_pt_evening_stays_on_pacific_day(monkeypatch):
    """A UTC timestamp past PT midnight-rollover buckets to the PACIFIC day.

    2026-07-08T03:40:58Z is 2026-07-07 20:40 PDT — a bare .date() would report
    07-08 (the UTC day), silently rolling the evening onto tomorrow.
    """
    monkeypatch.setenv("SAMUS_BUSINESS_TZ", "America/Los_Angeles")
    dt = datetime(2026, 7, 8, 3, 40, 58, tzinfo=timezone.utc)
    assert dt.date().isoformat() == "2026-07-08"      # naive UTC day
    assert business_date(dt) == "2026-07-07"          # the Pacific business day


def test_business_date_assumes_utc_for_naive(monkeypatch):
    monkeypatch.setenv("SAMUS_BUSINESS_TZ", "America/Los_Angeles")
    naive = datetime(2026, 7, 8, 3, 40, 58)  # treated as UTC
    assert business_date(naive) == "2026-07-07"


def test_business_date_matches_business_today_for_now():
    # business_date(now) and business_today() are the same Pacific day.
    assert business_date(utc_now()) == business_today()
