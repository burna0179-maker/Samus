"""Provision the Cash Engine SQS queue + DLQ + redrive policy.

DRY-RUN by default: prints exactly what it would create (queue names, region,
the redrive policy JSON) and touches no AWS. Pass ``--apply`` to actually
create the queues via the SQS API (``backend.common.sqs.ensure_queue_with_dlq``),
which requires AWS credentials + a region in the environment. On success it
prints the ``SQS_CASH_ENGINE_QUEUE_URL`` line to export so the gateway producer
and the ``samus-cash-engine-worker`` sidecar go live.

The operation is idempotent — re-running ``--apply`` reconciles attributes on
the existing queues rather than failing.

Run from the Samus repo root:

    python scripts/provision_cash_engine_sqs.py                  # dry-run plan
    python scripts/provision_cash_engine_sqs.py --apply          # provision
    python scripts/provision_cash_engine_sqs.py --apply --max-receive-count 3
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_DEFAULT_QUEUE = "samus-cash-engine-jobs"


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
        prog="provision_cash_engine_sqs",
        description="Create the Cash Engine SQS queue + DLQ + redrive policy.",
    )
    parser.add_argument("--queue-name", default=_DEFAULT_QUEUE)
    parser.add_argument("--region", default=None, help="AWS region (default: env / settings)")
    parser.add_argument("--max-receive-count", type=int, default=5,
                        help="deliveries before a message is redriven to the DLQ")
    parser.add_argument("--visibility-timeout", type=int, default=60)
    parser.add_argument("--apply", action="store_true",
                        help="actually create the queues (default: dry-run)")
    args = parser.parse_args(argv)

    region = _resolve_region(args.region)
    dlq_name = f"{args.queue_name}-dlq"

    if not args.apply:
        print("== DRY RUN (no AWS calls) — re-run with --apply to provision ==")
        print(f"  region              : {region or '<UNSET — set AWS_REGION>'}")
        print(f"  main queue          : {args.queue_name}")
        print(f"  dead-letter queue   : {dlq_name}")
        print(f"  visibility timeout  : {args.visibility_timeout}s")
        print(f"  message retention   : 14 days (SQS max)")
        print("  redrive policy      : "
              + json.dumps({
                  "deadLetterTargetArn": f"arn:aws:sqs:{region or '<region>'}:<account>:{dlq_name}",
                  "maxReceiveCount": args.max_receive_count,
              }))
        print(f"\n  Then export: SQS_CASH_ENGINE_QUEUE_URL=<printed on --apply>")
        return 0

    if not region:
        print("ERROR: no AWS region resolved — set AWS_REGION before --apply.",
              file=sys.stderr)
        return 2

    try:
        from backend.common.sqs import ensure_queue_with_dlq
        result = ensure_queue_with_dlq(
            args.queue_name,
            region=region,
            max_receive_count=args.max_receive_count,
            visibility_timeout=args.visibility_timeout,
        )
    except Exception as exc:  # noqa: BLE001 — surface AWS/credential errors cleanly
        print(f"ERROR: provisioning failed: {exc}", file=sys.stderr)
        return 1

    print("== PROVISIONED ==")
    print(json.dumps(result, indent=2))
    print(f"\nexport SQS_CASH_ENGINE_QUEUE_URL={result['queue_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
