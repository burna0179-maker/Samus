"""CLI for the open-no-click nudge watcher.

Subcommands::

    # one tick — for the scheduled task
    python -m backend.outreach.open_no_click_cli tick
    python -m backend.outreach.open_no_click_cli tick --dry-run
    python -m backend.outreach.open_no_click_cli tick --force-fire

    # retroactive register (Kelly's send, etc.)
    python -m backend.outreach.open_no_click_cli register \\
        --prospect-id pr_... --email ... --sent-at 2026-06-30T17:14:47Z \\
        --subject "..." --buy-url "https://..." --message-id z0tRVc...

    # operator cancel (Stripe payment, manual close)
    python -m backend.outreach.open_no_click_cli close \\
        --prospect-id pr_... --reason closed_won
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from backend.outreach import open_no_click_watch as watch


def _cmd_tick(args: argparse.Namespace) -> int:
    now_iso = args.now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    dry = True if args.dry_run else (False if args.force_fire else None)
    out = watch.tick(now_iso=now_iso, dry_run=dry)
    sys.stdout.write(json.dumps({"now": now_iso, "results": out}) + "\n")
    return 0


def _cmd_register(args: argparse.Namespace) -> int:
    out = watch.register(
        prospect_id=args.prospect_id,
        email=args.email,
        sent_at_iso=args.sent_at,
        subject=args.subject,
        buy_url=args.buy_url,
        message_id=args.message_id or "",
        company=args.company or "",
        campaign_id=args.campaign_id or "",
    )
    sys.stdout.write(json.dumps(out) + "\n")
    return 0 if out.get("registered") else 1


def _cmd_close(args: argparse.Namespace) -> int:
    n = watch.mark_closed(
        prospect_id=args.prospect_id,
        reason=args.reason,
        message_id=args.message_id or "",
    )
    sys.stdout.write(json.dumps({"closed_count": n}) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Open-no-click nudge watcher CLI.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("tick", help="Run one watcher pass.")
    t.add_argument("--now", default=None, help="Override 'now' (ISO Z, for tests).")
    t.add_argument("--dry-run", action="store_true", help="Plan only — never send.")
    t.add_argument(
        "--force-fire", action="store_true", help="Send regardless of the flag (operator override)."
    )
    t.set_defaults(func=_cmd_tick)

    r = sub.add_parser("register", help="Add a watch record.")
    r.add_argument("--prospect-id", required=True)
    r.add_argument("--email", required=True)
    r.add_argument("--sent-at", required=True, help='Send timestamp, "YYYY-MM-DDTHH:MM:SSZ"')
    r.add_argument("--subject", required=True)
    r.add_argument("--buy-url", required=True)
    r.add_argument("--message-id", default="")
    r.add_argument("--company", default="")
    r.add_argument("--campaign-id", default="")
    r.set_defaults(func=_cmd_register)

    c = sub.add_parser("close", help="Close a watch record (e.g. closed_won).")
    c.add_argument("--prospect-id", required=True)
    c.add_argument("--reason", required=True, help='e.g. "closed_won", "manual", "stale".')
    c.add_argument("--message-id", default="")
    c.set_defaults(func=_cmd_close)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
