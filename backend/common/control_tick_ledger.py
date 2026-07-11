"""Control-loop tick ledger — additive observability telemetry (2026-05-20).

Records one entry per stack-level control-loop tick (see
``backend.gateway.control_tick``) so an operator can see the observe->decide
history of the autonomous control loop:

    entropy_score + countermeasures  (from the entropy workcell scan)
    quota_cuts / priority_boosts     (from portfolio_controller.run_rebalance)
    per-sub-call error fields        (best-effort: a failed sub-call still
                                      produces a tick row)

WHY A JSONL LEDGER, NOT A DDB TABLE
-----------------------------------
This is best-effort telemetry, not authoritative state. An append-only JSONL
file (the same :class:`backend.common.persistence.JsonlLedger` the CRM audit
trail, DLQ, and conversion funnel already use) means a tick record can never
fail the tick itself, never needs an AWS round-trip, and survives an AWS
outage. The recent-ticks read tails the file.

GRACEFUL DEGRADATION (non-negotiable)
-------------------------------------
``record_tick`` returns a bool and never raises — a filesystem error degrades
to a logged warning + ``False``. ``recent_ticks`` returns an empty list with
an error string on any read failure. A telemetry hiccup must never break the
control loop it instruments.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Final

from .dates import iso_now
from .persistence import JsonlLedger

_LOG = logging.getLogger("samus.common.control_tick_ledger")

_LEDGER_PATH_DEFAULT: Final[str] = "/opt/samus/data/telemetry/control_ticks.jsonl"

# Tail window for the recent-ticks read. The control loop is low-frequency
# (one entry per operator/scheduler trigger), so this covers a long operating
# history without an unbounded read.
_RECENT_TAIL_MAX: Final[int] = 10_000


def _ledger_path() -> str:
    """Resolve the control-tick ledger path (env override -> default)."""
    return os.getenv("SAMUS_CONTROL_TICK_PATH", _LEDGER_PATH_DEFAULT)


def _ledger() -> JsonlLedger:
    return JsonlLedger(_ledger_path())


def record_tick(record: dict[str, Any]) -> bool:
    """Append one control-loop tick record to the ledger.

    ``record`` is the snapshot dict assembled by
    :func:`backend.gateway.control_tick.run_control_tick`. A ``ts`` ISO8601
    field is injected if the caller did not supply one so every row is
    timestamped consistently.

    Returns ``True`` when the row was written, ``False`` on a filesystem
    error. Never raises — the tick endpoint fires this best-effort and
    surfaces the result as a ``recorded`` flag without failing the tick.
    """
    row = dict(record)
    row.setdefault("ts", iso_now())
    try:
        _ledger().append(row)
        return True
    except OSError as exc:
        _LOG.warning("control_tick_ledger append failed: %s", exc)
        return False


def recent_ticks(limit: int = 50) -> dict[str, Any]:
    """Tail the control-tick ledger into a recent-ticks view.

    Returns a serialisable dict::

        {"ticks": [<tick record>, ...],   # newest last, up to ``limit``
         "count": int,
         "error": str | None}

    ``limit`` is clamped to ``[1, _RECENT_TAIL_MAX]``. Never raises: any read
    failure yields an empty list with the error string populated so an
    operator dashboard does not have to special-case a telemetry outage.
    """
    safe_limit = max(1, min(int(limit), _RECENT_TAIL_MAX))
    ticks: list[dict[str, Any]] = []
    error: str | None = None
    try:
        ticks = _ledger().tail(limit=safe_limit)
    except Exception as exc:  # noqa: BLE001 - telemetry read, never blocks
        _LOG.warning("control_tick_ledger recent read failed: %s", exc)
        error = f"control_tick_read_failed: {exc}"
    return {
        "ticks": ticks,
        "count": len(ticks),
        "error": error,
    }


__all__ = ["record_tick", "recent_ticks"]
