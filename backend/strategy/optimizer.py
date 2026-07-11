"""Portfolio-level prospect ranker.

Ported from recovery/campaign_optimizer.py.

Tier-3 note: calibration weights (momentum signal values, tier boundary constants)
are seed values derived from first-principles reasoning. Tune against real CRM
conversion data once accumulated.

Canonical relationship:
- Portfolio layer above StrategyEngine; no canonical equivalent.
- Expands §6 autonomy: global decision (vs per-prospect StrategyEngine local decision).

Objective: maximise expected_revenue − execution_cost − time_decay_risk.

Allocation tiers (by rank after score sort):
  rank < TOP_TIER  (default 5)   → accelerate
  rank < MID_TIER  (default 15)  → maintain
  rank >= MID_TIER               → defer
  score <= 0                     → drop
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ProspectSignals",
    "PortfolioRanking",
    "score_prospect",
    "rank_portfolio",
    "momentum_from_signals",
    "TOP_TIER",
    "MID_TIER",
]

_LOG = logging.getLogger("samus.strategy.optimizer")

# Tier boundaries — seed constants; tune with conversion data.
TOP_TIER: int = 5
MID_TIER: int = 15


@dataclass
class ProspectSignals:
    """Input signals for a single prospect portfolio entry.

    All numeric fields have conservative defaults so callers can omit
    unknown values rather than guessing.
    """

    prospect_id: str
    expected_value: float = 1_000.0       # estimated deal value in $
    conversion_prob: float = 0.1          # 0.0 – 1.0
    time_to_close: int = 7                # days remaining; 1 minimum enforced
    execution_cost: float = 100.0         # cost to work this prospect
    stage: str = "new"
    momentum: float = 0.0                 # 0.0 – 1.0; pre-computed or from signals
    engagement_signals: list[str] = field(default_factory=list)


@dataclass
class PortfolioRanking:
    """Result record for a single prospect after portfolio scoring."""

    prospect_id: str
    score: float
    action: str                           # accelerate | maintain | defer | drop
    rank: int                             # 0-based position in sorted list
    raw_signals: dict[str, Any] = field(default_factory=dict)


def momentum_from_signals(signals: list[str]) -> float:
    """Convert engagement signal strings to a momentum float in [0.0, 1.0].

    Signal weights (seed values; tune with data):
      email_open      +0.2
      link_click      +0.3
      pricing_request +0.5

    Sum is capped at 1.0.
    """
    score = 0.0
    if "email_open" in signals:
        score += 0.2
    if "link_click" in signals:
        score += 0.3
    if "pricing_request" in signals:
        score += 0.5
    return min(score, 1.0)


def score_prospect(prospect_signals: ProspectSignals) -> float:
    """Compute a scalar score for one prospect.

    Formula::

        momentum_boost = 1 + prospect.momentum
        time_penalty   = 1 / max(time_to_close, 1)
        raw            = expected_value × conversion_prob × momentum_boost × time_penalty
        score          = max(raw − execution_cost, 0.0)

    Returns 0.0 for any unworkable prospect (cost exceeds gross EV).
    """
    # Honour pre-computed momentum if set; otherwise derive from engagement signals.
    effective_momentum = prospect_signals.momentum
    if effective_momentum == 0.0 and prospect_signals.engagement_signals:
        effective_momentum = momentum_from_signals(prospect_signals.engagement_signals)

    momentum_boost = 1.0 + effective_momentum
    time_penalty = 1.0 / max(prospect_signals.time_to_close, 1)
    raw = (
        prospect_signals.expected_value
        * prospect_signals.conversion_prob
        * momentum_boost
        * time_penalty
    )
    return max(raw - prospect_signals.execution_cost, 0.0)


def _classify_action(rank: int, score: float, top: int, mid: int) -> str:
    """Map rank + score to an action label.

    Score-zero prospects are always dropped regardless of rank.
    """
    if score <= 0.0:
        return "drop"
    if rank < top:
        return "accelerate"
    if rank < mid:
        return "maintain"
    return "defer"


def rank_portfolio(
    prospects: list[ProspectSignals],
    *,
    top_tier: int = TOP_TIER,
    mid_tier: int = MID_TIER,
) -> list[tuple[str, str]]:
    """Rank a list of prospects and assign portfolio actions.

    Returns a list of ``(prospect_id, action)`` tuples ordered from highest
    to lowest score. Action values: ``"accelerate"``, ``"maintain"``,
    ``"defer"``, ``"drop"``.

    Pure function — no I/O, no external calls.
    """
    if not prospects:
        _LOG.debug("rank_portfolio called with empty portfolio")
        return []

    scored: list[tuple[ProspectSignals, float]] = sorted(
        ((p, score_prospect(p)) for p in prospects),
        key=lambda t: t[1],
        reverse=True,
    )

    result: list[tuple[str, str]] = []
    for rank, (prospect, score) in enumerate(scored):
        action = _classify_action(rank, score, top_tier, mid_tier)
        _LOG.debug(
            "rank_portfolio: prospect=%s rank=%d score=%.4f action=%s",
            prospect.prospect_id,
            rank,
            score,
            action,
        )
        result.append((prospect.prospect_id, action))

    return result
