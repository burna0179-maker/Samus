"""Real-time adaptive decisioning for live conversation. Pure-functional — takes
a transcript snippet + intel + current closer state, returns (tone, pacing,
strategy) recommendations.

TIER-3 / WIRING NOTE: this module is buildable + testable as a pure function,
but the LIVE call site (Vapi mid-call transcript stream → adaptive.decide →
outreach.closer next-state) DOES NOT EXIST yet. Current voice workcell handles
outbound calls + post-call webhook receipt only. Mid-call transcript streaming
requires Vapi's WebSocket/SSE transcript subscription, which is unbuilt. Treat
adaptive as ready-for-wiring; do not assume it's running in production.

Ported from: Samus/recovery/realtime_adaptive_agent.py (2026-05-17)
Sibling modules: backend/outreach/objection.py, backend/outreach/closer.py
"""

from __future__ import annotations

from typing import Final

__all__: list[str] = [
    "POSITIVE_WORDS",
    "NEGATIVE_WORDS",
    "HESITATION_WORDS",
    "detect_sentiment",
    "decide_tone",
    "decide_pacing",
    "decide_strategy",
    "adapt",
]

# ---------------------------------------------------------------------------
# Sentiment word lists
# ---------------------------------------------------------------------------

POSITIVE_WORDS: Final[list[str]] = [
    "interested",
    "yes",
    "tell me more",
    "great",
    "love",
    "perfect",
    "yeah",
    "sure",
    "okay",
    "absolutely",
    "sounds good",
]

NEGATIVE_WORDS: Final[list[str]] = [
    "not interested",
    "no",
    "stop",
    "expensive",
    "can't afford",
    "no thanks",
    "busy",
    "not for us",
    "don't need",
]

HESITATION_WORDS: Final[list[str]] = [
    "maybe",
    "let me think",
    "not sure",
    "i'll",
    "later",
    "think about it",
    "not certain",
]

# ---------------------------------------------------------------------------
# Pure-functional decision layer
# ---------------------------------------------------------------------------


def detect_sentiment(transcript_text: str) -> str:
    """Return the dominant sentiment from a transcript snippet.

    Priority order: negative → hesitant → positive → neutral.
    Matching is case-insensitive substring scan — same approach as
    objection.py's detect_objection().

    Parameters
    ----------
    transcript_text:
        Raw conversation snippet. May be multi-sentence.

    Returns
    -------
    str
        One of: ``"negative"`` | ``"hesitant"`` | ``"positive"`` | ``"neutral"``
    """
    lowered = (transcript_text or "").lower()
    if any(w in lowered for w in NEGATIVE_WORDS):
        return "negative"
    if any(w in lowered for w in HESITATION_WORDS):
        return "hesitant"
    if any(w in lowered for w in POSITIVE_WORDS):
        return "positive"
    return "neutral"


def decide_tone(sentiment: str, intel: dict | None = None) -> str:
    """Map sentiment (+ optional intel) to a delivery tone.

    Tone values correspond to Morgan SDR script variants:
      - ``"confident"``   — press forward; prospect is engaged
      - ``"disarming"``   — defuse resistance; acknowledge before continuing
      - ``"reassuring"``  — slow reassurance; reduce friction without backing off
      - ``"educational"`` — info-gap bridging; prospect needs context before yes
      - ``"assertive"``   — neutral, on-script; no strong signal either way

    The ``"hesitant"`` → ``"educational"`` branch fires when intel signals a
    high information gap (``intel["info_gap"] == "high"``), otherwise defaults
    to ``"reassuring"``.

    Parameters
    ----------
    sentiment:
        Output of :func:`detect_sentiment`.
    intel:
        Optional intelligence dict. Checked for ``"info_gap"`` key.

    Returns
    -------
    str
        One of the five tone strings above.
    """
    if sentiment == "positive":
        return "confident"
    if sentiment == "negative":
        return "disarming"
    if sentiment == "hesitant":
        if intel and intel.get("info_gap") == "high":
            return "educational"
        return "reassuring"
    # neutral
    return "assertive"


def decide_pacing(sentiment: str, current_state: str) -> str:
    """Map sentiment + FSM state to a pacing directive.

    Pacing values:
      - ``"accelerate"`` — shorten pauses, move toward close
      - ``"normal"``     — default cadence
      - ``"slow_down"``  — extend pauses, give prospect space

    Parameters
    ----------
    sentiment:
        Output of :func:`detect_sentiment`.
    current_state:
        Current closer FSM state — one of the 7 states in outreach.closer.STATES.

    Returns
    -------
    str
        One of: ``"accelerate"`` | ``"normal"`` | ``"slow_down"``
    """
    if sentiment == "positive" and current_state == "close_attempt":
        return "accelerate"
    if sentiment == "negative":
        return "slow_down"
    if sentiment == "hesitant":
        return "slow_down"
    return "normal"


def decide_strategy(
    sentiment: str,
    current_state: str,
    intel: dict | None = None,
) -> str:
    """Select a strategy directive based on sentiment + FSM state.

    Strategy values map to script branches the caller (live wiring, when built)
    passes into outreach.closer.run_closer_step():
      - ``"hard_close"``         — direct close language; high-intent path
      - ``"pivot_or_exit"``      — change angle or wind down gracefully
      - ``"educate_then_close"`` — bridge the info gap before asking for yes
      - ``"quick_exit"``         — no traction; exit cleanly without burning trust
      - ``"soft_close"``         — default; gentle commitment ask

    The ``intel`` parameter is accepted for forward-compatibility with deal-scoring
    overrides; not consumed in the current implementation.

    Parameters
    ----------
    sentiment:
        Output of :func:`detect_sentiment`.
    current_state:
        Current closer FSM state.
    intel:
        Optional intelligence dict (reserved; not read in current impl).

    Returns
    -------
    str
        One of the five strategy strings above.
    """
    if sentiment == "positive" and current_state == "close_attempt":
        return "hard_close"
    if sentiment == "negative" and current_state in ("close_attempt", "fallback"):
        return "pivot_or_exit"
    if sentiment == "hesitant" and current_state in ("pitch", "engage"):
        return "educate_then_close"
    if sentiment == "negative" and current_state in ("open", "pitch"):
        return "quick_exit"
    return "soft_close"


def adapt(
    transcript_text: str,
    current_state: str,
    intel: dict | None = None,
) -> dict:
    """Compute full adaptive recommendation for one transcript turn.

    This is the primary entry point. Composes the four sub-functions and
    returns a single dict suitable for serialisation or direct consumption
    by the closer FSM (once live wiring exists).

    WIRING NOTE: the intended call site is:
        Vapi mid-call transcript event
        → ``adapt(transcript_text, closer_state, intel)``
        → feed ``result["strategy"]`` into ``outreach.closer.run_closer_step()``
        → feed ``result["tone"]`` into ``outreach.script`` response rendering

    That call site does not exist yet — see module docstring.

    Parameters
    ----------
    transcript_text:
        Raw transcript snippet from the current conversation turn.
    current_state:
        Current closer FSM state — one of the 7 states in outreach.closer.STATES.
    intel:
        Optional intelligence dict from deal-scoring or upstream modules.
        Passed through to :func:`decide_tone` (``"info_gap"`` key) and
        :func:`decide_strategy` (reserved).

    Returns
    -------
    dict with keys:
        ``"sentiment"``  — str: ``"positive"`` | ``"negative"`` | ``"hesitant"`` | ``"neutral"``
        ``"tone"``       — str: ``"confident"`` | ``"disarming"`` | ``"reassuring"`` | ``"educational"`` | ``"assertive"``
        ``"pacing"``     — str: ``"accelerate"`` | ``"normal"`` | ``"slow_down"``
        ``"strategy"``   — str: ``"hard_close"`` | ``"pivot_or_exit"`` | ``"educate_then_close"`` | ``"quick_exit"`` | ``"soft_close"``
    """
    sentiment = detect_sentiment(transcript_text)
    return {
        "sentiment": sentiment,
        "tone": decide_tone(sentiment, intel),
        "pacing": decide_pacing(sentiment, current_state),
        "strategy": decide_strategy(sentiment, current_state, intel),
    }
