"""Deal probability scoring + tier classification for the prospecting workcell.

Ported verbatim from recovery/deal_scoring_agent.py with stack-fit adjustments:
  - module docstring matches Samus style
  - ``from __future__ import annotations`` header
  - ``__all__`` for public surface
  - module-level weight dicts and all functions preserved unchanged

Relationship:
  - Consumes ``intel`` produced by intelligence.analyze_business /
    intelligence.score_opportunity (keys: ``opportunity_scores``, ``signals``).
  - Called by the prospecting /work handler for the ``score_deal`` action.

Tiers (score thresholds):
  hot    >= 0.75
  warm   >= 0.55
  nurture >= 0.35
  cold   <  0.35
"""

from __future__ import annotations

from typing import Any, Dict, Optional

__all__ = [
    "clamp",
    "compute_base_score",
    "adjust_for_signals",
    "adjust_for_engagement",
    "classify_deal",
    "score_deal",
    "BASE_WEIGHTS",
    "SIGNAL_WEIGHTS",
    "ENGAGEMENT_WEIGHTS",
]


BASE_WEIGHTS = {
    "website": 0.25,
    "seo": 0.20,
    "ads": 0.15,
    "automation": 0.20,
    "reputation": 0.20,
}

SIGNAL_WEIGHTS = {
    "has_website": -0.10,
    "has_cta": -0.15,
    "has_booking": -0.10,
    "review_count": -0.10,
    "rating": -0.10,
    "ads_detected": -0.05,
}

ENGAGEMENT_WEIGHTS = {
    "positive_response": 0.25,
    "neutral_response": 0.10,
    "objection": -0.20,
    "repeat_objection": -0.35,
    "asked_questions": 0.20,
}


def clamp(val: float) -> float:
    return max(0.0, min(1.0, val))


def compute_base_score(opportunity: Dict[str, float]) -> float:
    score = 0.0
    for k, v in opportunity.items():
        score += (v / 100.0) * BASE_WEIGHTS.get(k, 0)
    return min(score, 1.0)


def adjust_for_signals(score: float, signals: Dict[str, Any]) -> float:
    adj = score
    if signals.get("has_website"):
        adj += SIGNAL_WEIGHTS["has_website"]
    if signals.get("has_cta"):
        adj += SIGNAL_WEIGHTS["has_cta"]
    if signals.get("has_booking"):
        adj += SIGNAL_WEIGHTS["has_booking"]
    if signals.get("review_count", 0) > 20:
        adj += SIGNAL_WEIGHTS["review_count"]
    if signals.get("rating", 5) >= 4.5:
        adj += SIGNAL_WEIGHTS["rating"]
    if signals.get("ads_detected"):
        adj += SIGNAL_WEIGHTS["ads_detected"]
    return clamp(adj)


def adjust_for_engagement(score: float, engagement: Dict[str, Any]) -> float:
    adj = score
    if engagement.get("positive"):
        adj += ENGAGEMENT_WEIGHTS["positive_response"]
    if engagement.get("neutral"):
        adj += ENGAGEMENT_WEIGHTS["neutral_response"]
    if engagement.get("objection"):
        adj += ENGAGEMENT_WEIGHTS["objection"]
    if engagement.get("repeat_objection"):
        adj += ENGAGEMENT_WEIGHTS["repeat_objection"]
    if engagement.get("questions"):
        adj += ENGAGEMENT_WEIGHTS["asked_questions"]
    return clamp(adj)


def classify_deal(score: float) -> str:
    if score >= 0.75:
        return "hot"
    if score >= 0.55:
        return "warm"
    if score >= 0.35:
        return "nurture"
    return "cold"


def score_deal(
    intel: Dict[str, Any], engagement: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    opportunity = intel.get("opportunity_scores", {})
    signals = intel.get("signals", {})
    base = compute_base_score(opportunity)
    base = adjust_for_signals(base, signals)
    if engagement:
        base = adjust_for_engagement(base, engagement)
    return {
        "probability": round(base, 3),
        "tier": classify_deal(base),
        "priority_score": int(base * 100),
    }
