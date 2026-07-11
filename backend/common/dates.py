"""Timestamp helpers per doc §3.17."""
from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta, timezone


_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"
_LOG = logging.getLogger("samus.common.dates")

# Business operates on US Pacific time; containers run UTC, so "today" for an
# operator-facing daily counter must be the PACIFIC calendar day, not the UTC
# one. They diverge for anything logged after ~5pm PT (>= 00:00 UTC) — an
# evening call would otherwise roll onto the next UTC day and vanish from the
# HUD's "calls today". Override the zone via SAMUS_BUSINESS_TZ if the business
# ever relocates.
_BUSINESS_TZ_NAME = os.getenv("SAMUS_BUSINESS_TZ", "America/Los_Angeles")


def utc_now() -> datetime:
    """Timezone-aware datetime in UTC."""
    return datetime.now(timezone.utc)


def iso_now() -> str:
    """Current UTC time as ISO-8601 string with Z suffix."""
    return time.strftime(_ISO_FMT, time.gmtime())


def hours_from_now(hours: int | float) -> str:
    """ISO-8601 string offset by N hours from now (UTC, Z suffix)."""
    return (utc_now() + timedelta(hours=hours)).strftime(_ISO_FMT)


def _business_tzinfo() -> timezone | object:
    """Resolve the business tz. Falls back to UTC if the tz database is
    unavailable (e.g. a slim image with no tzdata) so callers degrade to the
    old UTC-day behavior rather than raising."""
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415
        return ZoneInfo(_BUSINESS_TZ_NAME)
    except Exception as exc:  # noqa: BLE001 — tzdata missing / bad name
        _LOG.warning(
            "business tz %r unavailable (%s); falling back to UTC for the "
            "business day", _BUSINESS_TZ_NAME, exc,
        )
        return timezone.utc


def business_today() -> str:
    """The current business (US Pacific) calendar day as YYYY-MM-DD.

    Use this instead of ``datetime.utcnow().date()`` for any operator-facing
    "today" counter — the two disagree in the PT evening and that gap silently
    drops evening activity from daily roll-ups.
    """
    return utc_now().astimezone(_business_tzinfo()).date().isoformat()


def business_date(dt: datetime) -> str:
    """The business (US Pacific) calendar day an aware datetime falls on, YYYY-MM-DD.

    Companion to :func:`business_today` for timestamps that are NOT "now":
    converts an arbitrary datetime into the Pacific business day it belongs to,
    so UTC-stamped ledger rows bucket into the same day the operator sees. A
    naive datetime is assumed to be UTC. Use this instead of ``dt.date()`` when
    grouping/windowing ledger rows by business day — a bare ``.date()`` on a
    UTC-Z timestamp rolls PT-evening activity onto the next calendar day.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_business_tzinfo()).date().isoformat()


def business_day_utc_bounds(day_iso: str | None = None) -> tuple[str, str]:
    """UTC ISO-8601 [start, end) bounds spanning one PACIFIC business day.

    Because DynamoDB rows stamp ``updated_at`` in UTC (Z-suffixed ISO), you
    cannot select a Pacific day with a ``begins_with(updated_at, "YYYY-MM-DD")``
    prefix — that matches the UTC day. Instead scan
    ``updated_at BETWEEN :start AND :end`` using the bounds returned here, which
    are the UTC instants of Pacific midnight → next Pacific midnight (DST-aware).

    ``day_iso`` defaults to the current business day. Returns
    ``(start_z, end_z)`` where end is EXCLUSIVE (next Pacific midnight).
    """
    tz = _business_tzinfo()
    if day_iso:
        d = date.fromisoformat(day_iso)
    else:
        d = utc_now().astimezone(tz).date()
    start_local = datetime(d.year, d.month, d.day, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    start_z = start_local.astimezone(timezone.utc).strftime(_ISO_FMT)
    end_z = end_local.astimezone(timezone.utc).strftime(_ISO_FMT)
    return start_z, end_z
