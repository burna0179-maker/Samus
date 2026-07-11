"""Promotion ledger — the pipeline's memory of who was already promoted.

THE GAP THIS CLOSES (operator-reported 2026-07-03): discovery re-queried the
same cumulative geo-ring zips daily, Places returned the same businesses, and
the call list re-promoted them as "new prospects" every single day. Email
suppression only guarded the SEND; the voicemail drafts key per-day (so the
same prospect re-drafts tomorrow); nothing anywhere remembered "this business
already entered the pipeline". The rings are CUMULATIVE (ring N re-queries
rings 0..N), so ring advancement alone cannot fix this — the pipeline needs
durable promotion memory.

Semantics:
  * A prospect is PROMOTED when it lands on a daily call list. That is the
    single funnel entry; record it here.
  * A prospect promoted within ``SAMUS_PROSPECT_RECYCLE_DAYS`` (default 30)
    is NOT fresh — it is excluded from subsequent call lists. It is already
    in the CRM with whatever state the pipeline gave it (emailed, drafted,
    staked, closed_lost, ...); re-promoting it as new would double-contact.
  * When the cooldown lapses, the prospect naturally re-qualifies — that IS
    the recycle pass. Combined with ring auto-advance + wrap-to-ring-0 (see
    cadence_task), the operator's intended lifecycle emerges: exhaust ring →
    extend ring → wrap → recycle aged prospects through the pipeline again.

Storage: ``<artifact_root>/daily_calls/promoted_prospects.jsonl`` — append-
only, one row per promotion, replayed on read (the read set is small and the
file is bounded by real-world prospect volume).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from backend.common import storage
from backend.common.dates import iso_now

_LOG = logging.getLogger("samus.prospecting.promotion_ledger")

_ENV_RECYCLE_DAYS = "SAMUS_PROSPECT_RECYCLE_DAYS"
_DEFAULT_RECYCLE_DAYS = 30


def recycle_days() -> int:
    try:
        return max(1, int(os.environ.get(_ENV_RECYCLE_DAYS, "") or _DEFAULT_RECYCLE_DAYS))
    except ValueError:
        return _DEFAULT_RECYCLE_DAYS


def _ledger_path() -> Path:
    d = storage.root() / "daily_calls"
    d.mkdir(parents=True, exist_ok=True)
    return d / "promoted_prospects.jsonl"


def _parse_ts(raw: Any) -> datetime | None:
    try:
        s = str(raw).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def recently_promoted_ids(within_days: int | None = None) -> set[str]:
    """prospect_ids promoted within the cooldown window. Never raises —
    an unreadable ledger degrades to 'no memory' (old behaviour) rather
    than blocking discovery."""
    days = recycle_days() if within_days is None else max(1, int(within_days))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out: set[str] = set()
    try:
        path = _ledger_path()
        if not path.is_file():
            return out
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(row, dict):
                continue
            ts = _parse_ts(row.get("ts"))
            pid = str(row.get("prospect_id") or "").strip()
            if pid and ts is not None and ts >= cutoff:
                out.add(pid)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("promotion ledger read failed: %s", exc)
    return out


def ever_promoted_ids() -> set[str]:
    """ALL prospect_ids ever promoted, regardless of cooldown. A prospect that
    is eligible again (cooldown lapsed) but appears here is a RECYCLED
    prospect — it re-enters the pipeline as a FOLLOW-UP, not a first touch,
    and the touch-context assembler enriches it with its prior history."""
    out: set[str] = set()
    try:
        path = _ledger_path()
        if not path.is_file():
            return out
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            pid = str(row.get("prospect_id") or "").strip() if isinstance(row, dict) else ""
            if pid:
                out.add(pid)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("promotion ledger read failed: %s", exc)
    return out


def record_promotions(prospects: Iterable[Any]) -> int:
    """Append one row per promoted prospect. Returns rows written."""
    written = 0
    try:
        with _ledger_path().open("a", encoding="utf-8") as fh:
            for p in prospects:
                pid = str(getattr(p, "prospect_id", "") or "").strip()
                if not pid:
                    continue
                fh.write(json.dumps({
                    "ts": iso_now(),
                    "prospect_id": pid,
                    "company": str(getattr(p, "company_name", "") or ""),
                    "owner_email": str(getattr(p, "owner_email", "") or ""),
                }, ensure_ascii=False) + "\n")
                written += 1
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("promotion ledger append failed: %s", exc)
    return written
