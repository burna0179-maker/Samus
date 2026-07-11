"""``HeuristicRegistry`` — META-PERCEPTION pre-bias text (Contract 2).

Maps a classified situation to an optional bias string the
``MetaCognitionEngine`` prepends to the loop's ``system_prompt``. Pure,
deterministic, fail-safe. Returns ``""`` for "no bias" (the wrapper treats
an empty string as "do not modify the prompt").

Contract 2 method:
    ``evaluate_pre(text=, situation=) -> str``   ("" = none)

DORMANCY: constructed no-arg; no live importer; no flag of its own (the
WRAPPER's enable posture governs whether it runs). Bias is advisory PROMPT
text only — it never bypasses any governance gate.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

log = logging.getLogger(__name__)

# Situation-type -> advisory bias prompt. Conservative by construction:
# higher-risk situations bias TOWARD caution / governance deference, never
# toward action. Empty type / unknown -> no bias.
_BIAS_BY_TYPE: Dict[str, str] = {
    "high_risk": (
        "This situation is high-risk. Defer to the operator and every governance "
        "gate; propose rather than act; do not take any irreversible or financial "
        "action autonomously."
    ),
    "command": (
        "This looks like an action request. Confirm scope and stay within mandate; "
        "surface any side-effecting step for approval before proceeding."
    ),
    "question": (
        "This looks informational. Answer precisely and ground claims in available "
        "evidence; do not fabricate."
    ),
}


class HeuristicRegistry:
    """Situation -> advisory pre-bias text. Pure, deterministic, fail-safe."""

    def __init__(self) -> None:  # no-arg construct (Contract 2)
        pass

    def evaluate_pre(self, *, text: str = "", situation: Dict[str, Any] | None = None) -> str:
        """Return an advisory bias string for ``situation`` (or "" for none).

        Never raises — any error degrades to "" (no bias), so a registry
        failure can never corrupt the system prompt or break META-PERCEPTION.
        """
        try:
            situation = situation or {}
            stype = str(situation.get("type", "") or "")
            return _BIAS_BY_TYPE.get(stype, "")
        except Exception:  # pragma: no cover — must never break META-PERCEPTION
            log.exception("HeuristicRegistry.evaluate_pre failed; returning no bias")
            return ""


__all__ = ["HeuristicRegistry"]
