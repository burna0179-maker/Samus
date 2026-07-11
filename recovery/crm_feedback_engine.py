#!/usr/bin/env python3
"""
CRMFeedbackEngine — outcome tracking + angle performance + auto-optimization
Source: ChatGPT recovery chat 03 (crm_feedback_engine.py section)

Canonical relationship:
- [EXPANDS §6 data plane] outcome / objection / angle metrics
- [EXPANDS §6 observability] performance projection over time
- [DEFERRED CRITICAL] persistence — currently in-memory, needs PostgreSQL/Supabase/SQLite
  Tables required: calls / objections / outcomes / angle_performance
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple


METRICS: Dict[str, Any] = {
    "objections": defaultdict(int),
    "closes": defaultdict(int),
    "failures": defaultdict(int),
    "angles": defaultdict(lambda: {"wins": 0, "losses": 0}),
}


def log_interaction(
    prospect_id: str,
    outcome: str,
    objection: Optional[str],
    product: str,
    angle: str,
) -> None:
    if objection:
        METRICS["objections"][objection] += 1
    if outcome == "closed":
        METRICS["closes"][product] += 1
        METRICS["angles"][angle]["wins"] += 1
    else:
        METRICS["failures"][product] += 1
        METRICS["angles"][angle]["losses"] += 1


def get_top_objections() -> List[Tuple[str, int]]:
    return sorted(METRICS["objections"].items(), key=lambda x: x[1], reverse=True)


def get_best_products() -> List[Tuple[str, int]]:
    return sorted(METRICS["closes"].items(), key=lambda x: x[1], reverse=True)


def get_angle_performance() -> Dict[str, float]:
    results: Dict[str, float] = {}
    for angle, data in METRICS["angles"].items():
        total = data["wins"] + data["losses"]
        if total == 0:
            continue
        results[angle] = data["wins"] / total
    return results


def optimize_weights(intel: Dict[str, Any]) -> Dict[str, Any]:
    """Adjust opportunity scoring dynamically based on performance."""
    angle_perf = get_angle_performance()
    if angle_perf:
        best_angle = max(angle_perf, key=angle_perf.get)
        intel.setdefault("strategy", {})["angle_bias"] = best_angle
    return intel


def snapshot() -> Dict[str, Any]:
    return {
        "objections": dict(METRICS["objections"]),
        "closes": dict(METRICS["closes"]),
        "failures": dict(METRICS["failures"]),
        "angles": {k: dict(v) for k, v in METRICS["angles"].items()},
    }
