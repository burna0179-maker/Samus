#!/usr/bin/env python3
"""
AutonomousCloser — multi-turn conversation state machine
Source: ChatGPT recovery chat 03 (autonomous_closer.py section)

Canonical relationship:
- [NEW pod] business/sales pack — conversation flow controller
- [EXPANDS §6 agents.cognitive] 7-state FSM for call flow (parallel to §6 9-stage CognitiveLoop)
- [DEFERRED] persistence; currently stateless per-call

States:  open → pitch → engage → {handle_objection | close_attempt} → {fallback | exit}
"""

from __future__ import annotations

from typing import Any, Dict, Optional

STATES = ["open", "pitch", "engage", "handle_objection", "close_attempt", "fallback", "exit"]


def next_state(current: str, context: Dict[str, Any]) -> str:
    if current == "open":
        return "pitch"
    if current == "pitch":
        return "engage"
    if current == "engage":
        return "handle_objection" if context.get("objection") else "close_attempt"
    if current == "handle_objection":
        return "close_attempt"
    if current == "close_attempt":
        return "fallback" if context.get("resistance") else "exit"
    if current == "fallback":
        return "exit"
    return "exit"


def run_closer_step(
    state: str,
    user_input: str,
    intel: Dict[str, Any],
    objection_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    products = intel["products"]
    primary = products["primary"]
    secondary = products.get("secondary")

    context = {
        "objection": objection_result.get("detected") if objection_result else None,
        "resistance": "not interested" in (user_input or "").lower(),
    }
    next_step = next_state(state, context)

    if state == "open":
        action = "deliver_opener"
    elif state == "pitch":
        action = "deliver_pitch"
    elif state == "engage":
        action = "ask_question"
    elif state == "handle_objection":
        action = objection_result["response"] if objection_result else "acknowledge"
    elif state == "close_attempt":
        action = f"attempt_close_on_{primary}"
    elif state == "fallback":
        action = f"pivot_to_{secondary}" if secondary else "offer_free_audit"
    else:
        action = "exit_clean"

    return {
        "current_state": state,
        "next_state": next_step,
        "action": action,
        "primary_product": primary,
        "secondary_product": secondary,
    }
