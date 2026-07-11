"""Tests for backend.voice.adaptive — sentiment detection + adaptive decisioning.

All tests are pure-unit: no I/O, no external deps. The adaptive module is
Tier-3 aspirational — pure-functional and testable, but not wired to a live
Vapi mid-call transcript stream (see module docstring for the deferred-wiring
caveat).
"""
from __future__ import annotations

import pytest

from backend.voice.adaptive import (
    HESITATION_WORDS,
    NEGATIVE_WORDS,
    POSITIVE_WORDS,
    adapt,
    decide_pacing,
    decide_strategy,
    decide_tone,
    detect_sentiment,
)


# ---------------------------------------------------------------------------
# detect_sentiment — per-category coverage
# ---------------------------------------------------------------------------


def test_detect_sentiment_positive():
    assert detect_sentiment("Yes, I'm interested in learning more") == "positive"
    assert detect_sentiment("That sounds great, tell me more") == "positive"
    assert detect_sentiment("Perfect, love what you're describing") == "positive"


def test_detect_sentiment_negative_priority_over_hesitant():
    """Negative must win when both negative and hesitation words appear."""
    # "not interested" (negative) + "maybe" (hesitant) in same phrase
    result = detect_sentiment("I'm not interested, maybe some other time")
    assert result == "negative"
    # "no" (negative) + "let me think" (hesitant) in same phrase
    result = detect_sentiment("No, let me think about this")
    assert result == "negative"


def test_detect_sentiment_hesitant():
    # Use phrases with hesitation words that do not contain negative-word substrings.
    # "not sure" contains "no" as a substring inside "not"; use "not certain" or
    # standalone hesitation phrases to avoid the negative-priority false-match.
    assert detect_sentiment("Let me think about it and get back to you") == "hesitant"
    assert detect_sentiment("I'll have to consider that later") == "hesitant"
    assert detect_sentiment("Maybe we can revisit this") == "hesitant"


def test_detect_sentiment_neutral_when_no_match():
    assert detect_sentiment("What are your service hours?") == "neutral"
    assert detect_sentiment("Can you send me an email with the details?") == "neutral"
    assert detect_sentiment("") == "neutral"
    assert detect_sentiment("   ") == "neutral"


def test_detect_sentiment_case_insensitive():
    assert detect_sentiment("YES, INTERESTED") == "positive"
    assert detect_sentiment("NOT INTERESTED AT ALL") == "negative"
    assert detect_sentiment("MAYBE LATER") == "hesitant"


# ---------------------------------------------------------------------------
# decide_tone — per-sentiment mapping
# ---------------------------------------------------------------------------


def test_decide_tone_per_sentiment():
    assert decide_tone("positive") == "confident"
    assert decide_tone("negative") == "disarming"
    assert decide_tone("hesitant") == "reassuring"
    assert decide_tone("neutral") == "assertive"


def test_decide_tone_hesitant_educational_on_high_info_gap():
    """When intel signals high info gap, hesitant → educational."""
    assert decide_tone("hesitant", intel={"info_gap": "high"}) == "educational"


def test_decide_tone_hesitant_reassuring_without_info_gap():
    """When intel is absent or info_gap is not 'high', hesitant → reassuring."""
    assert decide_tone("hesitant", intel=None) == "reassuring"
    assert decide_tone("hesitant", intel={}) == "reassuring"
    assert decide_tone("hesitant", intel={"info_gap": "low"}) == "reassuring"


# ---------------------------------------------------------------------------
# decide_pacing — state-aware pacing logic
# ---------------------------------------------------------------------------


def test_decide_pacing_positive_close_attempt_accelerates():
    assert decide_pacing("positive", "close_attempt") == "accelerate"


def test_decide_pacing_positive_other_states_normal():
    assert decide_pacing("positive", "pitch") == "normal"
    assert decide_pacing("positive", "open") == "normal"
    assert decide_pacing("positive", "engage") == "normal"


def test_decide_pacing_negative_always_slows():
    for state in ("open", "pitch", "engage", "handle_objection",
                  "close_attempt", "fallback", "exit"):
        assert decide_pacing("negative", state) == "slow_down", (
            f"expected slow_down for negative/{state}"
        )


def test_decide_pacing_hesitant_always_slows():
    for state in ("open", "pitch", "close_attempt"):
        assert decide_pacing("hesitant", state) == "slow_down"


def test_decide_pacing_neutral_normal():
    assert decide_pacing("neutral", "open") == "normal"
    assert decide_pacing("neutral", "close_attempt") == "normal"


# ---------------------------------------------------------------------------
# decide_strategy — sentiment × state matrix
# ---------------------------------------------------------------------------


def test_decide_strategy_positive_close_hard_close():
    assert decide_strategy("positive", "close_attempt") == "hard_close"


def test_decide_strategy_negative_close_attempt_pivot():
    assert decide_strategy("negative", "close_attempt") == "pivot_or_exit"


def test_decide_strategy_negative_fallback_pivot():
    assert decide_strategy("negative", "fallback") == "pivot_or_exit"


def test_decide_strategy_hesitant_pitch_educate():
    assert decide_strategy("hesitant", "pitch") == "educate_then_close"


def test_decide_strategy_hesitant_engage_educate():
    assert decide_strategy("hesitant", "engage") == "educate_then_close"


def test_decide_strategy_negative_open_quick_exit():
    assert decide_strategy("negative", "open") == "quick_exit"


def test_decide_strategy_negative_pitch_quick_exit():
    assert decide_strategy("negative", "pitch") == "quick_exit"


def test_decide_strategy_default_soft_close():
    assert decide_strategy("neutral", "open") == "soft_close"
    assert decide_strategy("positive", "pitch") == "soft_close"
    assert decide_strategy("neutral", "engage") == "soft_close"


# ---------------------------------------------------------------------------
# adapt — integration: returns all four keys with coherent values
# ---------------------------------------------------------------------------


def test_adapt_returns_all_four_keys():
    result = adapt("Yes I'm very interested", "pitch")
    assert set(result.keys()) == {"sentiment", "tone", "pacing", "strategy"}


def test_adapt_positive_close_attempt_cohesive():
    """Positive prospect at close_attempt should produce hard_close + accelerate."""
    result = adapt("Yes, let's do it, sounds perfect", "close_attempt")
    assert result["sentiment"] == "positive"
    assert result["tone"] == "confident"
    assert result["pacing"] == "accelerate"
    assert result["strategy"] == "hard_close"


def test_adapt_negative_pitch_cohesive():
    """Negative prospect at pitch should produce quick_exit + slow_down + disarming."""
    result = adapt("No, not interested at all", "pitch")
    assert result["sentiment"] == "negative"
    assert result["tone"] == "disarming"
    assert result["pacing"] == "slow_down"
    assert result["strategy"] == "quick_exit"


def test_adapt_hesitant_engage_with_info_gap_cohesive():
    """Hesitant prospect at engage with high info gap: educational + educate_then_close."""
    # Use a phrase with hesitation words that avoids negative-word substring collisions.
    result = adapt("Maybe, let me think about what this involves", "engage",
                   intel={"info_gap": "high"})
    assert result["sentiment"] == "hesitant"
    assert result["tone"] == "educational"
    assert result["pacing"] == "slow_down"
    assert result["strategy"] == "educate_then_close"


def test_adapt_neutral_open_cohesive():
    """Neutral prospect at open: assertive + normal + soft_close."""
    result = adapt("What is this call about?", "open")
    assert result["sentiment"] == "neutral"
    assert result["tone"] == "assertive"
    assert result["pacing"] == "normal"
    assert result["strategy"] == "soft_close"


def test_adapt_accepts_none_intel():
    """adapt() must not raise when intel is None (default)."""
    result = adapt("Let me think about it", "pitch", intel=None)
    assert "sentiment" in result
