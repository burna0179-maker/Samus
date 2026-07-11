"""Deterministic inbound-reply intent classifier (the "Interpreter" pod).

Pure, zero-LLM, deterministic — matching the house doctrine of the autonomous
workcells (signal_filter / path_optimizer / entropy are all deterministic). It
turns a parsed inbound email into one of five intents the reply pipeline can act
on. An optional LLM pass can be layered later behind a flag; the deterministic
classifier is the always-available floor and the test surface.

Priority order is deliberate and legal-first: ``opt_out`` is checked before
everything else, because honoring an unsubscribe is a hard CAN-SPAM obligation
that must win even when a message also sounds interested ("unsubscribe me, and
no I'm not interested"). After that: ``meeting_booked`` > ``interested`` >
``not_interested`` > ``unknown``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Intent labels.
INTENT_OPT_OUT = "opt_out"
INTENT_MEETING_BOOKED = "meeting_booked"
INTENT_INTERESTED = "interested"
INTENT_NOT_INTERESTED = "not_interested"
INTENT_UNKNOWN = "unknown"

# Pattern banks, checked in priority order. Each is a (label, [regex]) tuple.
_OPT_OUT = [
    r"\bunsubscribe\b", r"\bopt[\s\-]?out\b", r"\bremove me\b",
    r"\btake me off\b", r"\bstop (?:emailing|contacting|messaging)\b",
    r"\bdo not (?:contact|email|message)\b", r"\bno longer wish\b",
    r"\bplease stop\b",
]
_MEETING = [
    r"\b(?:schedule|set up|book|arrange) (?:a )?(?:call|meeting|time|demo)\b",
    r"\blet'?s (?:meet|talk|chat|connect|hop on)\b",
    r"\b(?:my )?availabilit", r"\bwhat time(?:s)? work", r"\bcalendar\b",
    r"\bhop on a (?:call|quick call)\b", r"\bbook a (?:slot|time)\b",
    r"\bsend (?:me )?(?:a )?(?:calendar|invite|link)\b",
]
_INTERESTED = [
    r"\binterested\b", r"\btell me more\b", r"\bmore info(?:rmation)?\b",
    r"\blearn more\b", r"\b(?:pricing|price|quote|cost|how much)\b",
    r"\bsounds (?:good|great|interesting)\b", r"\byes,? (?:please|i'?m in)\b",
    r"\bsend (?:me )?(?:the )?(?:details|info|deck|proposal)\b",
    r"\bi'?d like to\b",
]
_NOT_INTERESTED = [
    r"\bnot interested\b", r"\bno,? thank", r"\bno thanks\b",
    r"\bwe'?re (?:all set|good|covered)\b", r"\bnot (?:a fit|for us|right now)\b",
    r"\bpass\b", r"\balready have\b", r"\bno need\b",
]

# Order matters. not_interested is checked BEFORE interested because the bare
# "interested" pattern also matches "not interested" — the more-specific
# negative must win. opt_out is first of all (legal-first).
_BANKS: list[tuple[str, list[str]]] = [
    (INTENT_OPT_OUT, _OPT_OUT),
    (INTENT_MEETING_BOOKED, _MEETING),
    (INTENT_NOT_INTERESTED, _NOT_INTERESTED),
    (INTENT_INTERESTED, _INTERESTED),
]

_COMPILED: list[tuple[str, list[re.Pattern]]] = [
    (label, [re.compile(p, re.IGNORECASE) for p in pats]) for label, pats in _BANKS
]


@dataclass(frozen=True)
class ReplyIntent:
    intent: str
    confidence: float
    signals: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {"intent": self.intent, "confidence": round(self.confidence, 3),
                "signals": list(self.signals)}


def classify_reply(subject: str, body: str) -> ReplyIntent:
    """Classify an inbound reply. Subject + body are scanned together.

    Returns the highest-priority intent whose patterns match, with a confidence
    that scales with how many distinct patterns hit (capped at 1.0). No match
    anywhere -> ``unknown`` at confidence 0.0.
    """
    blob = f"{subject or ''}\n{body or ''}"
    for label, patterns in _COMPILED:
        hits = [p.pattern for p in patterns if p.search(blob)]
        if hits:
            # 1 hit -> 0.6, 2 -> 0.8, 3+ -> ~1.0. opt_out is high-stakes so a
            # single clear hit already reads confident.
            confidence = min(1.0, 0.4 + 0.2 * len(hits))
            if label == INTENT_OPT_OUT:
                confidence = min(1.0, confidence + 0.2)
            return ReplyIntent(intent=label, confidence=confidence, signals=tuple(hits))
    return ReplyIntent(intent=INTENT_UNKNOWN, confidence=0.0)


__all__ = [
    "ReplyIntent",
    "classify_reply",
    "INTENT_OPT_OUT",
    "INTENT_MEETING_BOOKED",
    "INTENT_INTERESTED",
    "INTENT_NOT_INTERESTED",
    "INTENT_UNKNOWN",
]
