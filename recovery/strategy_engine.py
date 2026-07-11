#!/usr/bin/env python3
"""
StrategyEngine — central decision layer above fulfillment
Source: ChatGPT recovery chat 02 (strategy engine section)

Canonical relationship:
- [NEW] decision layer; no direct canonical equivalent (sits between agents + autonomy)
- [EXPANDS §6 autonomy plane] verdict-style decisioning (PROCEED/HOLD/ABORT analog)
- [EXPANDS §6 orchestration] dispatches across services based on decision

Trigger points:
  - after fulfillment step completion
  - after call outcome logged
  - scheduled sweep (every 5-15 min) over active prospects
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


@dataclass
class StrategyContext:
    prospect_id: str
    lead_score: float = 0.0
    seo_score: float = 100.0
    stage: str = "new"
    engagement: str = "low"           # low | medium | high
    last_activity: Optional[str] = None
    conversion_signals: List[str] = None

    def __post_init__(self):
        if self.conversion_signals is None:
            self.conversion_signals = []


def build_context(prospect_id: str, crm: Any) -> StrategyContext:
    p = crm.get_prospect(prospect_id)
    return StrategyContext(
        prospect_id=prospect_id,
        lead_score=p.get("lead_score", 0),
        seo_score=p.get("seo_score", 100),
        stage=p.get("status", "new"),
        engagement=p.get("engagement_level", "low"),
        last_activity=p.get("last_activity_at"),
        conversion_signals=p.get("signals", []),
    )


class StrategyEngine:
    HIGH_SCORE_THRESHOLD = 85
    LOW_SEO_THRESHOLD = 50

    def evaluate(self, ctx: StrategyContext) -> Dict[str, Any]:
        # Guardrails — closed deals don't get further actions
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
        score = 0.0
        score += ctx.lead_score * 0.4
        score += (100 - ctx.seo_score) * 0.3
        if ctx.engagement == "high":
            score += 20
        if "pricing_request" in ctx.conversion_signals:
            score += 30
        return min(score, 100)


def dispatch_strategy_action(
    decision: Dict[str, Any],
    ctx: StrategyContext,
    dispatcher: Callable[..., None],
) -> None:
    action = decision["action"]
    if action == "replan_fulfillment":
        dispatcher(service="fulfillment", action="resume_plan",
                   payload={"prospect_id": ctx.prospect_id})
    elif action == "trigger_outreach":
        dispatcher(service="outreach", action="send_outreach",
                   payload={"prospect_id": ctx.prospect_id})
    elif action == "escalate_close":
        dispatcher(service="outreach", action="send_close",
                   payload={"prospect_id": ctx.prospect_id})


# ----- Pattern-learning skeleton (boost / penalize) -----
PATTERNS: Dict[str, int] = {}

def boost_pattern(key: str) -> None:
    PATTERNS[key] = PATTERNS.get(key, 1) + 1

def penalize_pattern(key: str) -> None:
    PATTERNS[key] = max(1, PATTERNS.get(key, 1) - 1)


def record_outcome(prospect_id: str, won: bool, store: Any) -> None:
    store.put({"prospect_id": prospect_id, "won": won})
    boost_pattern("similar_prospects") if won else penalize_pattern("strategy_path")
