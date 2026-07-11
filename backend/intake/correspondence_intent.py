"""LLM-backed intent reasoning over client correspondence.

Runs on every ``client_correspondence`` email (both directions) after the
classifier has resolved client identity. Extracts a structured
:class:`IntentReasoning` from the message body — WHAT the sender is trying
to do, what they want next, and the emotional tone — so downstream
surfaces (operator queue, campaign posture updates) can act on intent, not
just text.

Design:

* **Local-first** via :func:`backend.common.local_llm.chat`. LM Studio
  first, OpenAI fallback if configured. Zero paid spend on the default
  path — client thread reasoning is free.
* **Fail-soft**. ``chat`` never raises; if it returns empty or the JSON
  won't parse, this module returns an empty :class:`IntentReasoning`
  with ``error`` populated. The caller attaches the result to the
  artifact when non-empty and simply skips it on error — the thread is
  still captured, just without intent tags.
* **Save->parse->show->approve->run**. The LLM emits structured JSON
  matching a known schema; unknown intent values fall back to
  ``"unknown"`` rather than crashing so a model hallucination can't
  break the pipeline.
* **Direction-aware intent vocabulary**. Inbound (client -> us) and
  outbound (us -> client) draw from separate, business-meaningful tag
  sets. The prompt tells the model which side we're on.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

_LOG = logging.getLogger("samus.intake.correspondence_intent")

DirectionType = Literal["inbound", "outbound"]

# Business-meaningful tag sets, kept small so the model picks reliably.
INBOUND_INTENTS: frozenset[str] = frozenset({
    "agreed_to_move_forward",
    "counter_offered",
    "objected_price",
    "objected_scope",
    "objected_timing",
    "requested_more_info",
    "requested_meeting",
    "expressed_hesitation",
    "closed_out_conversation",
    "service_issue_reported",
    "question_general",
    "acknowledgment",
    "escalation_needed",
    "unknown",
})

OUTBOUND_INTENTS: frozenset[str] = frozenset({
    "accepted_counter_offer",
    "sent_new_proposal",
    "sent_signing_link",
    "offered_installments",
    "revised_scope",
    "escalated_to_meeting",
    "follow_up_check_in",
    "closed_gracefully",
    "service_response",
    "general_reply",
    "unknown",
})

_SENTIMENTS: frozenset[str] = frozenset({"positive", "neutral", "negative"})


@dataclass
class IntentReasoning:
    """Structured intent extracted from a client-correspondence body."""

    intent: str = ""
    secondary_intents: list[str] = field(default_factory=list)
    sentiment: str = ""
    requested_action: str = ""
    summary_sentence: str = ""
    confidence: float = 0.0
    # Set on any failure path (LLM empty, JSON unparseable, invalid values).
    # A non-empty error means the other fields may be defaults; do NOT
    # attach to the artifact when error is set.
    error: str = ""

    def is_empty(self) -> bool:
        return not self.intent and not self.summary_sentence and not self.error

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.intent:
            d["intent"] = self.intent
        if self.secondary_intents:
            d["secondary_intents"] = self.secondary_intents
        if self.sentiment:
            d["sentiment"] = self.sentiment
        if self.requested_action:
            d["requested_action"] = self.requested_action
        if self.summary_sentence:
            d["summary_sentence"] = self.summary_sentence
        if self.confidence:
            d["confidence"] = round(self.confidence, 2)
        if self.error:
            d["error"] = self.error
        return d


_SYSTEM_PROMPT = """\
You are an intent classifier for a small consultancy's client email thread. You
respond with ONLY a JSON object that matches the schema below. Do not include
any prose before or after the JSON. Do not wrap the JSON in markdown fences.

Schema:
{
  "intent": "<one of the allowed intents for this direction>",
  "secondary_intents": ["<zero or more additional intents>"],
  "sentiment": "positive" | "neutral" | "negative",
  "requested_action": "<one-line description of what the sender wants next, or empty>",
  "summary_sentence": "<one-line plain-English summary of the message>",
  "confidence": <a float 0.0 to 1.0>
}

Rules:
- Pick the SINGLE closest intent as "intent".
- Use "unknown" if you truly cannot tell.
- Never invent an intent that isn't in the allowed list.
- Keep summary_sentence under 150 characters.
"""

_BODY_MAX_CHARS = 3000  # keep the prompt small; email bodies past this are rare
_MAX_TOKENS = 500
_TEMPERATURE = 0.0


def _allowed_intents(direction: DirectionType) -> frozenset[str]:
    return OUTBOUND_INTENTS if direction == "outbound" else INBOUND_INTENTS


def _clip_body(body: str) -> str:
    if not body:
        return ""
    b = body.strip()
    if len(b) <= _BODY_MAX_CHARS:
        return b
    return b[:_BODY_MAX_CHARS] + "\n[... truncated ...]"


def _build_user_prompt(
    *,
    direction: DirectionType,
    client_id: str,
    subject: str,
    body_text: str,
) -> str:
    intents = sorted(_allowed_intents(direction))
    return (
        f"DIRECTION: {direction}\n"
        f"CLIENT: {client_id}\n"
        f"SUBJECT: {subject}\n"
        f"ALLOWED INTENTS: {intents}\n\n"
        f"BODY:\n{_clip_body(body_text)}\n"
    )


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Return the first JSON object embedded in ``text``, or None."""
    if not text:
        return None
    # First: hope the whole thing is JSON.
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    # Fallback: greedy regex extract of the outermost {...}.
    m = _JSON_OBJECT_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _validate_and_pack(
    payload: dict[str, Any],
    direction: DirectionType,
) -> IntentReasoning:
    allowed = _allowed_intents(direction)

    intent = str(payload.get("intent") or "").strip().lower()
    if intent and intent not in allowed:
        # Model hallucinated an out-of-vocab tag — drop to unknown, keep
        # everything else so operator still sees the summary/sentiment.
        _LOG.info("intent %r not in %s allowed set; downgrading to unknown", intent, direction)
        intent = "unknown"

    secondary_raw = payload.get("secondary_intents") or []
    secondary: list[str] = []
    if isinstance(secondary_raw, list):
        for s in secondary_raw:
            s = str(s or "").strip().lower()
            if s and s != intent and s in allowed:
                secondary.append(s)

    sentiment = str(payload.get("sentiment") or "").strip().lower()
    if sentiment not in _SENTIMENTS:
        sentiment = ""

    requested_action = str(payload.get("requested_action") or "").strip()
    summary_sentence = str(payload.get("summary_sentence") or "").strip()

    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return IntentReasoning(
        intent=intent,
        secondary_intents=secondary,
        sentiment=sentiment,
        requested_action=requested_action[:280],
        summary_sentence=summary_sentence[:200],
        confidence=confidence,
    )


def reason_intent(
    *,
    direction: DirectionType,
    client_id: str,
    subject: str,
    body_text: str,
    llm_chat=None,
) -> IntentReasoning:
    """Extract intent + sentiment + requested action from one message body.

    Fail-soft end to end: LLM unavailable / empty output / malformed JSON
    yields ``IntentReasoning(error=...)`` — never raises.

    ``llm_chat`` is a seam for tests. Production leaves it None and this
    module imports :func:`backend.common.local_llm.chat` lazily.
    """
    if not body_text or not body_text.strip():
        return IntentReasoning(error="empty_body")

    if llm_chat is None:
        try:
            from backend.common.local_llm import chat as llm_chat  # type: ignore
        except Exception as exc:  # noqa: BLE001
            return IntentReasoning(error=f"llm_client_import_failed: {exc}")

    user_prompt = _build_user_prompt(
        direction=direction,
        client_id=client_id,
        subject=subject,
        body_text=body_text,
    )

    try:
        raw = llm_chat(
            _SYSTEM_PROMPT, user_prompt,
            max_tokens=_MAX_TOKENS, temperature=_TEMPERATURE,
        )
    except Exception as exc:  # noqa: BLE001 — chat should never raise, defensive
        _LOG.warning("intent llm call raised: %s", exc)
        return IntentReasoning(error=f"llm_raised: {exc}")

    if not raw or not raw.strip():
        return IntentReasoning(error="llm_empty_response")

    payload = _extract_json_object(raw)
    if payload is None:
        return IntentReasoning(error=f"llm_output_not_json: {raw[:200]!r}")

    return _validate_and_pack(payload, direction)


__all__ = [
    "DirectionType",
    "INBOUND_INTENTS",
    "OUTBOUND_INTENTS",
    "IntentReasoning",
    "reason_intent",
]
