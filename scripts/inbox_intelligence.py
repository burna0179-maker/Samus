"""Inbox Intelligence — show what Samus knows about the business email stream.

Reads the inbound_email ledger + runs the email classifier over persisted
entries to show category breakdown, vendor billing signals, recent
business-critical emails, and processing health.

    python scripts/inbox_intelligence.py             # full report
    python scripts/inbox_intelligence.py --days 7    # last 7 days only
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _load_ledger(path: Path, since: datetime | None = None) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since:
                ts = rec.get("ts", "")
                try:
                    entry_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if entry_dt < since:
                        continue
                except (ValueError, AttributeError):
                    pass
            entries.append(rec)
    return entries


def main() -> int:
    logging.basicConfig(level="WARNING")
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=0,
                        help="Limit to last N days (0 = all)")
    args = parser.parse_args()

    from backend.common.config import get_settings
    settings = get_settings()
    ledger_path = Path(settings.gmail_inbox_ledger_path)

    since = None
    if args.days > 0:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)

    entries = _load_ledger(ledger_path, since=since)
    if not entries:
        print("No entries in ledger.")
        return 0

    bar = "=" * 78
    print(bar)
    print("  SAMUS INBOX INTELLIGENCE REPORT")
    if args.days:
        print(f"  Window: last {args.days} day(s)")
    print(bar)

    # Processing health
    persisted = sum(1 for e in entries if e.get("persisted"))
    failed = sum(1 for e in entries if e.get("error"))
    replayed = sum(1 for e in entries if e.get("replay"))
    total = len(entries)
    print(f"\n  PROCESSING HEALTH")
    print(f"    Total entries:  {total}")
    print(f"    Persisted:      {persisted}")
    print(f"    Failed:         {failed}")
    print(f"    Replayed:       {replayed}")
    if total:
        print(f"    Success rate:   {persisted/total:.0%}")

    # Classify persisted entries
    categories: dict[str, int] = collections.Counter()
    vendors: dict[str, list[float]] = collections.defaultdict(list)
    bills: list[dict] = []

    try:
        from backend.intake.email_classifier import classify
        from backend.intake.gmail_poller import ParsedInboundEmail
        can_classify = True
    except ImportError:
        can_classify = False

    for e in entries:
        cat = e.get("category")
        if cat:
            categories[cat] += 1
        elif can_classify and e.get("persisted"):
            # Classify from ledger data (limited info)
            p = ParsedInboundEmail(
                message_id=e.get("message_id", ""),
                from_addr=("x@" + e.get("from_addr_tail", "unknown"))
                    if "@" not in e.get("from_addr_tail", "") else e.get("from_addr_tail", ""),
                from_display="",
                to_addrs=[],
                subject=e.get("subject_head", ""),
                date_header="",
                body_text="",
                body_format="text/plain",
            )
            try:
                c = classify(p)
                categories[c.category] += 1
                if c.category == "bill" and c.vendor_registry_id:
                    vendors[c.vendor_registry_id].append(
                        c.bill_amount_usd or 0.0,
                    )
            except Exception:
                categories["unclassified"] += 1
        elif e.get("persisted"):
            categories["unclassified"] += 1

        # Track vendor amounts from ledger fields
        if e.get("vendor") and e.get("amount_usd") is not None:
            vendors[e["vendor"]].append(e["amount_usd"])

    if categories:
        print(f"\n  EMAIL CATEGORIES")
        for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
            pct = count / max(persisted, 1) * 100
            print(f"    {cat:<16} {count:>5}  ({pct:4.1f}%)")

    if vendors:
        print(f"\n  VENDOR BILLING SIGNALS")
        for vendor, amounts in sorted(vendors.items(), key=lambda x: -sum(x[1])):
            real_amounts = [a for a in amounts if a > 0]
            total_usd = sum(real_amounts)
            count = len(amounts)
            latest = real_amounts[-1] if real_amounts else 0
            print(f"    {vendor:<35} signals={count:>3}  "
                  f"latest=${latest:>8.2f}  total=${total_usd:>10.2f}")

    # Recent high-priority emails
    print(f"\n  RECENT BUSINESS-CRITICAL EMAILS (last 10)")
    critical = [
        e for e in entries
        if e.get("persisted")
        and e.get("from_addr_tail", "")
        and not any(
            skip in e.get("from_addr_tail", "")
            for skip in ("linkedin.com", "noreply", "no-reply")
        )
    ][-10:]
    for e in critical:
        ts = e.get("ts", "")[:16]
        tail = e.get("from_addr_tail", "?")
        subj = e.get("subject_head", "(no subject)")[:60]
        cat = e.get("category", "")
        cat_str = f" [{cat}]" if cat else ""
        print(f"    {ts}  ...{tail:<15} {subj}{cat_str}")

    print(f"\n{bar}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
