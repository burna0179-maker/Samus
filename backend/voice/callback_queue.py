"""Date-anchored callback queue — defer a prospect to a future call date.

Distinct from :mod:`backend.voice.retry` (short-window no-answer/voicemail
retries within ~36h). This is for prospects who are a LEAD but can't be
reached NOW for a known reason with a known return date — most commonly a
business closed for a vacation/holiday that announced when it reopens
("closed June 29th through July 5th, reopening July 6th"). Such a prospect
must NOT be dropped as a dead-end; it should re-surface in the dial queue on
its return date.

Store: ``<storage_root>/voice/callback_queue.jsonl`` — and because
``storage.root()`` is the host-bound artifacts dir (Gap-4), the queue is
CRASH-DURABLE: a callback scheduled for next week survives host crashes that
wipe the named volume. That durability is the whole point — losing a deferred
lead means it's never called back.

One JSON line per scheduled callback::

    {"prospect_id","company","phone","callback_date":"2026-07-06",
     "reason":"office closed for vacation","scheduled_at":"...Z","done":false}

The dialer prepends DUE callbacks (callback_date <= today, not done) ahead of
fresh prospects, mirroring how it prepends retry candidates. Best-effort +
fail-open throughout: a queue fault never blocks a dial run.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from backend.common import storage

_LOG = logging.getLogger("samus.voice.callback_queue")

_QUEUE_SUBPATH = "voice/callback_queue.jsonl"


def _queue_path() -> Path:
    # storage.root() == the host-bound artifacts dir → crash-durable.
    return storage.root() / _QUEUE_SUBPATH


def _read() -> list[dict[str, Any]]:
    path = _queue_path()
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError as exc:
        _LOG.warning("callback_queue read failed: %s", exc)
    return out


def _write_all(records: list[dict[str, Any]]) -> bool:
    path = _queue_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cbq_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for r in records:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            os.replace(tmp, str(path))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return True
    except OSError as exc:
        _LOG.warning("callback_queue write failed: %s", exc)
        return False


def schedule_callback(
    *,
    prospect_id: str,
    callback_date: str,
    company: str = "",
    phone: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """Schedule (or update) a date-anchored callback. Idempotent per
    prospect_id: a new schedule for an existing, not-done prospect updates its
    date/reason rather than duplicating. ``callback_date`` is ISO ``YYYY-MM-DD``.
    Never raises."""
    if not prospect_id:
        return {"scheduled": False, "reason": "no_prospect_id"}
    try:
        datetime.strptime(callback_date, "%Y-%m-%d")
    except ValueError:
        return {"scheduled": False, "reason": "bad_date"}

    records = _read()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for r in records:
        if r.get("prospect_id") == prospect_id and not r.get("done"):
            r["callback_date"] = callback_date
            r["reason"] = reason or r.get("reason", "")
            r["updated_at"] = now_iso
            _write_all(records)
            _LOG.info("callback rescheduled prospect=%s -> %s", prospect_id, callback_date)
            return {"scheduled": True, "updated": True, "callback_date": callback_date}

    records.append({
        "prospect_id": prospect_id,
        "company": company,
        "phone": phone,
        "callback_date": callback_date,
        "reason": reason,
        "scheduled_at": now_iso,
        "done": False,
    })
    if not _write_all(records):
        return {"scheduled": False, "reason": "persist_failed"}
    _LOG.info("callback scheduled prospect=%s for %s (%s)", prospect_id, callback_date, reason)
    return {"scheduled": True, "callback_date": callback_date}


def get_due_callbacks(*, today: str | None = None) -> list[dict[str, Any]]:
    """Return not-done callbacks whose callback_date <= today (deduped by
    prospect_id, latest schedule wins). The dialer prepends these."""
    today = today or date.today().isoformat()
    by_pid: dict[str, dict[str, Any]] = {}
    for r in _read():
        if r.get("done"):
            continue
        cb = str(r.get("callback_date") or "")
        if not cb or cb > today:
            continue
        pid = str(r.get("prospect_id") or "")
        if pid:
            by_pid[pid] = r  # later line wins
    return list(by_pid.values())


def pending_future_ids(*, today: str | None = None) -> dict[str, str]:
    """Return ``{prospect_id: callback_date}`` for not-done callbacks dated in
    the FUTURE (callback_date > today). The dialer uses this to SKIP a prospect
    that's deferred to a later date (e.g. an operator noted "closed until
    July 6th", or an auto-detected closure) — don't dial before then."""
    today = today or date.today().isoformat()
    out: dict[str, str] = {}
    for r in _read():
        if r.get("done"):
            continue
        cb = str(r.get("callback_date") or "")
        pid = str(r.get("prospect_id") or "")
        if pid and cb and cb > today:
            # keep the latest (largest) future date if multiple
            if pid not in out or cb > out[pid]:
                out[pid] = cb
    return out


def mark_done(prospect_id: str) -> int:
    """Mark all queued callbacks for a prospect done (called after a dial).
    Returns the count updated."""
    records = _read()
    n = 0
    for r in records:
        if r.get("prospect_id") == prospect_id and not r.get("done"):
            r["done"] = True
            r["done_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            n += 1
    if n:
        _write_all(records)
    return n
