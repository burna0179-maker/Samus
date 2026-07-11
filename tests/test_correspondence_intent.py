"""Tests for backend.intake.correspondence_intent — LLM intent reasoning."""
from __future__ import annotations

import json

from backend.intake.correspondence_intent import (
    INBOUND_INTENTS,
    OUTBOUND_INTENTS,
    IntentReasoning,
    reason_intent,
)


def _mk_chat(response: str):
    """Return a chat stub that always returns ``response``."""
    def _stub(system, user, **kw):
        _stub.last_call = {"system": system, "user": user, **kw}
        return response
    _stub.last_call = None  # type: ignore[attr-defined]
    return _stub


# --- happy paths ------------------------------------------------------------

def test_reason_intent_inbound_counter_offered_from_json():
    chat = _mk_chat(json.dumps({
        "intent": "counter_offered",
        "secondary_intents": ["objected_price"],
        "sentiment": "neutral",
        "requested_action": "Confirm phased approach at $300/mo",
        "summary_sentence": "Pastor counter-offers $300/mo instead of $3550 lump sum.",
        "confidence": 0.9,
    }))
    r = reason_intent(
        direction="inbound",
        client_id="sample_school",
        subject="Re: Enrollment Operations",
        body_text="The ministry does not have that kind of resources...",
        llm_chat=chat,
    )
    assert r.error == ""
    assert r.intent == "counter_offered"
    assert r.secondary_intents == ["objected_price"]
    assert r.sentiment == "neutral"
    assert "phased" in r.requested_action.lower()
    assert 0.0 <= r.confidence <= 1.0
    # Prompt sanity — direction + client + intents list all reach the model
    call = chat.last_call
    assert "DIRECTION: inbound" in call["user"]
    assert "sample_school" in call["user"]
    assert "counter_offered" in call["user"]


def test_reason_intent_outbound_accepted_counter_offer():
    chat = _mk_chat(json.dumps({
        "intent": "accepted_counter_offer",
        "secondary_intents": ["revised_scope"],
        "sentiment": "positive",
        "requested_action": "",
        "summary_sentence": "Alex accepts phased $300/mo, will revise roadmap.",
        "confidence": 0.85,
    }))
    r = reason_intent(
        direction="outbound",
        client_id="sample_school",
        subject="Re: Enrollment Operations",
        body_text="I'd rather take a phased approach that grows alongside the school...",
        llm_chat=chat,
    )
    assert r.intent == "accepted_counter_offer"
    assert r.sentiment == "positive"


# --- fail-soft behavior -----------------------------------------------------

def test_reason_intent_empty_body_short_circuits():
    r = reason_intent(
        direction="inbound", client_id="x", subject="s", body_text="",
        llm_chat=_mk_chat("should not be called"),
    )
    assert r.error == "empty_body"
    assert r.intent == ""


def test_reason_intent_llm_returns_empty_string():
    r = reason_intent(
        direction="inbound", client_id="x", subject="s", body_text="body",
        llm_chat=_mk_chat(""),
    )
    assert r.error == "llm_empty_response"


def test_reason_intent_llm_returns_malformed_json():
    r = reason_intent(
        direction="inbound", client_id="x", subject="s", body_text="body",
        llm_chat=_mk_chat("here is my answer: not JSON at all"),
    )
    assert r.error.startswith("llm_output_not_json")


def test_reason_intent_llm_raises_is_caught():
    def _boom(s, u, **kw):
        raise RuntimeError("network blew up")
    r = reason_intent(
        direction="inbound", client_id="x", subject="s", body_text="body",
        llm_chat=_boom,
    )
    assert r.error.startswith("llm_raised")


# --- schema validation ------------------------------------------------------

def test_reason_intent_extracts_json_from_wrapping_prose():
    chat = _mk_chat(
        'Sure! Here is the classification:\n'
        '{"intent": "acknowledgment", "sentiment": "positive", '
        '"summary_sentence": "Thanks for the update.", '
        '"confidence": 0.7, "requested_action": "", "secondary_intents": []}'
        '\nHope that helps!'
    )
    r = reason_intent(
        direction="inbound", client_id="x", subject="s", body_text="body",
        llm_chat=chat,
    )
    assert r.error == ""
    assert r.intent == "acknowledgment"


def test_reason_intent_downgrades_out_of_vocab_intent_to_unknown():
    chat = _mk_chat(json.dumps({
        "intent": "definitely_not_a_real_intent",
        "sentiment": "neutral",
        "summary_sentence": "hi",
        "confidence": 0.5,
    }))
    r = reason_intent(
        direction="inbound", client_id="x", subject="s", body_text="body",
        llm_chat=chat,
    )
    assert r.intent == "unknown"
    assert r.summary_sentence == "hi"  # other fields kept


def test_reason_intent_ignores_bad_sentiment_value():
    chat = _mk_chat(json.dumps({
        "intent": "acknowledgment",
        "sentiment": "wildly_optimistic",
        "summary_sentence": "hi",
        "confidence": 0.5,
    }))
    r = reason_intent(
        direction="inbound", client_id="x", subject="s", body_text="body",
        llm_chat=chat,
    )
    assert r.sentiment == ""  # rejected


def test_reason_intent_clamps_confidence_range():
    chat = _mk_chat(json.dumps({
        "intent": "acknowledgment",
        "sentiment": "positive",
        "summary_sentence": "hi",
        "confidence": 2.5,  # bogus over-1.0
    }))
    r = reason_intent(
        direction="inbound", client_id="x", subject="s", body_text="body",
        llm_chat=chat,
    )
    assert r.confidence == 1.0


def test_reason_intent_filters_secondary_intents_against_vocab():
    chat = _mk_chat(json.dumps({
        "intent": "counter_offered",
        "secondary_intents": [
            "objected_price",
            "wholly_invented_intent",
            "counter_offered",  # duplicate of primary — drop
        ],
        "sentiment": "neutral",
        "summary_sentence": "hi",
        "confidence": 0.6,
    }))
    r = reason_intent(
        direction="inbound", client_id="x", subject="s", body_text="body",
        llm_chat=chat,
    )
    assert r.secondary_intents == ["objected_price"]


def test_to_dict_omits_empty_fields():
    r = IntentReasoning(intent="acknowledgment", summary_sentence="hi", confidence=0.4)
    d = r.to_dict()
    assert d == {"intent": "acknowledgment", "summary_sentence": "hi", "confidence": 0.4}
    assert "sentiment" not in d
    assert "requested_action" not in d
    assert "error" not in d


def test_inbound_and_outbound_intents_are_disjoint_business_meanings():
    # Basic sanity: both sets are non-empty and contain "unknown" fallback
    assert "unknown" in INBOUND_INTENTS
    assert "unknown" in OUTBOUND_INTENTS
    # The two vocabularies overlap only on "unknown" (by design — they
    # describe different sides of the conversation).
    assert INBOUND_INTENTS & OUTBOUND_INTENTS == {"unknown"}
