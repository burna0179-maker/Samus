"""Per-vertical saturation-risk monitoring.

Saturation risk measures how over-explored / crowded a vertical is: a
vertical that already absorbs a large share of recent bandit trials (or
allocation) yields diminishing returns and should be down-weighted by the
predictive allocator.

Pure-logic — deterministic, no I/O.
"""

from __future__ import annotations

__all__ = [
    "compute_saturation_risk",
    "saturation_risk_by_vertical",
    "SATURATION_FAIR_SHARE_FLOOR",
]

# Below this trial-share a vertical is considered under-explored (risk 0).
SATURATION_FAIR_SHARE_FLOOR: float = 0.1


def _clamp01(value: float) -> float:
    """Clamp a value into [0.0, 1.0]."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def compute_saturation_risk(vertical_trials: float, total_trials: float) -> float:
    """Saturation risk in [0,1] from a vertical's share of recent trials.

    ``share = vertical_trials / total_trials``. Risk ramps linearly from 0
    at the fair-share floor to 1.0 at full share; a vertical below the
    floor is treated as under-explored (risk 0.0). With no total trials
    there is no signal yet, so risk is 0.0.

    Pure function.
    """
    if total_trials <= 0.0:
        return 0.0
    share = _clamp01(vertical_trials / total_trials)
    if share <= SATURATION_FAIR_SHARE_FLOOR:
        return 0.0
    span = 1.0 - SATURATION_FAIR_SHARE_FLOOR
    if span <= 0.0:  # pragma: no cover - defensive
        return 1.0
    return _clamp01((share - SATURATION_FAIR_SHARE_FLOOR) / span)


def saturation_risk_by_vertical(
    trials_by_vertical: dict[str, float],
) -> dict[str, float]:
    """Saturation risk for every vertical given its recent trial counts.

    ``trials_by_vertical`` maps vertical -> trial count (or allocation
    share); the total is the sum. Returns a vertical -> risk map. An empty
    input yields an empty map. Pure function.
    """
    total = float(sum(trials_by_vertical.values()))
    return {
        vertical: compute_saturation_risk(float(count), total)
        for vertical, count in trials_by_vertical.items()
    }
