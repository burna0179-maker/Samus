"""``SituationIndex`` — META-PERCEPTION situation classifier (Contract 2).

Deterministic, zero-dependency, fail-safe classification of an incoming
stimulus into a coarse ``{risk, type}`` descriptor the
``MetaCognitionEngine`` keys reinforcement + bias on. Pure heuristics
(keyword + channel) so it is $0, offline, and never raises.

Contract 2 method:
    ``classify(text=, channel=, plan_token=) -> dict{risk, type}``

DORMANCY: constructed no-arg; has no live importer; carries no flag of its
own (it is always-safe pure compute — the WRAPPER's enable posture governs
whether it runs at all). When the wrapper is dormant this class is never
constructed.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

log = logging.getLogger(__name__)

# Situation TYPE keywords. First match wins (order matters: high-risk
# financial/irreversible patterns are checked before generic question/command).
_HIGH_RISK_TERMS = (
    "wire", "transfer", "payment", "bank", "refund", "credential", "password",
    "ssn", "delete", "purchase", "buy ", "send money", "publish", "deploy",
)
_COMMAND_TERMS = ("do ", "run ", "execute", "create", "make ", "build", "send", "schedule")
_QUESTION_TERMS = ("?", "what", "why", "how", "when", "who", "where", "which")


class SituationIndex:
    """Coarse situation classifier — pure, deterministic, fail-safe."""

    def __init__(self) -> None:  # no-arg construct (Contract 2)
        pass

    def classify(self, *, text: str = "", channel: str = "", plan_token: str = "") -> Dict[str, Any]:
        """Classify a stimulus into ``{risk, type}``.

        ``risk`` ∈ {"low","medium","high"}; ``type`` ∈ {"command","question",
        "high_risk","generic"}. Never raises — any error degrades to the
        neutral ``{"risk": "unknown", "type": "generic"}`` the wrapper expects.
        """
        try:
            lowered = (text or "").lower()
            if any(term in lowered for term in _HIGH_RISK_TERMS):
                return {"risk": "high", "type": "high_risk"}
            if lowered.strip().endswith("?") or any(
                lowered.startswith(t) or f" {t}" in lowered for t in _QUESTION_TERMS
            ):
                return {"risk": "low", "type": "question"}
            if any(lowered.startswith(t) or f" {t}" in lowered for t in _COMMAND_TERMS):
                return {"risk": "medium", "type": "command"}
            return {"risk": "low", "type": "generic"}
        except Exception:  # pragma: no cover — must never break META-PERCEPTION
            log.exception("SituationIndex.classify failed; returning neutral")
            return {"risk": "unknown", "type": "generic"}


__all__ = ["SituationIndex"]
