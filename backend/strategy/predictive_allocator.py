"""Predictive allocation — forecast scoring + proactive-shift decision.

The reactive UCB1 bandit only learns *after* an arm under-performs. This
module scores each industry vertical's *forecast* so the portfolio can shift
capital BEFORE the bandit's regret accumulates: prediction beats reaction.

Pure-logic — deterministic, no I/O. Consumes :class:`IndustryForecast`
records (see :mod:`backend.strategy.momentum_tracker`).

Phase-2 interface
-----------------
``closer_mode_for`` is the contract a future ``autonomous_closer`` will
consume. The closer itself lives in ``recovery/`` and is NOT a deployed
workcell — this helper is a pure mapping only, it wires nothing live.
"""
from __future__ import annotations

from .momentum_tracker import IndustryForecast

__all__ = [
    "forecast_score",
    "should_proactively_shift",
    "closer_mode_for",
    "FORECAST_REWARD_DENSITY_WEIGHT",
    "FORECAST_MOMENTUM_WEIGHT",
    "FORECAST_EMA_TREND_WEIGHT",
    "FORECAST_TOKEN_EFFICIENCY_WEIGHT",
    "PROACTIVE_SHIFT_THRESHOLD_DEFAULT",
    "CLOSER_AGGRESSIVE_THRESHOLD",
    "CLOSER_HYBRID_THRESHOLD",
]

# --- forecast_score coefficients (named — no magic numbers) ----------------
FORECAST_REWARD_DENSITY_WEIGHT: float = 0.40
FORECAST_MOMENTUM_WEIGHT: float = 0.25
FORECAST_EMA_TREND_WEIGHT: float = 0.20
FORECAST_TOKEN_EFFICIENCY_WEIGHT: float = 0.15
# saturation_risk is subtracted at unit weight.

# Default threshold at which a forecast warrants a proactive portfolio shift.
PROACTIVE_SHIFT_THRESHOLD_DEFAULT: float = 0.6

# closer_mode_for thresholds (Phase-2 interface).
CLOSER_AGGRESSIVE_THRESHOLD: float = 0.82
CLOSER_HYBRID_THRESHOLD: float = 0.65


def forecast_score(f: IndustryForecast) -> float:
    """Score a vertical's forward outlook — higher means more worth capital.

    Formula (every coefficient is a named module constant)::

        score = reward_density*0.40 + momentum*0.25 + ema_trend*0.20
                + token_efficiency*0.15 - saturation_risk

    Saturation risk is a straight subtraction: a crowded vertical is
    penalised regardless of how strong its other signals look.
    """
    return (
        f.reward_density * FORECAST_REWARD_DENSITY_WEIGHT
        + f.momentum * FORECAST_MOMENTUM_WEIGHT
        + f.ema_trend * FORECAST_EMA_TREND_WEIGHT
        + f.token_efficiency * FORECAST_TOKEN_EFFICIENCY_WEIGHT
        - f.saturation_risk
    )


def should_proactively_shift(
    forecasts: list[IndustryForecast],
    *,
    threshold: float = PROACTIVE_SHIFT_THRESHOLD_DEFAULT,
) -> bool:
    """True when any vertical's forecast score clears ``threshold``.

    A single vertical scoring above the threshold is enough — that vertical
    is a strong-enough forward bet that the portfolio should re-plan now
    rather than wait for the reactive bandit to catch up. An empty forecast
    list returns False (no signal to act on).
    """
    if not forecasts:
        return False
    return any(forecast_score(f) >= threshold for f in forecasts)


def closer_mode_for(forecast_score_value: float) -> str:
    """Map a forecast score to a closer mode — the Phase-2 closer interface.

    Returns ``"aggressive"`` above 0.82, ``"hybrid"`` above 0.65, else
    ``"template_only"``. This is a pure mapping; the ``autonomous_closer``
    that will consume it lives in ``recovery/`` and is not wired live.
    """
    if forecast_score_value > CLOSER_AGGRESSIVE_THRESHOLD:
        return "aggressive"
    if forecast_score_value > CLOSER_HYBRID_THRESHOLD:
        return "hybrid"
    return "template_only"
