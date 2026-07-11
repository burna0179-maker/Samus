"""StrategyEngine — pure decision logic.

Ported from recovery/strategy_engine.py.  No I/O; all methods are
synchronous and side-effect-free (except the module-level pattern
counters, which are intentionally mutable state for pattern learning).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "StrategyContext",
    "StrategyEngine",
    "PATTERNS",
    "boost_pattern",
    "penalize_pattern",
    "reset_patterns",
]


@dataclass(frozen=False)
class StrategyContext:
    """Snapshot of a prospect's current state for strategy evaluation."""

    prospect_id: str
    lead_score: float = 0.0
    seo_score: float = 100.0
    stage: str = "new"
    engagement: str = "low"  # low | medium | high
    last_activity: str | None = None
    conversion_signals: list[str] = field(default_factory=list)


class StrategyEngine:
    """Scores an opportunity and selects a dispatch action."""

    HIGH_SCORE_THRESHOLD = 85
    LOW_SEO_THRESHOLD = 50

    def evaluate(self, ctx: StrategyContext) -> dict[str, Any]:
        """Return a decision dict with ``action`` and optional ``score``/``reason``."""
        # Guardrails — closed deals receive no further actions
        if ctx.stage in ("closed_won", "closed_lost"):
            return {"action": "none", "reason": "deal_closed"}

        score = self._score_opportunity(ctx)

        if score >= self.HIGH_SCORE_THRESHOLD:
            return {"action": "escalate_close", "score": score}
        if ctx.seo_score < self.LOW_SEO_THRESHOLD:
            return {"action": "replan_fulfillment", "score": score}
        if ctx.engagement == "low":
            return {"action": "trigger_outreach", "score": score}
        return {"action": "monitor", "score": score}

    def _score_opportunity(self, ctx: StrategyContext) -> float:
        """Compute a 0-100 opportunity score from context signals."""
        score = 0.0
        score += ctx.lead_score * 0.4
        score += (100 - ctx.seo_score) * 0.3
        if ctx.engagement == "high":
            score += 20
        if "pricing_request" in ctx.conversion_signals:
            score += 30
        return min(score, 100)


# ----- Pattern-learning skeleton (boost / penalize) -----

PATTERNS: dict[str, int] = {}


def boost_pattern(key: str) -> None:
    """Increment the win-weight for a pattern key."""
    PATTERNS[key] = PATTERNS.get(key, 1) + 1


def penalize_pattern(key: str) -> None:
    """Decrement the win-weight for a pattern key, floored at 1."""
    PATTERNS[key] = max(1, PATTERNS.get(key, 1) - 1)


def reset_patterns() -> None:
    """Clear all pattern counters — for test isolation only."""
    PATTERNS.clear()
