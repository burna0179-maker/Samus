"""Apply call pattern learnings to the callsheet generation pipeline.

Reads a PatternReport and writes dynamic callsheet intel to:
  <artifact_root>/voice/dynamic_callsheet_intel.json

This file is loaded by backend.prospecting.callsheet at build time to:
  - replace / augment the static hardcoded _OBJECTIONS tuple with handlers
    learned from real calls (highest-effectiveness handlers win)
  - provide "what to listen for" signals for the lead qualification pod
  - surface talking point guidance for the LLM callsheet path

The static fallback in callsheet.py always applies when this file is absent
or incomplete, so no degradation on fresh installs.
"""

from __future__ import annotations

import json
import logging

from backend.common import storage
from backend.common.dates import iso_now

from .call_strategy_reasoner import StrategyBrief, load_strategy_brief
from .pattern_aggregator import PatternReport, load_pattern_report

_LOG = logging.getLogger("samus.voice.callsheet_updater")

_DYNAMIC_INTEL_FILE = "voice/dynamic_callsheet_intel.json"

# Effectiveness threshold for promoting a learned handler into the callsheet
_MIN_EFFECTIVENESS = 0.5
# Minimum call count before we trust the pattern
_MIN_OBJECTION_COUNT = 2


def update_callsheet_intel(
    *,
    report: PatternReport | None = None,
    strategy: StrategyBrief | None = None,
) -> dict:
    """Translate the pattern report into dynamic callsheet intel and persist it.

    Returns the written intel dict (empty dict on no-op).
    """
    if report is None:
        report = load_pattern_report()
    if report is None:
        _LOG.info("callsheet_updater: no pattern report yet — nothing to update")
        return {}

    objection_handlers: list[str] = []
    flagged_gaps: list[str] = []

    for obj in report.top_objections:
        if obj.count < _MIN_OBJECTION_COUNT:
            continue

        if obj.avg_handling_effectiveness >= _MIN_EFFECTIVENESS and obj.best_handler:
            handler = f"{obj.text.strip().title()} — '{obj.best_handler.strip()}'"
            objection_handlers.append(handler)
        else:
            flagged_gaps.append(
                f"{obj.text.strip().title()} — "
                f"[NEEDS STRONGER HANDLER: {obj.count}x occurrences, "
                f"{obj.avg_handling_effectiveness:.0%} effectiveness]"
            )

    # Gaps go at the end so the callsheet shows them but they stand out
    all_handlers = objection_handlers + flagged_gaps

    # ── Strategy brief (day-over-day reasoning) ─────────────────────
    if strategy is None:
        strategy = load_strategy_brief()

    strategy_section: dict = {}
    if strategy and not strategy.llm_error:
        strategy_section = {
            "synthesis": strategy.synthesis,
            "recommendations": [r for r in strategy.recommendations if not r.startswith("[KEEP]")],
            "keep_doing": [
                r.removeprefix("[KEEP] ")
                for r in strategy.recommendations
                if r.startswith("[KEEP]")
            ],
            "trend_deltas": [
                {
                    "metric": d.metric,
                    "previous": d.previous,
                    "current": d.current,
                    "direction": d.direction,
                }
                for d in strategy.trend_deltas
            ],
            "days_with_data": strategy.days_with_data,
        }

    intel = {
        "generated_ts": iso_now(),
        "based_on_call_count": report.total_calls_analyzed,
        "positive_outcome_rate": report.positive_outcome_rate,
        "avg_reward": report.avg_reward,
        "strategy_brief": strategy_section,
        "objection_handlers": all_handlers,
        "what_to_listen_for": report.what_to_listen_for,
        "conversion_signals": report.top_conversion_signals,
        "talking_points_to_emphasize": report.talking_points_landed,
        "talking_points_to_avoid": report.talking_points_flopped,
        "script_guidance": report.script_feedback_summary,
        "outcome_distribution": report.outcome_distribution,
    }

    try:
        target = storage.root() / _DYNAMIC_INTEL_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(intel, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        _LOG.info(
            "callsheet_updater: wrote intel — %d calls, %.0f%% positive, "
            "%d objection handlers (%d gaps flagged)",
            report.total_calls_analyzed,
            report.positive_outcome_rate * 100,
            len(objection_handlers),
            len(flagged_gaps),
        )
    except OSError as exc:
        _LOG.warning("callsheet_updater persist failed: %s", exc)

    return intel


def load_dynamic_callsheet_intel() -> dict | None:
    """Return the current dynamic intel dict, or None if unavailable."""
    target = storage.root() / _DYNAMIC_INTEL_FILE
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("load_dynamic_callsheet_intel failed: %s", exc)
        return None


def get_dynamic_objections() -> tuple[str, ...] | None:
    """Return dynamic objection handler tuple for callsheet.py (or None if absent)."""
    intel = load_dynamic_callsheet_intel()
    if not intel:
        return None
    handlers = intel.get("objection_handlers") or []
    if not handlers:
        return None
    return tuple(str(h) for h in handlers)
