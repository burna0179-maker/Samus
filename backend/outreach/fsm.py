"""7-state FSM porting ``recovery/autonomous_closer.py``.

States: open -> pitch -> engage -> {handle_objection | close_attempt}
        -> {fallback | exit}

Pure functions — no module-level state, no side effects.
"""
from __future__ import annotations

from typing import Any

from .models import OutreachIntel


STATES: tuple[str, ...] = (
    "open", "pitch", "engage", "handle_objection",
    "close_attempt", "fallback", "exit",
)


def next_state(current: str, context: dict[str, Any]) -> str:
    """Return the next FSM state given the current state + a small context dict."""
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


def decide_action(
    state: str,
    intel: OutreachIntel,
    objection_response: str | None = None,
) -> str:
    """Map the current state + intel to the action the caller should perform."""
    products = intel.products or {}
    primary = products.get("primary", "")
    secondary = products.get("secondary")

    if state == "open":
        return "deliver_opener"
    if state == "pitch":
        return "deliver_pitch"
    if state == "engage":
        return "ask_question"
    if state == "handle_objection":
        return objection_response or "acknowledge"
    if state == "close_attempt":
        return f"attempt_close_on_{primary}" if primary else "attempt_close"
    if state == "fallback":
        return f"pivot_to_{secondary}" if secondary else "offer_free_audit"
    return "exit_clean"
