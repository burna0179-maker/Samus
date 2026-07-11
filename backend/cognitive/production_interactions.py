"""Compile the day's voice + email INTERACTIONS into compressed signal for the
intelligence cycle (STRATEGIC DAILY INTELLIGENCE framework — signal preservation,
noise elimination).

`gather_production_state()` snapshots financial state (CODB/runway); this adds
the missing half — what actually HAPPENED in production today: calls placed +
their outcomes/objections/intent, emails sent + failures. Compressed to the
framework's inclusion set (accomplishments, risks, bottlenecks, anomalies) with
routine successes collapsed to counts.

Pure reads over the day's ledgers; every source degrades to an error note rather
than raising, so a report is never blocked.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

_LOG = logging.getLogger("samus.cognitive.production_interactions")

# Intent score at/above which a voice interaction is "notable" (signal, not noise).
_HIGH_INTENT = 70


def _read_jsonl(path) -> list[dict[str, Any]]:
    try:
        if not path.is_file():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:  # noqa: BLE001 — skip a corrupt line, keep the rest
                continue
        return rows
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("interaction read failed %s: %s", path, exc)
        return []


def _same_business_day(ts_iso: str, day_iso: str) -> bool:
    """True if an ISO timestamp falls on the given business (PT) date."""
    try:
        from backend.common.us_timezones import state_to_timezone

        s = str(ts_iso).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(state_to_timezone("CA")).date().isoformat() == day_iso
    except Exception:  # noqa: BLE001
        return False


def compile_voice_interactions(day_iso: str) -> dict[str, Any]:
    """Compress today's voice events to signal: totals + notable items only."""
    from backend.common import storage

    try:
        rows = _read_jsonl(storage.root() / "voice" / "voice_events.jsonl")
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}

    today = [
        r for r in rows if _same_business_day(r.get("ts") or r.get("received_at") or "", day_iso)
    ]
    out = {
        "events": len(today),
        "connected": 0,
        "voicemail": 0,
        "no_answer": 0,
        "high_intent": 0,
        "objections": [],
        "notable": [],
    }
    for r in today:
        outcome = str(r.get("outcome") or r.get("ended_reason") or r.get("kind") or "").lower()
        if "voicemail" in outcome or "machine" in outcome:
            out["voicemail"] += 1
        elif "no-answer" in outcome or "no_answer" in outcome:
            out["no_answer"] += 1
        elif "connect" in outcome or "ended" in outcome or "customer" in outcome:
            out["connected"] += 1
        summary = r.get("lead_summary") or {}
        intent = summary.get("intent_score")
        if isinstance(intent, (int, float)) and intent >= _HIGH_INTENT:
            out["high_intent"] += 1
            out["notable"].append(
                f"high-intent call ({intent}) {summary.get('tier', '')} "
                f"rec={summary.get('recommended_action', '')}".strip()
            )
        obj = summary.get("objection") or summary.get("objection_signal")
        if obj:
            out["objections"].append(str(obj)[:120])
    # De-dup + cap the noisy lists to keep the report compressed.
    out["objections"] = sorted(set(out["objections"]))[:10]
    out["notable"] = out["notable"][:10]
    return out


def compile_email_interactions(day_iso: str) -> dict[str, Any]:
    """Compress today's outreach sends to signal: totals + failures (the signal)."""
    from backend.common import storage

    try:
        root = storage.root()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}

    out = {"sent": 0, "failed": 0, "failures": []}
    # morning_batch_<date>.jsonl (status rows) + outreach_audit.jsonl (send events)
    for base in (root, root / "outreach"):
        for row in _read_jsonl(base / f"morning_batch_{day_iso}.jsonl"):
            st = str(row.get("status") or "").lower()
            if st == "sent":
                out["sent"] += 1
            elif st in ("failed", "bounce", "error"):
                out["failed"] += 1
                out["failures"].append(str(row.get("reason") or row.get("email") or st)[:120])
    for row in _read_jsonl(root / "outreach" / "outreach_audit.jsonl"):
        if not _same_business_day(row.get("ts") or "", day_iso):
            continue
        st = str(row.get("status") or "").lower()
        if st in ("sent", "delivered", "ok"):
            out["sent"] += 1
        elif st in ("failed", "bounce", "error", "suppressed"):
            out["failed"] += 1
            out["failures"].append(str(row.get("status") or "")[:120])
    out["failures"] = sorted(set(out["failures"]))[:10]
    return out


def compile_day_interactions(now_utc: Optional[datetime] = None) -> dict[str, Any]:
    """The day's compressed voice + email interaction signal for the EOD report."""
    from backend.common.us_timezones import business_today

    day_iso = business_today().isoformat()
    return {
        "date": day_iso,
        "voice": compile_voice_interactions(day_iso),
        "email": compile_email_interactions(day_iso),
    }
