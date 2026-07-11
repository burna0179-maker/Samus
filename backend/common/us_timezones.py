"""US state -> IANA timezone map + business-hours gate.

The morning dialer uses this to honor TCPA call-hour rules per-prospect
rather than collapsing every prospect into operator-local time. The map is
intentionally coarse: states split across multiple zones (FL, IN, MI, TN,
KS, etc.) resolve to the *majority* zone so the gate stays simple. Edge-case
prospects in the minority part of a split-zone state will see a slightly
shifted call window — acceptable tradeoff for "no external deps, no
geocoding required" at v1.

Public API:
    state_to_timezone("CA")            -> ZoneInfo("America/Los_Angeles")
    is_within_call_hours(state="CA", now=None) -> bool
    DEFAULT_CALL_HOURS = (8, 21)       # 8am-9pm prospect-local

A state code that isn't in the map (territories, blanks, typos) falls back
to America/Los_Angeles — the operator's tz. Conservative direction since
PT 8am-9pm is inside the legal window everywhere else in the US.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Final
from zoneinfo import ZoneInfo


# (start_hour_inclusive, end_hour_exclusive) — TCPA federal rule is 8am-9pm
# local; the more-restrictive end-9pm reads as "before 21:00" since 21:00
# would already be past the gate.
DEFAULT_CALL_HOURS: Final[tuple[int, int]] = (8, 21)

# Operator-local fallback used when a prospect's state is missing/unknown.
_OPERATOR_TZ_NAME: Final[str] = "America/Los_Angeles"


# Majority-zone mapping for 2-letter USPS state codes + DC. Split-zone notes:
#   FL panhandle -> CT, but Miami/Orlando dominate -> ET
#   IN northwest corner -> CT, but Indianapolis dominates -> ET
#   KS far west -> MT, but Wichita/KC dominates -> CT
#   MI upper peninsula far west -> CT, but everything else -> ET
#   NE western panhandle -> MT, but Omaha/Lincoln dominate -> CT
#   ND western quarter -> MT, but Fargo dominates -> CT
#   OR Malheur County -> MT, but Portland dominates -> PT
#   SD western half -> MT, but Sioux Falls dominates -> CT
#   TN eastern -> ET, western -> CT; Nashville/Memphis split, pick CT (larger pop)
#   TX El Paso -> MT, but everything else -> CT
#
# Territories (PR/VI/GU/AS/MP) are present so callsheet rows with a
# territory state code still get a defensible tz instead of falling back
# to operator-PT.
_STATE_TO_TZ: Final[dict[str, str]] = {
    # Eastern
    "CT": "America/New_York",
    "DE": "America/New_York",
    "DC": "America/New_York",
    "FL": "America/New_York",
    "GA": "America/New_York",
    "IN": "America/New_York",
    "ME": "America/New_York",
    "MD": "America/New_York",
    "MA": "America/New_York",
    "MI": "America/New_York",
    "NH": "America/New_York",
    "NJ": "America/New_York",
    "NY": "America/New_York",
    "NC": "America/New_York",
    "OH": "America/New_York",
    "PA": "America/New_York",
    "RI": "America/New_York",
    "SC": "America/New_York",
    "VT": "America/New_York",
    "VA": "America/New_York",
    "WV": "America/New_York",
    # Central
    "AL": "America/Chicago",
    "AR": "America/Chicago",
    "IL": "America/Chicago",
    "IA": "America/Chicago",
    "KS": "America/Chicago",
    "KY": "America/Chicago",
    "LA": "America/Chicago",
    "MN": "America/Chicago",
    "MS": "America/Chicago",
    "MO": "America/Chicago",
    "NE": "America/Chicago",
    "ND": "America/Chicago",
    "OK": "America/Chicago",
    "SD": "America/Chicago",
    "TN": "America/Chicago",
    "TX": "America/Chicago",
    "WI": "America/Chicago",
    # Mountain (Arizona is no-DST so it's a quirk — most of the year same as PT)
    "AZ": "America/Phoenix",
    "CO": "America/Denver",
    "ID": "America/Denver",
    "MT": "America/Denver",
    "NM": "America/Denver",
    "UT": "America/Denver",
    "WY": "America/Denver",
    # Pacific
    "CA": "America/Los_Angeles",
    "NV": "America/Los_Angeles",
    "OR": "America/Los_Angeles",
    "WA": "America/Los_Angeles",
    # Alaska
    "AK": "America/Anchorage",
    # Hawaii (no DST)
    "HI": "Pacific/Honolulu",
    # Territories
    "PR": "America/Puerto_Rico",
    "VI": "America/St_Thomas",
    "GU": "Pacific/Guam",
    "MP": "Pacific/Saipan",
    "AS": "Pacific/Pago_Pago",
}


def state_to_timezone(state: str | None) -> ZoneInfo:
    """Resolve a 2-letter USPS state code to its majority IANA timezone.

    Empty / unknown / non-string inputs fall back to the operator's tz
    (America/Los_Angeles). Case-insensitive; whitespace tolerated.
    """
    if not state or not isinstance(state, str):
        return ZoneInfo(_OPERATOR_TZ_NAME)
    key = state.strip().upper()
    return ZoneInfo(_STATE_TO_TZ.get(key, _OPERATOR_TZ_NAME))


def business_today(state: str = "CA"):
    """Calendar date in the business's operating timezone (default
    America/Los_Angeles). Use this for date-stamped artifact filenames
    (call lists, ledgers) so a UTC-clock container agrees with PT-dated
    production data — plain ``date.today()`` is UTC and reads a day AHEAD all
    evening PT, breaking same-day file lookups."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).astimezone(state_to_timezone(state)).date()


def is_within_call_hours(
    *,
    state: str | None,
    now: datetime | None = None,
    hours: tuple[int, int] = DEFAULT_CALL_HOURS,
) -> bool:
    """True iff prospect-local time is inside the legal call window.

    ``now`` defaults to UTC now; passing a fixed value lets tests pin the
    clock. ``hours`` is (start_inclusive, end_exclusive) on a 24h clock,
    defaulting to TCPA 8am-9pm. Weekends are intentionally NOT excluded —
    TCPA permits weekend calls; some sales operations explicitly do them.
    """
    instant = now if now is not None else datetime.now(timezone.utc)
    if instant.tzinfo is None:
        # Naive datetime -> treat as UTC. Better than guessing.
        instant = instant.replace(tzinfo=timezone.utc)
    local = instant.astimezone(state_to_timezone(state))
    start, end = hours
    return start <= local.hour < end
