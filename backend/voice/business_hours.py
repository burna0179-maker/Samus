"""Per-prospect "is this business open right now?" check for the dialer.

The morning dialer can use this to skip prospects whose business is closed at
the prospect-local moment of the dial, cutting wasted voicemail attempts. It
is a pure, stdlib-only helper that NEVER raises: on any missing or unparseable
input it returns ``None`` so the caller can fail-OPEN (a bad ``business_hours``
string must never block a legitimate dial).

Input format (from the daily call-list CSV ``business_hours`` column)::

    "Monday: 8:00 AM - 8:00 PM | Tuesday: ... | Saturday: Closed | Sunday: Closed"

Day entries are split on ``" | "``. Each entry is either
``"<DayName>: <open>-<close>"`` or ``"<DayName>: Closed"``.

The real CSV carries unicode that must be normalized before parsing:
    * U+202F NARROW NO-BREAK SPACE before AM/PM
    * U+2009 THIN SPACE flanking the range dash
    * the dash itself may be an en-dash (U+2013) or a plain hyphen
Both U+202F and U+2009 are folded to a regular space; the en-dash is folded to
a hyphen. (Stray U+FFFD replacement chars produced by lossy CSV round-trips are
also tolerated as range separators.)

Public API::

    is_open_now(business_hours, *, now_local) -> bool | None
"""
from __future__ import annotations

import re
from datetime import datetime

__all__ = ["is_open_now"]

# datetime.weekday(): Monday == 0 ... Sunday == 6
_WEEKDAY_NAMES = (
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
)


def _normalize(raw: str) -> str:
    """Fold the CSV's unicode whitespace/dashes into ASCII equivalents."""
    return (
        raw.replace(" ", " ")   # narrow no-break space (before AM/PM)
        .replace(" ", " ")      # thin space (around the dash)
        .replace("–", "-")      # en-dash -> hyphen
        .replace("—", "-")      # em-dash -> hyphen (defensive)
        .replace(" ", " ")      # plain no-break space (defensive)
        .replace("�", "-")      # replacement char from lossy round-trips
    )


def _parse_12h(token: str) -> int | None:
    """Parse "8:00 AM" / "8 PM" / "12:30 pm" into minutes-since-midnight."""
    m = re.match(
        r"^(\d{1,2})(?::(\d{2}))?\s*([AaPp][Mm])$",
        token.strip(),
    )
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    if not (1 <= hour <= 12) or not (0 <= minute <= 59):
        return None
    ampm = m.group(3).lower()
    if ampm == "am":
        hour = 0 if hour == 12 else hour
    else:  # pm
        hour = 12 if hour == 12 else hour + 12
    return hour * 60 + minute


def is_open_now(business_hours: str, *, now_local: datetime) -> bool | None:
    """Return whether the business is open at ``now_local``.

    Args:
        business_hours: the raw ``business_hours`` CSV cell (may contain the
            unicode described in the module docstring).
        now_local: a tz-aware ``datetime`` in the *prospect's* local time.

    Returns:
        ``True``  -- the day's window contains ``now_local``.
        ``False`` -- the day is "Closed", or ``now_local`` is outside the
                     parsed [open, close] window.
        ``None``  -- the input is missing, the day is absent, or the entry is
                     unparseable. The caller MUST fail-open on ``None``.
    """
    try:
        if not business_hours or not isinstance(business_hours, str):
            return None
        if now_local is None or not isinstance(now_local, datetime):
            return None

        text = _normalize(business_hours)
        day_name = _WEEKDAY_NAMES[now_local.weekday()]

        # Find this weekday's entry among the " | "-separated segments.
        entry: str | None = None
        for segment in text.split("|"):
            seg = segment.strip()
            head, sep, rest = seg.partition(":")
            if not sep:
                continue
            if head.strip().lower() == day_name:
                entry = rest.strip()
                break

        if entry is None:
            return None  # day missing -> fail-open

        if entry.lower() == "closed":
            return False

        # "<open> - <close>" — split on the (normalized) hyphen.
        parts = [p.strip() for p in entry.split("-")]
        if len(parts) != 2:
            return None
        open_min = _parse_12h(parts[0])
        close_min = _parse_12h(parts[1])
        if open_min is None or close_min is None:
            return None

        now_min = now_local.hour * 60 + now_local.minute

        if close_min <= open_min:
            # Overnight window (e.g. 8 PM - 2 AM): open if at/after open OR
            # before close. Equal bounds are treated as a 24h window.
            if open_min == close_min:
                return True
            return now_min >= open_min or now_min < close_min

        return open_min <= now_min <= close_min
    except Exception:
        # Pure helper contract: never raise. Unknown -> fail-open.
        return None
