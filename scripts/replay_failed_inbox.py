"""Replay failed inbound emails from the idempotency ledger.

Reads the ledger at SAMUS_GMAIL_INBOX_LEDGER_PATH (or the config default),
finds entries that failed due to missing AWS credentials, re-fetches each
from Gmail by its gmail_id, and re-processes through handle_parsed_email
(which writes CRM artifacts + operator tasks to DynamoDB).

Requires: SAMUS_GMAIL_* and AWS_* env vars (the Replay-FailedInbox.ps1
wrapper handles DPAPI secret export).

Usage:
    python scripts/replay_failed_inbox.py             # replay all cred failures
    python scripts/replay_failed_inbox.py --dry-run   # count without processing
    python scripts/replay_failed_inbox.py --batch 50  # process 50 at a time
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.common.config import get_settings
from backend.common.dates import iso_now
from backend.intake.gmail_api_client import GmailApiClient
from backend.intake.gmail_poller import (
    _append_ledger,
    handle_parsed_email,
    parse_rfc822,
)

_LOG = logging.getLogger("samus.replay_failed_inbox")


def _load_failed_gmail_ids(ledger_path: Path) -> list[str]:
    """Extract gmail_ids from ledger entries that failed on missing creds."""
    if not ledger_path.exists():
        return []
    ids: list[str] = []
    with ledger_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            err = rec.get("error", "")
            if "credentials" in err.lower() or "unable to locate" in err.lower():
                gid = rec.get("gmail_id", "")
                if gid:
                    ids.append(gid)
    return ids


def replay(
    *,
    dry_run: bool = False,
    batch_size: int = 0,
) -> dict:
    settings = get_settings()
    ledger_path = Path(settings.gmail_inbox_ledger_path)
    failed_ids = _load_failed_gmail_ids(ledger_path)

    if not failed_ids:
        print("No failed entries found in ledger.")
        return {"total": 0}

    if batch_size > 0:
        failed_ids = failed_ids[:batch_size]

    print(f"Found {len(failed_ids)} failed entries to replay.")

    if dry_run:
        print("DRY RUN -- no processing. Pass without --dry-run to execute.")
        return {"total": len(failed_ids), "dry_run": True}

    if not (settings.gmail_inbox_email and settings.gmail_oauth_client_id
            and settings.gmail_oauth_client_secret):
        print("ERROR: Gmail OAuth credentials not set. Cannot replay.")
        return {"total": len(failed_ids), "error": "no_gmail_creds"}

    token_path = Path(settings.gmail_oauth_token_path)
    ts = iso_now()
    processed = 0
    failed = 0
    skipped = 0

    with GmailApiClient(
        client_id=settings.gmail_oauth_client_id,
        client_secret=settings.gmail_oauth_client_secret,
        token_path=token_path,
    ) as client:
        for i, gmail_id in enumerate(failed_ids):
            try:
                raw = client.fetch_raw(gmail_id)
                parsed = parse_rfc822(raw)
            except Exception as exc:
                _LOG.warning("fetch/parse failed gmail_id=%s: %s", gmail_id, exc)
                failed += 1
                _append_ledger({
                    "ts": ts,
                    "gmail_id": gmail_id,
                    "message_id": "",
                    "status": "replay_fetch_failed",
                    "error": str(exc)[:240],
                })
                continue

            handled = handle_parsed_email(parsed)
            if handled.persisted and not handled.error:
                processed += 1
            else:
                failed += 1

            _append_ledger({
                "ts": ts,
                "gmail_id": gmail_id,
                "message_id": parsed.message_id,
                "from_addr_tail": parsed.from_addr[-12:],
                "subject_head": parsed.subject[:120],
                "artifact_id": handled.artifact_id,
                "operator_task_id": handled.operator_task_id,
                "opportunity_id": handled.opportunity_id,
                "billing_state": handled.billing_state,
                "persisted": handled.persisted,
                "error": handled.error[:240] if handled.error else "",
                "replay": True,
            })

            if (i + 1) % 25 == 0:
                print(f"  ... {i + 1}/{len(failed_ids)} "
                      f"(ok={processed} fail={failed} skip={skipped})")

    print(f"\nReplay complete: {processed} persisted, {failed} failed, "
          f"{skipped} skipped out of {len(failed_ids)} total.")
    return {
        "total": len(failed_ids),
        "processed": processed,
        "failed": failed,
        "skipped": skipped,
    }


def main() -> int:
    logging.basicConfig(
        level=os.getenv("SAMUS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="Replay failed inbound emails")
    parser.add_argument("--dry-run", action="store_true",
                        help="Count failures without re-processing")
    parser.add_argument("--batch", type=int, default=0,
                        help="Process at most N messages (0 = all)")
    args = parser.parse_args()

    result = replay(dry_run=args.dry_run, batch_size=args.batch)
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
