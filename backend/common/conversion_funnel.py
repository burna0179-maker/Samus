"""Conversion-funnel ledger — additive observability telemetry (2026-05-20).

Records counts at each stage of the deal funnel so an operator can see
*where deals leak*:

    lead -> prospect -> opportunity -> proposal -> closed_won
                                                \\-> closed_lost

WHY A JSONL LEDGER, NOT A DDB TABLE
-----------------------------------
This is best-effort telemetry, not authoritative state. An append-only JSONL
file (the same :class:`backend.common.persistence.JsonlLedger` the CRM audit
trail and DLQ already use) means a funnel increment can never fail a CRM
write, never needs an AWS round-trip, and survives an AWS outage. The roll-up
read tails the file and aggregates in memory.

GRACEFUL DEGRADATION (non-negotiable)
-------------------------------------
Every public function here is wrapped so a filesystem error degrades to a
logged warning + a zeroed/empty result. ``record_stage`` returns a bool and
never raises; ``funnel_snapshot`` returns an all-zero snapshot on any failure.
A telemetry hiccup must never break the CRM lifecycle it instruments.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Final

from .dates import iso_now
from .persistence import JsonlLedger

_LOG = logging.getLogger("samus.common.conversion_funnel")

# Ordered funnel stages — index order is the leak-analysis order. ``closed_won``
# and ``closed_lost`` both descend from ``proposal``; the snapshot treats
# ``closed_won`` as the terminal success stage for conversion-rate math.
FUNNEL_STAGES: Final[tuple[str, ...]] = (
    "lead",
    "prospect",
    "opportunity",
    "proposal",
    "closed_won",
    "closed_lost",
)

_STAGE_SET: Final[frozenset[str]] = frozenset(FUNNEL_STAGES)

_LEDGER_PATH_DEFAULT: Final[str] = "/opt/samus/data/telemetry/conversion_funnel.jsonl"

# Tail window for the roll-up. Generous — the funnel is low-frequency (one
# entry per real lifecycle event), so this covers a long operating history
# without an unbounded read.
_SNAPSHOT_TAIL: Final[int] = 50_000


def _ledger_path() -> str:
    """Resolve the funnel ledger path (env override -> default)."""
    return os.getenv("SAMUS_CONVERSION_FUNNEL_PATH", _LEDGER_PATH_DEFAULT)


def _ledger() -> JsonlLedger:
    return JsonlLedger(_ledger_path())


def record_stage(
    stage: str,
    *,
    entity_id: str = "",
    source: str = "",
    industry: str = "",
) -> bool:
    """Append one funnel-stage increment to the ledger.

    ``stage`` must be one of :data:`FUNNEL_STAGES`; an unknown stage is a
    no-op (logged) so a typo upstream can never poison the roll-up. The
    optional ``entity_id`` / ``source`` / ``industry`` are recorded for
    later slicing but the roll-up only needs the stage.

    Returns ``True`` when the increment was written, ``False`` on a rejected
    stage or a filesystem error. Never raises — callers in the CRM lifecycle
    fire this best-effort and ignore the result.
    """
    if stage not in _STAGE_SET:
        _LOG.warning("conversion_funnel: rejecting unknown stage %r", stage)
        return False
    record = {
        "ts": iso_now(),
        "stage": stage,
        "entity_id": entity_id or "",
        "source": source or "",
        "industry": industry or "",
    }
    try:
        _ledger().append(record)
        return True
    except OSError as exc:
        _LOG.warning("conversion_funnel append failed stage=%s: %s", stage, exc)
        return False


def funnel_snapshot() -> dict[str, Any]:
    """Aggregate the funnel ledger into a leak-analysis snapshot.

    Returns a serialisable dict::

        {"stages": {"lead": int, "prospect": int, ...},   # all 6 stages
         "stage_order": [...],                             # FUNNEL_STAGES
         "transitions": [                                  # adjacent leak view
            {"from": "lead", "to": "prospect",
             "from_count": int, "to_count": int,
             "conversion_rate": 0.0..1.0, "leaked": int},
            ... ],
         "overall_conversion_rate": 0.0..1.0,  # closed_won / lead
         "total_events": int,
         "error": str | None}

    Conversion rate for an adjacent pair is ``to_count / from_count`` (0.0
    when the upstream stage has no entries — avoids divide-by-zero). The
    transition view stops at ``proposal -> closed_won`` because
    ``closed_lost`` is a sibling terminal, not a forward step.

    Never raises: any read failure yields an all-zero snapshot with the
    error string populated.
    """
    counts: dict[str, int] = {s: 0 for s in FUNNEL_STAGES}
    total = 0
    error: str | None = None
    try:
        for row in _ledger().tail(limit=_SNAPSHOT_TAIL):
            stage = str(row.get("stage", ""))
            if stage in counts:
                counts[stage] += 1
                total += 1
    except Exception as exc:  # noqa: BLE001 - telemetry read, never blocks
        _LOG.warning("conversion_funnel snapshot read failed: %s", exc)
        error = f"funnel_read_failed: {exc}"

    # Adjacent-stage transition view along the forward path
    # lead -> prospect -> opportunity -> proposal -> closed_won.
    forward = ("lead", "prospect", "opportunity", "proposal", "closed_won")
    transitions: list[dict[str, Any]] = []
    for upstream, downstream in zip(forward, forward[1:]):
        from_count = counts[upstream]
        to_count = counts[downstream]
        rate = round(to_count / from_count, 4) if from_count > 0 else 0.0
        transitions.append({
            "from": upstream,
            "to": downstream,
            "from_count": from_count,
            "to_count": to_count,
            "conversion_rate": rate,
            "leaked": max(0, from_count - to_count),
        })

    leads = counts["lead"]
    overall = (
        round(counts["closed_won"] / leads, 4) if leads > 0 else 0.0
    )
    return {
        "stages": counts,
        "stage_order": list(FUNNEL_STAGES),
        "transitions": transitions,
        "overall_conversion_rate": overall,
        "total_events": total,
        "error": error,
    }
