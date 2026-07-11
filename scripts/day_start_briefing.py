"""Pre-shift strategic briefing + end-of-day review (CLI wrapper).

Thin entrypoint over backend.cognitive.intelligence_cycle. Samus composes its
intelligence package from live production state, consults OpenAI for guidance,
and ingests it into the durable guidance ledger.

    python -m scripts.day_start_briefing              # pre-shift briefing
    python -m scripts.day_start_briefing --review     # end-of-day review

The autonomous Cloud Run path uses the gateway routes
(/api/gateway/pre_shift_briefing, /api/gateway/end_of_day_review) on a Cloud
Scheduler cadence; this script is the operator's manual/offline trigger.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.cognitive.intelligence_cycle import (  # noqa: E402
    run_end_of_day_review,
    run_pre_shift_briefing,
)


def _print_records(result: dict) -> None:
    led_summary = result.get("summary") or result.get("effectiveness") or {}
    print("=" * 70)
    print(result.get("briefing") or result.get("review") or "")
    print()
    raw = result.get("raw_guidance")
    if raw:
        print("=" * 70)
        print("OPENAI GUIDANCE (raw)")
        print("=" * 70)
        print(raw)
        print()
    print("=" * 70)
    print(f"INGESTED {result.get('ingested', 0)} recommendations -> guidance ledger")
    if result.get("error"):
        print(f"  (LLM/ingest note: {result['error']})")
    print("=" * 70)
    print(f"LEDGER STATE: {json.dumps(led_summary)}")
    print("=" * 70)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="Samus strategic intelligence cycle")
    ap.add_argument("--review", action="store_true", help="run the end-of-day review instead of the pre-shift briefing")
    ap.add_argument("--no-openai", action="store_true", help="(review only) skip the OpenAI tomorrow-guidance call")
    args = ap.parse_args()

    if args.review:
        result = run_end_of_day_review(consult_openai=not args.no_openai)
    else:
        result = run_pre_shift_briefing()
    _print_records(result)


if __name__ == "__main__":
    main()
