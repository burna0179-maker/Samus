#!/usr/bin/env python3
"""
RealtimeAdaptiveAgent — live tone/pacing/strategy modulation
Source: ChatGPT recovery chat 05 (realtime_adaptive_agent.py)

Canonical relationship:
- [NEW pod] business/sales pack
- [EXPANDS §6 agents.cognitive] live-call adaptation layer (sits between RETRIEVE→ACT in 9-stage loop)
- Integrates with: autonomous_closer (state) + deal_scoring_agent (tier)

Outputs: sentiment / tone / pacing / strategy / final modulated response.
"""

from __future__ import annotations

from typing import Any, Dict


POSITIVE_WORDS = ["yes", "yeah", "sure", "okay", "interested"]
NEGATIVE_WORDS = ["no", "not interested", "busy", "stop", "no thanks"]
HESITATION_WORDS = ["maybe", "not sure", "later", "think about it"]


def detect_sentiment(text: str) -> str:
    t = (text or "").lower()
    if any(w in t for w in NEGATIVE_WORDS):
        return "negative"
    if any(w in t for w in POSITIVE_WORDS):
        return "positive"
    if any(w in t for w in HESITATION_WORDS):
        return "hesitant"
    return "neutral"


def determine_tone(deal: Dict[str, Any], sentiment: str) -> str:
    tier = deal.get("tier")
    if sentiment == "negative":
        return "disarming"
    if sentiment == "hesitant":
        return "reassuring"
    if tier == "hot":
        return "assertive"
    if tier == "warm":
        return "confident"
    if tier == "nurture":
        return "educational"
    return "light"


def determine_pacing(sentiment: str, objections: int) -> str:
    if sentiment == "negative":
        return "slow"
    if objections > 1:
        return "slow + deliberate"
    if sentiment == "positive":
        return "fast"
    return "moderate"


def determine_strategy(deal: Dict[str, Any], sentiment: str, objections: int) -> str:
    tier = deal.get("tier")
    if tier == "hot" and sentiment == "positive":
        return "hard_close"
    if objections >= 2:
        return "pivot_or_exit"
    if sentiment == "hesitant":
        return "educate_then_close"
    if tier == "cold":
        return "quick_exit"
    return "soft_close"


def modulate_response(base_text: str, tone: str) -> str:
    if tone == "assertive":
        return base_text + " Let's get this set up today."
    if tone == "reassuring":
        return "No pressure — " + base_text
    if tone == "disarming":
        return "Totally understand — " + base_text
    if tone == "educational":
        return base_text + " Most businesses aren't aware of this until it's shown."
    return base_text


def adapt_agent(
    user_input: str,
    deal: Dict[str, Any],
    objection_count: int,
    base_script: Dict[str, str],
) -> Dict[str, Any]:
    sentiment = detect_sentiment(user_input)
    tone = determine_tone(deal, sentiment)
    pacing = determine_pacing(sentiment, objection_count)
    strategy = determine_strategy(deal, sentiment, objection_count)

    if strategy == "hard_close":
        response = base_script.get("close", "")
    elif strategy == "educate_then_close":
        response = base_script.get("pitch", "") + " " + base_script.get("close", "")
    elif strategy == "pivot_or_exit":
        response = "Would it make more sense to show you a simpler option?"
    elif strategy == "quick_exit":
        response = "No problem — I'll send you something you can review later."
    else:
        response = base_script.get("pitch", "")

    return {
        "sentiment": sentiment,
        "tone": tone,
        "pacing": pacing,
        "strategy": strategy,
        "response": modulate_response(response, tone),
    }
