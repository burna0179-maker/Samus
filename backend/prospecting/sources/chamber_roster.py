"""Chamber of Commerce roster lookup.

Operator-curated rosters live as JSON files under
``Samus/.data/chamber_rosters/<city>.json`` (path overridable via the
``SAMUS_CHAMBER_ROSTER_DIR`` env var). Each file is a JSON array of
objects with at least a ``business_name`` field; optional
``member_since``, ``chamber_name``, ``url`` fields are passed through
into the signal's evidence.

This module is a lookup only. It does NOT scrape chamber sites —
roster files are curated by the operator. A medium-confidence signal
is emitted on match because exact-name match is the only join key and
DBA-vs-legal-name drift is common.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from ..legitimacy import LegitimacySignal

_LOG = logging.getLogger("samus.prospecting.sources.chamber_roster")

_DEFAULT_ROSTER_DIR = "Samus/.data/chamber_rosters"


def _roster_dir() -> str:
    return os.getenv("SAMUS_CHAMBER_ROSTER_DIR", _DEFAULT_ROSTER_DIR)


def _normalize(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


def _load_roster(city: str) -> list[dict[str, Any]]:
    path = os.path.join(_roster_dir(), f"{city.lower()}.json")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _LOG.info("chamber roster load failed for %s: %s", path, exc)
        return []
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict)]


def lookup_chamber_roster(
    business_name: str,
    *,
    city: str,
) -> LegitimacySignal | None:
    """Return a chamber_roster signal when business_name is on city's roster."""
    target = _normalize(business_name)
    if not target or not (city or "").strip():
        return None
    for entry in _load_roster(city):
        if _normalize(str(entry.get("business_name") or "")) == target:
            return LegitimacySignal(
                kind="chamber_roster",
                source=f"chamber_rosters/{city.lower()}.json",
                discovered_at=datetime.now(timezone.utc),
                evidence={
                    "business_name": business_name,
                    "city": city,
                    "member_since": str(entry.get("member_since") or ""),
                    "chamber_name": str(entry.get("chamber_name") or ""),
                    "url": str(entry.get("url") or ""),
                },
                confidence="medium",
            )
    return None


__all__ = ["lookup_chamber_roster"]
