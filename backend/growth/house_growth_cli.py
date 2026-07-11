"""Operator CLI for the Hustleforge Client Zero growth engine.

A runnable surface for the house-growth loop without touching any existing app:

  python -m backend.growth.house_growth_cli run          # one observe->decide->record pass
  python -m backend.growth.house_growth_cli scorecard     # recent scorecards + SEO trend
  python -m backend.growth.house_growth_cli profile       # the loaded Brand Brain

Schedule the ``run`` command (host scheduled task / cron, weekly) to make the
loop recurring; it is idempotent and safe on any cadence. Disable with
``SAMUS_HOUSE_GROWTH_ENABLED=0``.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import house_account, house_growth_ledger, house_growth_tick


def _cmd_run(_args: argparse.Namespace) -> int:
    print(json.dumps(house_growth_tick.run_house_growth_tick(), indent=2, default=str))
    return 0


def _cmd_scorecard(args: argparse.Namespace) -> int:
    view = house_growth_ledger.recent_scorecards(limit=args.limit)
    trend = house_growth_ledger.scorecard_trend("seo_score", window=args.limit)
    print(json.dumps({"recent": view, "seo_trend": trend}, indent=2, default=str))
    return 0


def _cmd_profile(_args: argparse.Namespace) -> int:
    p = house_account.load_brand_profile()
    print(
        json.dumps(
            {
                "account_id": p.account_id,
                "client_key": house_account.house_client_key(),
                "domain": p.domain,
                "revenue_goal_monthly_usd": p.revenue_goal_monthly_usd,
                "offer_ladder": p.offer_ladder_lines(),
                "content_pillars": list(p.content_pillars),
                "target_keywords": list(p.target_keywords),
                "channels": list(p.channels),
            },
            indent=2,
            default=str,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="house_growth_cli",
        description="Hustleforge Client Zero growth engine.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="run one observe->decide->record pass").set_defaults(func=_cmd_run)

    sc = sub.add_parser("scorecard", help="show recent scorecards + SEO trend")
    sc.add_argument("--limit", type=int, default=12, help="rows / trend window (default 12)")
    sc.set_defaults(func=_cmd_scorecard)

    sub.add_parser("profile", help="show the loaded Brand Brain").set_defaults(func=_cmd_profile)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
