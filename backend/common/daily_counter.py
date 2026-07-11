"""Durable per-key daily counter — the hard-cap accounting substrate (HOTL T5).

The constitutional send/call caps (``SAMUS_MAX_SENDS_PER_DAY`` /
``SAMUS_MAX_CALLS_PER_DAY``) need a counter that survives a process restart:
an in-process dict (``common.idempotency``) would reset the day's tally every
time a worker recycles, silently re-opening the cap. So the count lives in a
tiny append-only JSONL ledger under the writable state root — the same durable
pattern :mod:`backend.common.send_ramp` already uses for warmup accounting.

One row per increment: ``{"date": "YYYY-MM-DD", "key": "<counter>"}``.
``count_today(key)`` is the number of rows for today; ``increment(key)`` appends
one and returns the NEW count. The caller enforces ``count >= cap`` itself.

FAIL-CLOSED for the cap check, FAIL-OPEN for the record:
  * ``count_today`` returns the true count, or 0 if the ledger is unreadable —
    an unreadable ledger must not silently pretend the cap was hit (that would
    be a fail-open cap; here the surrounding caller decides). Read failures are
    logged.
  * ``increment`` records best-effort; an I/O failure is logged and the
    returned count still reflects the attempt (count_today+1) so a ledger
    hiccup never lets an unbounded burst slip the cap within one process.

Path: honors ``SAMUS_DAILY_COUNTER_PATH`` (explicit override, used by tests);
otherwise ``<state_root>/coordination/daily_counters.jsonl`` via the canonical
:func:`backend.common.state_paths.state_path` resolver.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
from pathlib import Path

from .state_paths import state_path

_LOG = logging.getLogger("samus.daily_counter")

ENV_LEDGER = "SAMUS_DAILY_COUNTER_PATH"

_LOCK = threading.Lock()


def ledger_path() -> Path:
    """Resolve the daily-counter JSONL ledger path (env override wins)."""
    explicit = os.environ.get(ENV_LEDGER, "").strip()
    if explicit:
        return Path(explicit)
    return state_path("coordination", "daily_counters.jsonl")


def _read_rows(path: Path | None = None) -> list[dict]:
    target = path or ledger_path()
    if not target.exists():
        return []
    rows: list[dict] = []
    try:
        for line in target.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError as exc:
        _LOG.warning("daily_counter ledger read failed (%s)", exc)
        return []
    return rows


def count_today(key: str, *, today: _dt.date | None = None) -> int:
    """Count increments recorded for ``key`` on ``today`` (default: today)."""
    day = (today or _dt.date.today()).isoformat()
    return sum(
        1 for r in _read_rows() if r.get("date") == day and r.get("key") == key
    )


def increment(key: str, *, today: _dt.date | None = None) -> int:
    """Append one increment for ``key`` and return the resulting today-count.

    The count is computed from the ledger BEFORE the append (so it is correct
    even if two processes race — the returned value is this caller's own
    position). Best-effort write; a failure is logged but the returned count
    still advances so the cap holds within the process.
    """
    day = (today or _dt.date.today()).isoformat()
    path = ledger_path()
    with _LOCK:
        prior = sum(
            1 for r in _read_rows(path)
            if r.get("date") == day and r.get("key") == key
        )
        row = {"date": day, "key": key}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001 — accounting, never a raise
            _LOG.warning("daily_counter append failed (%s)", exc)
        return prior + 1


__all__ = ["increment", "count_today", "ledger_path", "ENV_LEDGER"]
