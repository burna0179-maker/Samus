"""Portfolio scoring + action classification — port of recovery/campaign_optimizer.py.

Pure-Python ranking + tiering. No state.
"""
from __future__ import annotations

from .models import Opportunity


_SIGNAL_MOMENTUM_WEIGHTS: dict[str, float] = {
    "email_open": 0.2,
    "link_click": 0.3,
    "pricing_request": 0.5,
    "reply": 0.4,
    "demo_request": 0.6,
}


def momentum_from_signals(signals: list[str]) -> float:
    """Cap-1.0 sum of per-signal momentum weights."""
    score = 0.0
    for sig in signals or []:
        score += _SIGNAL_MOMENTUM_WEIGHTS.get(sig, 0.0)
    return min(score, 1.0)


def score_opportunity(opp: Opportunity) -> float:
    """expected_value × conversion_prob × momentum_boost × time_penalty − execution_cost.

    Clamped to ≥ 0 so deprioritized opportunities don't drag a portfolio total negative.
    """
    momentum_boost = 1.0 + max(0.0, float(opp.momentum))
    time_penalty = 1.0 / max(1, int(opp.time_to_close))
    raw = float(opp.expected_value) * float(opp.conversion_prob) * momentum_boost * time_penalty
    return max(0.0, raw - float(opp.execution_cost))


def classify_action(rank: int, score: float, top_tier: int, mid_tier: int) -> str:
    """Map (rank, score) → ``accelerate | maintain | defer | deprioritize``.

    - score ≤ 0 → deprioritize regardless of rank
    - rank < top_tier → accelerate
    - rank < mid_tier → maintain
    - else → defer
    """
    if score <= 0:
        return "deprioritize"
    if rank < top_tier:
        return "accelerate"
    if rank < mid_tier:
        return "maintain"
    return "defer"
