"""7-state conversation FSM for outbound sales calls. Stateless per-turn decision
function — given current state + user input + intel + optional objection result,
returns next state + action recommendation. Consumes objection.handle_objection()
output. Caller tracks state across turns."""
from __future__ import annotations

STATES: list[str] = [
    "open",
    "pitch",
    "engage",
    "handle_objection",
    "close_attempt",
    "fallback",
    "exit",
]

INITIAL_STATE: str = "open"
TERMINAL_STATE: str = "exit"


def next_state(current: str, context: dict) -> str:
    """Pure transition function — maps (current state, context) -> next state.

    context keys consumed:
      "objection"  — truthy if an objection was detected in this turn
      "resistance" — truthy if the prospect expressed disinterest
    """
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
    intel: dict,
    objection_result: dict | None = None,
) -> dict:
    """Stateless per-turn decision function.

    Parameters
    ----------
    state:
        Current FSM state — one of STATES.
    user_input:
        Raw text from the prospect for this turn (may be empty string).
    intel:
        Intelligence dict. Must contain ``intel["products"]`` with at least
        ``"primary"`` (str). ``"secondary"`` (str | None) is optional.
        Matches the output of ``prospecting.intelligence.map_products()``.
    objection_result:
        Optional output from ``objection.handle_objection()``. When present,
        expected keys: ``"detected"`` (bool) and ``"response"`` (str).

    Returns
    -------
    dict with keys:
        current_state, next_state, action, primary_product, secondary_product
    """
    products = intel["products"]
    primary: str = products["primary"]
    secondary: str | None = products.get("secondary")

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


__all__ = [
    "STATES",
    "INITIAL_STATE",
    "TERMINAL_STATE",
    "next_state",
    "run_closer_step",
]
