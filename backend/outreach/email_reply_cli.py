"""Operator CLI for the email-reply buying-signal enrollment.

The voice buying-signal route is fed automatically by the Vapi end-of-call
webhook. The email side has no inbound-parse pipeline yet, so when a paid-
audit recipient (or any outbound-email target) replies expressing interest,
the operator runs this CLI to enroll them into the same warm sequence.

Usage::

    # Classify-and-enroll from a reply body
    python -m backend.outreach.email_reply_cli \\
        --prospect-id pr_ChIJq6p6781Hm4ARqfRR \\
        --email kellyzrealtor@gmail.com \\
        --company "Kelly Zimmerman, eXp Realty" \\
        --reply "Yes, very interested — what's the price and when can we start?"

    # Operator override: skip the classifier (use when the signal came
    # through a non-text channel — phone confirmation, a third-party
    # forwarded reply, etc.) and enroll at a fixed high score.
    python -m backend.outreach.email_reply_cli \\
        --prospect-id pr_ChIJq6p6781Hm4ARqfRR \\
        --email kellyzrealtor@gmail.com \\
        --company "Kelly Zimmerman, eXp Realty" \\
        --override

Output is one JSON line on stdout so the script composes cleanly into
other tools. Exit code is 0 on enroll, 2 on classifier-rejected, 1 on
configuration / persistence failure.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from backend.outreach.buying_signal_route import (
    maybe_enroll_buying_signal_from_email_reply,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Enroll a prospect into the buying_signal warm sequence based "
            "on an email reply (or operator override)."
        ),
    )
    parser.add_argument("--prospect-id", required=True,
                        help="CRM prospect_id (e.g. pr_ChIJ...)")
    parser.add_argument("--email", default="",
                        help="Prospect email — used by dispatch when armed")
    parser.add_argument("--company", default="",
                        help="Display company name")
    parser.add_argument("--reply", default="",
                        help="Raw reply body (required unless --override)")
    parser.add_argument("--override", action="store_true",
                        help="Skip classifier; enroll at fixed high score "
                             "(operator escape hatch)")
    args = parser.parse_args(argv)

    if not args.override and not args.reply.strip():
        sys.stderr.write("--reply is required unless --override is set\n")
        return 1

    result = maybe_enroll_buying_signal_from_email_reply(
        prospect_id=args.prospect_id,
        reply_text=args.reply,
        now_iso=_now_iso(),
        email=args.email,
        company=args.company,
        operator_override=args.override,
    )
    sys.stdout.write(json.dumps(result) + "\n")
    if result.get("enrolled"):
        return 0
    if result.get("reason") == "not_a_buying_signal":
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
