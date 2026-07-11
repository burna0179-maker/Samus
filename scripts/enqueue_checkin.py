"""Append a client check-in to the send queue with a 24h hold. The queue stores
metadata + a pointer to the draft .md (NOT the body) — the send sweep reads the
live draft at send time so operator edits up to the last minute are honored.

Usage:
  python enqueue_checkin.py --customer-id X --to a@b --company "C" \
     --contact "Name" --from-name "Alex" --reply-to r@h \
     --draft-path seo_clients/<slug>/CHECKIN_DRAFT_<date>.md [--hold-hours 24]
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

QUEUE = r"D:\Hustleforge\Samus\.data\host_artifacts\seo_clients\checkin_queue.jsonl"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--customer-id", required=True)
    p.add_argument("--to", required=True)
    p.add_argument("--company", default="")
    p.add_argument("--contact", default="")
    p.add_argument("--from-name", default="HustleForge")
    p.add_argument("--reply-to", default="")
    p.add_argument("--draft-path", required=True)
    p.add_argument("--hold-hours", type=float, default=24.0)
    a = p.parse_args()

    # Input validation (defense-in-depth; the sweep also allowlists + confines).
    import re as _re
    if not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", (a.to or "").strip()):
        print(json.dumps({"queued": False, "reason": "bad_recipient"})); return 0
    _dp = (a.draft_path or "").replace("\\", "/")
    if _dp.startswith("/") or ".." in _dp.split("/") or _re.match(r"^[A-Za-z]:", _dp):
        print(json.dumps({"queued": False, "reason": "unsafe_draft_path"})); return 0

    now = datetime.now(timezone.utc)
    entry = {
        "id": f"{a.customer_id}_{now.strftime('%Y%m%dT%H%M%SZ')}",
        "customer_id": a.customer_id,
        "to": a.to,
        "company": a.company,
        "contact": a.contact,
        "from_name": a.from_name,
        "reply_to": a.reply_to,
        "draft_path": a.draft_path.replace("\\", "/"),
        "drafted_at": now.isoformat(),
        "send_after": (now + timedelta(hours=a.hold_hours)).isoformat(),
        "status": "pending",
    }

    # de-dupe: don't double-queue a still-pending draft for the same file.
    existing = []
    if os.path.exists(QUEUE):
        for line in open(QUEUE, encoding="utf-8-sig"):
            line = line.strip()
            if line:
                try:
                    existing.append(json.loads(line))
                except ValueError:
                    pass
    for e in existing:
        if e.get("draft_path") == entry["draft_path"] and e.get("status") == "pending":
            print(json.dumps({"queued": False, "reason": "already_pending", "id": e.get("id")}))
            return 0

    os.makedirs(os.path.dirname(QUEUE), exist_ok=True)
    with open(QUEUE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(json.dumps({"queued": True, "id": entry["id"], "send_after": entry["send_after"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
