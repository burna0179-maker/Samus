"""Provision the recipient-index DynamoDB table (email -> prospect/opportunity).

The cash engine writes ``email -> {prospect_id, opportunity_id}`` here when it
composes outreach; the feedback workcell reads it on an SES bounce/complaint to
resolve the address back to a deal and halt it.

DRY-RUN by default (prints the plan, no AWS). ``--apply`` creates the table
(PK=``email`` String, on-demand billing) via the SQS-style provisioning helper;
idempotent — a pre-existing table is reported, not recreated. Needs AWS creds +
region only on ``--apply``.

Run from the Samus repo root:

    python scripts/provision_recipient_index_ddb.py            # dry-run
    python scripts/provision_recipient_index_ddb.py --apply    # provision
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_DEFAULT_TABLE = "samus_recipient_index"
_PARTITION_KEY = "email"


def _resolve_region(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if env:
        return env
    try:
        from backend.common.settings import get_settings
        return get_settings().aws_region or ""
    except Exception:  # noqa: BLE001 — settings optional for a dry-run plan
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="provision_recipient_index_ddb",
        description="Create the recipient-index DynamoDB table.",
    )
    parser.add_argument("--table-name", default=_DEFAULT_TABLE)
    parser.add_argument("--region", default=None, help="AWS region (default: env / settings)")
    parser.add_argument("--apply", action="store_true",
                        help="actually create the table (default: dry-run)")
    args = parser.parse_args(argv)

    region = _resolve_region(args.region)

    if not args.apply:
        print("== DRY RUN (no AWS calls) — re-run with --apply to provision ==")
        print(f"  region        : {region or '<UNSET — set AWS_REGION>'}")
        print(f"  table         : {args.table_name}")
        print(f"  partition key : {_PARTITION_KEY} (String)")
        print(f"  billing       : PAY_PER_REQUEST (on-demand)")
        print(f"\n  Default name matches settings.ddb_recipient_index_table; override "
              f"with DDB_RECIPIENT_INDEX_TABLE if you rename it.")
        return 0

    if not region:
        print("ERROR: no AWS region resolved — set AWS_REGION before --apply.",
              file=sys.stderr)
        return 2

    try:
        from backend.common.dynamodb import ensure_table
        result = ensure_table(
            args.table_name, partition_key=_PARTITION_KEY, region=region,
        )
    except Exception as exc:  # noqa: BLE001 — surface AWS/credential errors cleanly
        print(f"ERROR: provisioning failed: {exc}", file=sys.stderr)
        return 1

    print("== PROVISIONED ==")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
