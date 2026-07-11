"""Outbound-email send-ramp ledger — the warmup-volume ground truth.

A JSONL ledger under the writable state root records one row per *real* live
send: ``{"date": "YYYY-MM-DD", "to": "<recipient>"}``. The daily row count is
the raw material the sender-reputation warmup ramp reads ("how many did we
actually send today / on day N since the first send"). Before this, the live
cash-engine cold-send path wrote nothing here, so the ramp's day-volume
tracking was blind to actual outbound (B-6).

Scope — RECORD ONLY. This module deliberately does NOT gate, throttle, or
defer a send: it appends the audit row *after* a send has already succeeded so
the ledger reflects what truly went out. Send-gating/allowance policy is a
separate concern and is intentionally not wired here (the cadence loop + Heat
Field own pacing); this only fixes the accounting.

FAIL-OPEN: a ledger I/O failure must never break a send or raise into the send
path — accurate accounting is hygiene, not a hard gate. Every write is
best-effort and swallows its own errors.

Path: honors ``SAMUS_SEND_RAMP_LEDGER_PATH`` (explicit override); otherwise
``<state_root>/outreach/send_ramp.jsonl`` via the canonical
:func:`backend.common.state_paths.state_root` resolver (the writable
``samus-data`` volume in-container, ``<code root>/state`` on host/tests).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
from pathlib import Path

from backend.common.state_paths import state_path

_LOG = logging.getLogger("samus.send_ramp")

# Explicit ledger-path override (kept from the original ramp module's env
# contract so existing deployment config / tests keep pointing at one file).
ENV_LEDGER = "SAMUS_SEND_RAMP_LEDGER_PATH"

_LOCK = threading.Lock()


def ledger_path() -> Path:
    """Resolve the send-ramp JSONL ledger path.

    ``SAMUS_SEND_RAMP_LEDGER_PATH`` wins when set; otherwise
    ``<state_root>/outreach/send_ramp.jsonl``.
    """
    explicit = os.environ.get(ENV_LEDGER, "").strip()
    if explicit:
        return Path(explicit)
    return state_path("outreach", "send_ramp.jsonl")


def record_send(*, to: str = "", today: _dt.date | None = None) -> bool:
    """Append one send-ramp row for a send that has ALREADY happened.

    Records ``{"date": <today>, "to": <to>}`` — the same row shape the ramp
    ledger has always used. Best-effort and fail-open: returns ``True`` when
    the row was written, ``False`` on any I/O failure (never raises). Intended
    to be called from the live send path immediately after a real send
    succeeds, so the ramp's daily-volume accounting reflects actual outbound.
    """
    day = (today or _dt.date.today()).isoformat()
    row = {"date": day, "to": (to or "")}
    path = ledger_path()
    with _LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            return True
        except Exception as exc:  # noqa: BLE001 — accounting, never a hard gate
            _LOG.warning("send ramp ledger append failed (%s) — accounting skipped", exc)
            return False


def read_rows(path: Path | None = None) -> list[dict]:
    """Read the ledger rows (skipping blanks / malformed lines). [] if absent."""
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
        _LOG.warning("send ramp ledger read failed (%s)", exc)
        return []
    return rows


def sent_today(today: _dt.date | None = None, path: Path | None = None) -> int:
    """Count ledger rows recorded for ``today`` (read-only; for ops/tests)."""
    day = (today or _dt.date.today()).isoformat()
    return sum(1 for r in read_rows(path) if r.get("date") == day)


__all__ = ["record_send", "read_rows", "sent_today", "ledger_path", "ENV_LEDGER"]
