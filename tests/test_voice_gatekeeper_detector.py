"""Gatekeeper reclassifier (Gap-19) — detect a live human who answered after a
machine/hold greeting so the call is reclassified from voicemail -> gatekeeper."""
from __future__ import annotations

import backend.voice.gatekeeper_detector as gk


# The real Plumbing Doctor transcript (call 019f1ade): machine hold greeting,
# Morgan pitches into the void, then Lily (a live human) picks up.
_PLUMBING_DOCTOR = (
    "User: Thank you for calling Plumbing Doctor. Your call is very important "
    "to us. Please stay on the line and someone will answer your call in just a "
    "moment.\n"
    "AI: Oh, hey. It's Morgan calling from Hustle Forge. I know I'm an "
    "interruption here, but do you mind if I grab maybe thirty seconds?\n"
    "AI: Still there?\n"
    "User: Thank you for calling Plumbing Doctor. This is Lily. Can I help you?\n"
    "AI: Hi. This is Morgan from HustleForge. I was checking Google rankings "
    "for plumber in nine five nine nine one.\n"
)

_PURE_VOICEMAIL = (
    "User: You have reached the voicemail of Dr. Smith's office. We are unable "
    "to take your call right now. Please leave a message after the beep.\n"
    "AI: Hi, this is Morgan from HustleForge calling about your Google "
    "rankings. Give us a call back at five three zero. Thanks.\n"
)

_AMBIGUOUS_SHORT = (
    "User: Hello?\n"
    "AI: Hi, is this the front desk?\n"
)


def test_machine_greeting_then_live_human_is_gatekeeper():
    is_human, reason = gk.detect_human_engagement(
        _PLUMBING_DOCTOR, ended_reason="voicemail",
    )
    assert is_human is True
    assert "Lily" in reason or "live human" in reason.lower()


def test_pure_voicemail_greeting_only_is_not_gatekeeper():
    is_human, reason = gk.detect_human_engagement(
        _PURE_VOICEMAIL, ended_reason="voicemail",
    )
    assert is_human is False


def test_ambiguous_short_is_conservative_false():
    is_human, _ = gk.detect_human_engagement(
        _AMBIGUOUS_SHORT, ended_reason="no-answer",
    )
    assert is_human is False


def test_non_machine_ended_reason_is_not_eligible():
    # Even a clear live-human transcript is not reclassified when the call was
    # already a real conversation (assistant-ended-call).
    is_human, reason = gk.detect_human_engagement(
        _PLUMBING_DOCTOR, ended_reason="assistant-ended-call",
    )
    assert is_human is False
    assert reason == "not a machine-classified call"


def test_blank_transcript_returns_false_empty_reason():
    assert gk.detect_human_engagement("", ended_reason="voicemail") == (False, "")
    assert gk.detect_human_engagement("   ", ended_reason="voicemail") == (False, "")


def test_no_ended_reason_defaults_to_not_eligible():
    is_human, reason = gk.detect_human_engagement(_PLUMBING_DOCTOR)
    assert is_human is False
    assert reason == "not a machine-classified call"


def test_name_speaking_variant_detected():
    transcript = (
        "User: Thank you for calling. Please stay on the line.\n"
        "AI: Hey, it's Morgan.\n"
        "User: Front desk, Maria speaking, how can I help you?\n"
    )
    is_human, reason = gk.detect_human_engagement(transcript, ended_reason="voicemail")
    assert is_human is True


def test_ivr_loop_without_human_is_false():
    # Two machine greetings in a row (IVR loop), no human — must stay voicemail.
    transcript = (
        "User: Please stay on the line and someone will answer shortly.\n"
        "AI: Hey, it's Morgan.\n"
        "User: Your call is very important to us. Please continue to hold.\n"
    )
    is_human, _ = gk.detect_human_engagement(transcript, ended_reason="voicemail")
    assert is_human is False
