"""Two-proportion z-test — the promotion significance gate (stdlib only).

Compares wins/trials of two experiment arms with the classic pooled
two-proportion z-test. No scipy/numpy — the normal CDF comes from
``math.erf`` (exact for this purpose).

``is_significant(a_stats, b_stats)`` answers "is arm A's win rate different
from arm B's at level alpha?" (two-sided). Promotion callers pass the
candidate winner as ``a_stats`` and check ``a`` also has the HIGHER rate.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

__all__ = ["two_proportion_z", "p_value_two_sided", "is_significant"]


def _wins_trials(stats: Mapping[str, Any]) -> tuple[int, int]:
    try:
        wins = max(0, int(stats.get("wins", 0) or 0))
    except (TypeError, ValueError):
        wins = 0
    try:
        trials = max(0, int(stats.get("trials", 0) or 0))
    except (TypeError, ValueError):
        trials = 0
    return min(wins, trials), trials


def two_proportion_z(
    a_wins: int, a_trials: int, b_wins: int, b_trials: int,
) -> float:
    """Pooled two-proportion z statistic. 0.0 when undefined (empty arms)."""
    if a_trials <= 0 or b_trials <= 0:
        return 0.0
    p_a = a_wins / a_trials
    p_b = b_wins / b_trials
    pooled = (a_wins + b_wins) / (a_trials + b_trials)
    denom = pooled * (1.0 - pooled) * (1.0 / a_trials + 1.0 / b_trials)
    if denom <= 0.0:
        return 0.0  # both arms at 0% or 100% — no variance to test against
    return (p_a - p_b) / math.sqrt(denom)


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def p_value_two_sided(z: float) -> float:
    """Two-sided p-value for a z statistic."""
    return 2.0 * (1.0 - _normal_cdf(abs(z)))


def is_significant(
    a_stats: Mapping[str, Any],
    b_stats: Mapping[str, Any],
    alpha: float = 0.05,
) -> bool:
    """True when the two arms' win rates differ at level ``alpha``.

    ``a_stats`` / ``b_stats`` are ``{"wins": int, "trials": int}`` mappings
    (extra keys ignored — an attribution-store snapshot works directly).
    Arms with zero trials are never significant.
    """
    a_wins, a_trials = _wins_trials(a_stats)
    b_wins, b_trials = _wins_trials(b_stats)
    if a_trials == 0 or b_trials == 0:
        return False
    z = two_proportion_z(a_wins, a_trials, b_wins, b_trials)
    return p_value_two_sided(z) < alpha
