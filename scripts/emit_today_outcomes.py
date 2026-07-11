"""Runs INSIDE samus-voice. Emits ALL of today's end_of_call outcomes (from ANY
source — reconcile backfill OR a delivered webhook) as a reconcile-CLI-shaped
{"details":[...]} blob for Drain-CallOutcomes.py.

Gap-18: the reconcile CLI only reports NEWLY-backfilled calls in its `details`
(idempotent — a call appears once, in the run that backfills it). A call whose
end-of-call-report webhook is actually DELIVERED gets its end_of_call written by
the handler, never appears in reconcile's details, and so never drains to the
operator list. This latent bug activates the moment the ingress is fixed. Fix:
feed the drain the FULL set of today's outcomes; the drain's own idempotency
(_existing → skip already-journaled prospect_ids) dedups.

Window = a rolling last-WINDOW_HOURS (not a UTC-date match): a "today's calls"
run can straddle UTC midnight (e.g. 8pm Pacific = next-day 03:00 UTC), so a
date-string match would drop the pre-midnight half. The drain's idempotency
makes a slightly-wide window safe. Dedup by call_id, latest ts wins (so a
gatekeeper reclassification supersedes the original voicemail classification).
"""
import json
from datetime import datetime, timedelta, timezone

EVENTS = "/opt/samus/data/voice/voice_events.jsonl"
WINDOW_HOURS = 30


def _parse(ts: str):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def main() -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    latest: dict[str, dict] = {}
    try:
        with open(EVENTS, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("kind") != "end_of_call":
                    continue
                t = _parse(e.get("ts", ""))
                if t is None or t < cutoff:
                    continue
                cid = str(e.get("call_id") or "")
                if not cid:
                    continue
                prev = latest.get(cid)
                if prev is None or str(e.get("ts", "")) >= str(prev.get("ts", "")):
                    latest[cid] = e
    except OSError:
        print(json.dumps({"details": []}))
        return 0

    details = []
    for cid, e in latest.items():
        details.append({
            "call_id": cid,
            "prospect_id": e.get("prospect_id"),
            "company": e.get("company"),
            "phone": e.get("phone"),
            "outcome": e.get("outcome"),
        })
    print(json.dumps({"window_hours": WINDOW_HOURS, "count": len(details), "details": details}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
