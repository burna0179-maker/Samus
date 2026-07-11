"""CLI — produce the daily call_list_YYYY-MM-DD.csv.

Usage:
    python -m backend.prospecting.run_daily \\
        --zipcodes 95993,95991 --industries finance,roofing

Runs the prospecting pipeline in-process and writes the CSV under the Samus
artifact root (``SAMUS_ARTIFACT_ROOT`` env override or default
``E:\\Hustleforge\\Samus\\data\\artifacts\\daily_calls\\``).
"""
from __future__ import annotations

import argparse
import logging
import sys

from .models import DiscoveryRequest
from .service import process_discovery


_LOG = logging.getLogger("samus.prospecting.run_daily")


def _split_csv_arg(raw: str) -> list[str]:
    return [piece.strip() for piece in raw.split(",") if piece.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="backend.prospecting.run_daily",
        description="Run the daily Samus prospecting pipeline; emit call_list_*.csv.",
    )
    parser.add_argument("--zipcodes", required=True,
                        help="Comma-separated zipcodes, e.g. 95993,95991")
    parser.add_argument("--industries", default="",
                        help="Comma-separated industries / Google Places keywords")
    parser.add_argument("--max-per-zip", type=int, default=25)
    parser.add_argument("--radius-miles", type=int, default=15)
    parser.add_argument("--include-no-website", action="store_true",
                        help="Keep prospects with no website at discovery; they "
                             "are tagged website_status=no_website and ranked as "
                             "top web-design leads on the morning call list")
    parser.add_argument("--campaign", default="daily_call_list")
    parser.add_argument("--task-id", default=None,
                        help="Idempotency key (default: campaign + today's date)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Load the Codex registry. It is a per-process singleton and is otherwise
    # only loaded inside the FastAPI app lifespan (backend.common.app_factory).
    # This CLI runs as a standalone `python -m backend.prospecting.run_daily`
    # process that never touches app_factory, so without this call the registry
    # stays "not loaded" and the warm/hot full audit's check_action() raises
    # CodexUnavailable and fail-closes on EVERY prospect — burning the whole run
    # on doomed work while producing no enriched reports. Mirror app_factory's
    # boot-time load(). Unlike the app (which refuses to boot on parse failure),
    # this loud-but-non-fatal: the call list itself is an internal dial sheet,
    # not outreach, so a Codex parse fault must not deny the operator the
    # morning list — the per-prospect audit already fail-closes outreach
    # validation safely on its own.
    from backend.common.codex import REGISTRY

    if not REGISTRY.is_loaded():
        try:
            REGISTRY.load()
        except Exception as exc:  # noqa: BLE001 — defensive boundary
            _LOG.error(
                "codex registry load failed (%s) — warm/hot full audits will "
                "fail-closed and ship no SEO reports; call list still produced. "
                "Fix the Codex parse error, do not bypass.",
                exc,
            )

    zipcodes = _split_csv_arg(args.zipcodes)
    industries = _split_csv_arg(args.industries)
    if not zipcodes:
        parser.error("--zipcodes must contain at least one zipcode")

    request = DiscoveryRequest(
        campaign_name=args.campaign,
        zipcodes=zipcodes,
        industries=industries,
        max_results_per_zip=args.max_per_zip,
        radius_miles=args.radius_miles,
        must_have_website=not args.include_no_website,
    )

    try:
        result = process_discovery(request, task_id=args.task_id)
    except Exception as exc:
        _LOG.error("prospecting pipeline failed: %s", exc)
        return 2

    cache_note = " (cache hit)" if result.cache_hit else ""
    print(f"wrote {result.prospect_count} prospects{cache_note}")
    print(f"  CSV  -> {result.csv_path}")
    if result.txt_path:
        print(f"  TXT  -> {result.txt_path}  (morning call list)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
