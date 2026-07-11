"""Operational friction — where the control loop is leaking energy.

Topic-5 (Economic Thermodynamics) delta. The entropy workcell scores *systemic
instability* (queue/error/retry signals), but not the friction of the control
loop's own DECISIONS: is it thrashing — cutting a workcell's quota one tick and
restoring it the next — and how many workcells is it juggling per pass? Those are
the "organizational energy leaks" that no instability score captures.

This module reads the control-tick ledger (the record of every observe→decide
pass) and computes two friction metrics over a recent window:

  * ``decision_entropy`` — the mean rate at which per-workcell quota-cut
    decisions FLIP across ticks. High = the loop cannot settle (oscillation /
    context-switching cost).
  * ``coordination_cost`` — the mean number of workcells adjusted per tick. High
    = the loop is coordinating many units at once (coordination overhead).

Read-only over telemetry; ``ticks`` is injectable for testing. Fail-soft.
"""
from __future__ import annotations

import logging
from typing import Any

_LOG = logging.getLogger("samus.entropy.friction")

# A workcell that flips its quota-cut decision more than this fraction of the
# time, or an average coordination load above this, is flagged as leaking energy.
_DECISION_ENTROPY_LEAK = 0.4
_COORDINATION_LEAK = 3.0

__all__ = ["friction_report", "_DECISION_ENTROPY_LEAK", "_COORDINATION_LEAK"]


def _tick_cut_set(tick: dict[str, Any]) -> set[str]:
    """Workcells that took a quota cut in this tick (from the recommendations)."""
    recs = (tick.get("recommendations") or {}).get("workcell_adjustments") or []
    return {a["workcell"] for a in recs if a.get("quota_cut")}


def _adjustment_count(tick: dict[str, Any]) -> int:
    recs = (tick.get("recommendations") or {}).get("workcell_adjustments") or []
    return len(recs)


def friction_report(
    limit: int = 50,
    *,
    ticks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compute decision_entropy + coordination_cost over recent control ticks."""
    if ticks is None:
        try:
            from backend.common.control_tick_ledger import recent_ticks

            ticks = recent_ticks(limit).get("ticks") or []
        except Exception as exc:  # noqa: BLE001 — telemetry read never blocks
            _LOG.warning("friction read failed: %s", exc)
            ticks = []

    n = len(ticks)
    if n == 0:
        return {"ticks_analyzed": 0, "decision_entropy": 0.0,
                "coordination_cost": 0.0, "per_workcell": {}, "energy_leak": False}

    # Per-workcell boolean sequence: was it quota-cut in each tick (oldest->newest)?
    cut_sets = [_tick_cut_set(t) for t in ticks]
    all_workcells = set().union(*cut_sets) if cut_sets else set()
    per_workcell: dict[str, dict[str, Any]] = {}
    flip_rates: list[float] = []
    for wc in sorted(all_workcells):
        seq = [wc in s for s in cut_sets]
        flips = sum(1 for i in range(1, n) if seq[i] != seq[i - 1])
        flip_rate = round(flips / (n - 1), 4) if n > 1 else 0.0
        flip_rates.append(flip_rate)
        per_workcell[wc] = {
            "cut_count": sum(seq), "flips": flips, "flip_rate": flip_rate,
        }

    decision_entropy = round(sum(flip_rates) / len(flip_rates), 4) if flip_rates else 0.0
    coordination_cost = round(sum(_adjustment_count(t) for t in ticks) / n, 4)
    energy_leak = bool(
        decision_entropy > _DECISION_ENTROPY_LEAK
        or coordination_cost > _COORDINATION_LEAK
    )
    return {
        "ticks_analyzed": n,
        "decision_entropy": decision_entropy,
        "coordination_cost": coordination_cost,
        "per_workcell": per_workcell,
        "energy_leak": energy_leak,
    }
