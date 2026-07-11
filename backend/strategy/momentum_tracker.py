"""Per-vertical momentum + EMA-trend tracking.

Converts a time-ordered metric series (typically reward-density readings for
one industry vertical) into two scalars:

  - ``momentum``   — recent rate-of-change, normalised to [-1, 1].
  - ``ema_trend``  — exponential-moving-average velocity, the smoothed
    direction the vertical is heading.

Pure-logic — deterministic, no I/O. The predictive allocator
(:mod:`backend.strategy.predictive_allocator`) consumes
:class:`IndustryForecast` records built from these functions.
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "IndustryForecast",
    "compute_momentum",
    "compute_ema_trend",
    "compute_ema",
    "EMA_SMOOTHING_DEFAULT",
]

# Default EMA smoothing factor (alpha). Higher = more weight on recent points.
EMA_SMOOTHING_DEFAULT: float = 0.4


@dataclass
class IndustryForecast:
    """Forward-looking profile for one industry vertical.

    Built by the caller from a vertical's metric history; consumed by
    :func:`backend.strategy.predictive_allocator.forecast_score`.
    """

    vertical: str
    reward_density: float          # latest / representative reward density
    momentum: float                # recent rate-of-change, [-1, 1]
    ema_trend: float               # smoothed velocity of the EMA
    token_efficiency: float        # close-prob per token-dollar, normalised
    saturation_risk: float         # over-exploration risk, [0, 1]


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp ``value`` into ``[lo, hi]``."""
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def compute_momentum(history: list[float]) -> float:
    """Recent rate-of-change of a metric series, normalised to [-1, 1].

    Momentum = (last - first) / max(|first|, |last|, epsilon). With fewer
    than two points there is no change to measure, so momentum is 0.0.
    Deterministic and side-effect free.
    """
    if not history or len(history) < 2:
        return 0.0
    first = history[0]
    last = history[-1]
    denom = max(abs(first), abs(last), 1e-9)
    return _clamp((last - first) / denom, -1.0, 1.0)


def compute_ema(history: list[float], *, alpha: float = EMA_SMOOTHING_DEFAULT) -> float:
    """Exponential moving average of a metric series.

    ``alpha`` in (0, 1]; higher weights recent points more heavily. An
    empty series returns 0.0; a one-point series returns that point.
    """
    if not history:
        return 0.0
    a = _clamp(alpha, 1e-6, 1.0)
    ema = history[0]
    for value in history[1:]:
        ema = a * value + (1.0 - a) * ema
    return ema


def compute_ema_trend(
    history: list[float], *, alpha: float = EMA_SMOOTHING_DEFAULT
) -> float:
    """Smoothed velocity of the metric series — the EMA's direction.

    Computed as the EMA of the recent half of the series minus the EMA of
    the earlier half; positive means the vertical is trending up. With
    fewer than two points there is no trend, so returns 0.0.

    Deterministic, no I/O.
    """
    if not history or len(history) < 2:
        return 0.0
    midpoint = len(history) // 2
    earlier = history[:midpoint] or history[:1]
    recent = history[midpoint:]
    return compute_ema(recent, alpha=alpha) - compute_ema(earlier, alpha=alpha)
