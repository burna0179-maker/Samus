"""Retry queue for no-answer / voicemail outbound call outcomes.

Written to by service.handle_webhook_event on end-of-call when ended_reason
indicates the prospect was not reached. Read by the morning dialer to prepend
retry candidates ahead of fresh prospects in the next run.

File: <artifact_root>/voice/retry_queue.jsonl
Format: one JSON object per line, newest last.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

_LOG = logging.getLogger("samus.voice.retry")

_RETRY_QUEUE_DEFAULT = "/opt/samus/data/voice/retry_queue.jsonl"
_queue_lock = threading.Lock()

NO_ANSWER_REASONS: set[str] = {
    "no-answer",
    "voicemail",
    "machine_detected_silence",
    "machine_end_beep",
    "machine_end_silence",
    "machine_end_other",
    "assistant-error",
    "pipeline-error",
}


def _queue_path() -> Path:
    return Path(os.getenv("SAMUS_RETRY_QUEUE_PATH", _RETRY_QUEUE_DEFAULT))


def add_retry_entry(
    *,
    prospect_id: str,
    prospect_phone: str,
    company_name: str,
    priority: str,
    run_id: str,
    ended_reason: str,
    ts: str,
) -> None:
    """Append a retry candidate to the queue JSONL. Best-effort, never raises."""
    entry = {
        "prospect_id": prospect_id,
        "prospect_phone": prospect_phone,
        "company_name": company_name,
        "priority": priority,
        "run_id": run_id,
        "ended_reason": ended_reason,
        "ts": ts,
    }
    path = _queue_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _queue_lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        _LOG.info("retry: queued prospect=%s reason=%s", prospect_id, ended_reason)
    except OSError as exc:
        _LOG.warning("retry: failed to append queue entry for %s: %s", prospect_id, exc)


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def get_retry_entries(*, max_age_hours: int = 24) -> list[dict]:
    """Return retry candidates within max_age_hours, deduped by prospect_id.

    When the same prospect_id appears multiple times, only the latest entry
    (by ts) is returned. Results are sorted oldest-first so the caller can
    prepend them in arrival order.
    """
    path = _queue_path()
    if not path.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    latest: dict[str, dict] = {}

    try:
        with _queue_lock:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = _parse_ts(entry.get("ts"))
                    if ts is None or ts < cutoff:
                        continue
                    pid = entry.get("prospect_id") or ""
                    existing = latest.get(pid)
                    if existing is None:
                        latest[pid] = entry
                    else:
                        existing_ts = _parse_ts(existing.get("ts"))
                        if existing_ts is None or ts > existing_ts:
                            latest[pid] = entry
    except OSError as exc:
        _LOG.warning("retry: failed to read queue: %s", exc)
        return []

    return sorted(latest.values(), key=lambda e: e.get("ts") or "")


def has_active_voicemail_entry(
    *,
    prospect_id: str,
    prospect_phone: str = "",
    max_age_hours: int = 36,
) -> bool:
    """Return True if a voicemail-classified retry entry exists for
    ``prospect_id`` (or matching ``prospect_phone``) within the last
    ``max_age_hours`` hours.

    Used by the dialer cooldown gate to exempt same-day voicemail retries —
    those should ride the retry queue's short window (24-36h) rather than
    falling into the 7-day per-number cooldown floor.

    Match conditions:
      - ``ended_reason`` is one of: ``voicemail``, ``machine_detected_silence``,
        ``machine_end_beep``, ``machine_end_silence``, ``machine_end_other``.
      - ``ts`` is within ``max_age_hours`` of now.
      - ``prospect_id`` matches OR (``prospect_phone`` provided and matches).

    Best-effort: any read error returns False (deny exemption -> safer).
    """
    path = _queue_path()
    if not path.exists():
        return False
    if not prospect_id and not prospect_phone:
        return False

    voicemail_reasons = {
        "voicemail",
        "machine_detected_silence",
        "machine_end_beep",
        "machine_end_silence",
        "machine_end_other",
    }
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

    try:
        with _queue_lock:
            with path.open("r", encoding="utf-8") as fh:
                lines = fh.readlines()
    except OSError as exc:
        _LOG.warning("retry: failed to read queue for vm-exemption: %s", exc)
        return False

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        reason = (entry.get("ended_reason") or "").lower()
        if reason not in voicemail_reasons:
            continue
        ts = _parse_ts(entry.get("ts"))
        if ts is None or ts < cutoff:
            continue
        if prospect_id and entry.get("prospect_id") == prospect_id:
            return True
        if prospect_phone and entry.get("prospect_phone") == prospect_phone:
            return True
    return False


def clear_entry(prospect_id: str) -> None:
    """Remove all queue entries for prospect_id. Called after a successful retry.

    Rewrites the file without the matching entries. Best-effort, never raises.
    """
    path = _queue_path()
    if not path.exists():
        return

    try:
        with _queue_lock:
            with path.open("r", encoding="utf-8") as fh:
                lines = fh.readlines()

            kept = []
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    kept.append(line)
                    continue
                if entry.get("prospect_id") != prospect_id:
                    kept.append(line)

            with path.open("w", encoding="utf-8") as fh:
                fh.writelines(kept)

        _LOG.info("retry: cleared entries for prospect=%s", prospect_id)
    except OSError as exc:
        _LOG.warning("retry: failed to clear entry for %s: %s", prospect_id, exc)
