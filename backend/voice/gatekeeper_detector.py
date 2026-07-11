"""Detect a live human (gatekeeper/receptionist) who answered AFTER an IVR
hold-greeting or answering machine — the "gatekeeper reclassifier" (Gap-19).

Why this exists: Vapi's answer-machine-detection (AMD) classifies the WHOLE
call by its opening audio. When a call opens on an IVR hold loop or a machine
greeting, Vapi stamps ``endedReason=voicemail``/``no-answer`` and Samus files
the call as ``voicemail_left``/``no_answer`` — which DROP the prospect from the
operator's call list. But sometimes, after that machine greeting, a LIVE HUMAN
(a receptionist) actually picks up and converses. Example (call 019f1ade,
Plumbing Doctor):

    User: Thank you for calling Plumbing Doctor. Your call is very important to
          us. Please stay on the line ...          <- MACHINE / HOLD greeting
    AI:   Oh, hey. It's Morgan calling from Hustle Forge ...
    AI:   Still there?
    User: Thank you for calling Plumbing Doctor. This is Lily. Can I help you?
          <- LIVE HUMAN gatekeeper answered
    AI:   Hi. This is Morgan ...

That is a real WARM CONTACT, wrongly discarded as a voicemail. This module
reads the transcript and decides whether a human engaged after the machine
greeting, so :mod:`backend.voice.reconcile` can reclassify the outcome to
``gatekeeper`` (which STAYS on the operator list as a warm lead).

Design mirrors :mod:`backend.voice.closure_detector`: rule-based, keyword-
gated, fail-soft (NEVER raises), injectable, and NO LLM on the hot path. In a
Vapi transcript the CALLED PARTY (both the machine AND the human) is labeled
``User:`` and Morgan is ``AI:`` — so a "gatekeeper" is a *later* ``User:`` turn
that reads as a live human, following an *earlier* ``User:`` turn that reads as
a machine/hold greeting.

Conservative by default: when ambiguous, return ``(False, ...)`` and leave the
call as a voicemail. A false "gatekeeper" pollutes the operator list, which is
worse than missing one warm lead.

``detect_human_engagement(transcript, *, ended_reason="") -> (is_human, reason)``
"""
from __future__ import annotations

import logging
import re

_LOG = logging.getLogger("samus.voice.gatekeeper_detector")

# ---------------------------------------------------------------------------
# Keyword lists (module constants — exported for tests / reuse).
# ---------------------------------------------------------------------------

# ended_reason values (lowercased, substring-matched) that mean the AMD verdict
# was machine-ish. ONLY these are eligible for reclassification — a call that
# already completed as a real conversation is never touched.
MACHINE_ENDED_REASONS: tuple[str, ...] = (
    "voicemail",
    "no-answer",
    "no_answer",
    "machine_detected_silence",
    "machine_end_beep",
    "machine_end_silence",
    "machine_end_other",
    "customer-did-not-answer",
    "twilio-failed-to-connect-call",
)

# Phrases that mark a turn as a MACHINE / IVR HOLD greeting or a voicemail
# prompt (lowercased substrings). The presence of one of these in an early
# ``User:`` turn establishes the "machine greeting" the human must come after.
MACHINE_GREETING_HINTS: tuple[str, ...] = (
    "please stay on the line",
    "stay on the line",
    "your call is very important",
    "your call is important",
    "leave a message",
    "leave your message",
    "leave your name and number",
    "after the beep",
    "after the tone",
    "at the tone",
    "record your message",
    "reached the voicemail",
    "reached the voice mail",
    "you have reached",
    "you've reached",
    "the voicemail of",
    "please press",
    "press one",
    "press 1",
    "para espanol",
    "para español",
    "currently closed",
    "our office is closed",
    "we are closed",
    "we're closed",
    "regular business hours",
    "normal business hours",
    "someone will answer",
    "will be with you shortly",
    "next available representative",
    "unavailable to take your call",
    "unable to take your call",
    "cannot take your call",
    "can't take your call",
    "menu options",
    "listen carefully as our menu",
)

# Phrases that mark a turn as a LIVE HUMAN conversational response — a person
# identifying themselves or offering help. The presence of one of these in a
# ``User:`` turn that comes AFTER the machine greeting is the positive signal.
LIVE_HUMAN_HINTS: tuple[str, ...] = (
    "this is ",
    " speaking",
    "can i help you",
    "how can i help",
    "how may i help",
    "may i help you",
    "how can i assist",
    "how may i assist",
    "may i assist",
    "what can i do for you",
    "who's calling",
    "who is calling",
    "who am i speaking",
    "how can i direct",
    "may i ask who",
    "can i ask who",
    "may i tell",
    "what's this regarding",
    "what is this regarding",
    "what is this in reference",
    "regarding",
    "hold on",
    "one moment",
    "let me transfer",
    "let me connect",
    "thanks for calling",  # a receptionist live-answer often re-greets warmly
    "thank you for calling",
)

# A turn matching one of these on TOP of a live-human hint is treated as still
# machine (e.g. "This is the voicemail of ..." must NOT count as a human even
# though it contains "this is "). Guards the weak "this is " signal.
_LIVE_HUMAN_FALSE_FRIENDS: tuple[str, ...] = (
    "voicemail",
    "voice mail",
    "not available",
    "unavailable",
    "leave a message",
    "after the beep",
    "after the tone",
    "recorded",
    "automated",
    "answering service",
)

# "Name identification" pattern: "this is <Name>" or "<Name> speaking" with an
# actual capitalized-ish name token. Used as a strong human signal even when the
# generic help phrases are absent.
_THIS_IS_NAME = re.compile(r"\bthis is ([a-z][a-z'.-]+)\b", re.IGNORECASE)
_NAME_SPEAKING = re.compile(r"\b([a-z][a-z'.-]+) speaking\b", re.IGNORECASE)

# Minimum length for a transcript to be worth analyzing at all.
_MIN_TRANSCRIPT_LEN = 15
# A live-human turn should be at least this many chars to avoid one-word noise.
_MIN_HUMAN_TURN_LEN = 8


def _is_machine_ended_reason(ended_reason: str) -> bool:
    low = (ended_reason or "").lower()
    return any(h in low for h in MACHINE_ENDED_REASONS)


def _parse_turns(transcript: str) -> list[tuple[str, str]]:
    """Parse a Vapi transcript into ``[(speaker, text), ...]`` where speaker is
    ``"user"`` or ``"ai"``. Lines that don't start a new ``User:``/``AI:`` turn
    are appended to the current turn (multi-line turns). Unlabeled leading text
    is ignored. Never raises.
    """
    turns: list[tuple[str, str]] = []
    cur_speaker: str | None = None
    cur_parts: list[str] = []

    def _flush() -> None:
        if cur_speaker is not None and cur_parts:
            turns.append((cur_speaker, " ".join(cur_parts).strip()))

    for raw in (transcript or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^(user|ai|assistant|bot)\s*:\s*(.*)$", line, re.IGNORECASE)
        if m:
            _flush()
            spk = m.group(1).lower()
            cur_speaker = "user" if spk == "user" else "ai"
            cur_parts = [m.group(2).strip()] if m.group(2).strip() else []
        elif cur_speaker is not None:
            cur_parts.append(line)
    _flush()
    return turns


def _looks_machine(text: str) -> bool:
    low = text.lower()
    return any(h in low for h in MACHINE_GREETING_HINTS)


def _human_signal(text: str) -> str | None:
    """Return a short reason if ``text`` reads as a live human, else None.

    Guarded against machine false-friends (a voicemail greeting that happens to
    contain 'this is ') and against too-short one-word turns.
    """
    if len(text.strip()) < _MIN_HUMAN_TURN_LEN:
        return None
    low = text.lower()
    if any(ff in low for ff in _LIVE_HUMAN_FALSE_FRIENDS):
        return None

    # Strong signal: an explicit self-identification with a real name token.
    m = _THIS_IS_NAME.search(low) or _NAME_SPEAKING.search(low)
    if m:
        name = m.group(1)
        # Reject obvious non-names picked up by the loose regex.
        if name not in {"the", "a", "an", "our", "your", "regarding"}:
            return f"live human identified: 'This is {name.title()}'"

    # Offer-of-help / conversational-receptionist phrases.
    for h in LIVE_HUMAN_HINTS:
        if h.strip() and h in low:
            snippet = text.strip()
            if len(snippet) > 80:
                snippet = snippet[:77] + "..."
            return f"live human answered: '{snippet}'"
    return None


def detect_human_engagement(
    transcript: str,
    *,
    ended_reason: str = "",
) -> tuple[bool, str]:
    """Decide whether a live human engaged AFTER a machine/hold greeting.

    Returns ``(is_human_contact, reason)``. Rule-based, conservative, and
    fail-soft — never raises; on any internal error returns ``(False, "")``.

    Eligibility: only calls whose AMD verdict was machine-ish (``ended_reason``
    in :data:`MACHINE_ENDED_REASONS`) are considered — otherwise the call was
    already a real conversation / hangup and must not be second-guessed.
    """
    try:
        text = (transcript or "").strip()
        if len(text) < _MIN_TRANSCRIPT_LEN:
            return (False, "")
        if not _is_machine_ended_reason(ended_reason):
            return (False, "not a machine-classified call")

        turns = _parse_turns(text)
        user_turns = [(i, t) for i, (spk, t) in enumerate(turns) if spk == "user"]
        if not user_turns:
            return (False, "no user turns")

        # Find the first machine/hold greeting among the User turns. That anchors
        # "after the machine greeting".
        machine_idx: int | None = None
        for idx, t in user_turns:
            if _looks_machine(t):
                machine_idx = idx
                break

        if machine_idx is None:
            # No machine greeting detected. On a voicemail-classified call this
            # is ambiguous (the "machine" may just be silence) — stay
            # conservative and do NOT reclassify.
            return (False, "no machine/hold greeting detected — leaving as voicemail")

        # Look for a live-human User turn strictly AFTER the machine greeting.
        for idx, t in user_turns:
            if idx <= machine_idx:
                continue
            # A later User turn that is ITSELF just another machine greeting is
            # not a human (IVR loops repeat) — skip it.
            if _looks_machine(t):
                continue
            reason = _human_signal(t)
            if reason:
                return (True, f"{reason} after hold/machine greeting")

        return (False, "no live human after machine greeting")
    except Exception as exc:  # noqa: BLE001 — fail-soft, never block reconcile
        _LOG.warning("gatekeeper detect failed: %s", exc)
        return (False, "")
