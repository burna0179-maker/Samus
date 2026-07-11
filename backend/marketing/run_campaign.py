"""Cron entry point — runs one self-marketing monthly cycle for the current month.

Designed for Windows Task Scheduler or cron. Mirrors
``backend.retainer.run_due_cycles``: idempotent, fail-soft, structured exit
codes. Re-runs in the same month resume from the persisted plan.json, skipping
steps that already completed.

Invoke directly::

    python -m backend.marketing.run_campaign           # current month, live
    python -m backend.marketing.run_campaign --dry-run # current month, no LLM/dispatch
    python -m backend.marketing.run_campaign --month 7 --year 2026

Exit codes:
    0 - cycle completed (ok=True), or every step done/skipped without crash
    1 - cycle finished with at least one failed step
    2 - hard crash before/during execution
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

_LOG = logging.getLogger("samus.marketing.run_campaign")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    now = datetime.now(timezone.utc)
    parser = argparse.ArgumentParser(
        description="Run one self-marketing monthly cycle.",
    )
    parser.add_argument(
        "--month", type=int, default=now.month,
        help="Calendar month (1-12). Default: current month.",
    )
    parser.add_argument(
        "--year", type=int, default=now.year,
        help="Four-digit year. Default: current year.",
    )
    parser.add_argument(
        "--campaign", type=str, default="hustleforge",
        help="Campaign id. Currently only 'hustleforge' is registered.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Skip all LLM calls and dispatch; exercise the DAG shape only.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.campaign.lower() != "hustleforge":
        print(f"marketing: unknown campaign {args.campaign!r}", file=sys.stderr)
        return 2

    try:
        from backend.marketing.campaign_cycle import run_monthly_campaign
        from backend.marketing.self_campaign import HUSTLEFORGE_CAMPAIGN
    except Exception as exc:  # noqa: BLE001 — boot/import failure
        print(f"marketing: import failed: {exc}", file=sys.stderr)
        return 2

    try:
        result = run_monthly_campaign(
            month=args.month,
            year=args.year,
            campaign=HUSTLEFORGE_CAMPAIGN,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"marketing: hard crash during cycle: {exc}", file=sys.stderr)
        return 2

    badge = "OK" if result.ok else "FAIL"
    print(f"[{badge}] campaign={result.campaign_id} cycle={result.cycle_month} plan={result.plan_path}")
    failures = 0
    for s in result.steps:
        flag = " " if s.status == "done" else ("F" if s.status == "failed" else "-")
        print(f"  [{flag}] {s.status:8s} {s.id:20s} ({s.elapsed_ms} ms) {s.detail[:120]}")
        if s.status == "failed":
            failures += 1
    if result.summary:
        print("summary:")
        for k, v in result.summary.items():
            print(f"  {k}: {v}")
    return 1 if failures or not result.ok else 0


if __name__ == "__main__":
    sys.exit(main())
