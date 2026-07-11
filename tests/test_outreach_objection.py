"""Tests for backend.outreach.objection — 6-category detector + pivot lookup."""

from __future__ import annotations


from backend.outreach.objection import (
    OBJECTION_KEYWORDS,
    PIVOT_TABLE,
    RESPONSE_TABLE,
    detect_objection,
    handle_objection,
)


# ---------------------------------------------------------------------------
# detect_objection — per-category pattern coverage
# ---------------------------------------------------------------------------


def test_detect_price_objection_matches():
    assert detect_objection("That sounds too expensive for us") == "price"
    assert detect_objection("I can't afford that right now") == "price"
    assert detect_objection("Do you have anything cheaper?") == "price"
    assert detect_objection("It's out of my budget this quarter") == "price"
    assert detect_objection("The service costs too much for our team") == "price"
    assert detect_objection("That is too much money to spend") == "price"


def test_detect_not_interested_matches():
    assert detect_objection("I'm not interested, thanks") == "not_interested"
    assert detect_objection("We don't need that kind of service") == "not_interested"
    assert detect_objection("No thanks, we're good") == "not_interested"
    assert detect_objection("I'll pass on this one") == "not_interested"
    assert detect_objection("It's not for us") == "not_interested"


def test_detect_already_have_matches():
    assert detect_objection("We already have a vendor for that") == "already_have"
    assert detect_objection("I'm already using something similar") == "already_have"
    assert detect_objection("We have someone who handles that") == "already_have"
    assert detect_objection("We already got someone for that") == "already_have"


def test_detect_timing_matches():
    assert detect_objection("Call me back next month") == "timing"
    assert detect_objection("This is a really bad time") == "timing"
    assert detect_objection("Try me next quarter") == "timing"
    assert detect_objection("I'm busy right now, can we reschedule?") == "timing"
    assert detect_objection("Call me later this week") == "timing"


def test_detect_trust_matches():
    assert detect_objection("This sounds like a scam to me") == "trust"
    assert detect_objection("Honestly this looks like spam") == "trust"
    assert detect_objection("Can you prove that it actually works?") == "trust"
    assert detect_objection("I've never heard of you before") == "trust"
    assert detect_objection("How do I know this is legit?") == "trust"


def test_detect_no_need_matches():
    assert detect_objection("I don't see the need for that") == "no_need"
    assert detect_objection("Everything works fine as is") == "no_need"
    assert detect_objection("We have no issues right now") == "no_need"
    assert detect_objection("We're satisfied with what we have") == "no_need"


# ---------------------------------------------------------------------------
# detect_objection — general behaviour
# ---------------------------------------------------------------------------


def test_detect_is_case_insensitive():
    assert detect_objection("TOO EXPENSIVE") == "price"
    assert detect_objection("NOT INTERESTED") == "not_interested"
    assert detect_objection("ALREADY HAVE A VENDOR") == "already_have"
    assert detect_objection("CALL ME BACK LATER") == "timing"
    assert detect_objection("THIS IS A SCAM") == "trust"
    assert detect_objection("WORKS FINE") == "no_need"


def test_no_match_returns_none():
    assert detect_objection("Sure, that sounds interesting, tell me more") is None
    assert detect_objection("") is None
    assert detect_objection("Great, send me the proposal and I will review it.") is None


# ---------------------------------------------------------------------------
# detect_objection — priority order
# ---------------------------------------------------------------------------


def test_detect_priority_order_matters():
    # "too expensive" (price) + "not interested" both present; price wins.
    transcript = "I'm not interested, that's also too expensive for us"
    result = detect_objection(transcript)
    assert result == "price", f"Expected 'price' (higher priority) but got '{result}'"

    # "already have" + "timing" — already_have has higher priority.
    transcript2 = "We already have a vendor, and now is a bad time anyway"
    result2 = detect_objection(transcript2)
    assert result2 == "already_have", (
        f"Expected 'already_have' (higher priority) but got '{result2}'"
    )


# ---------------------------------------------------------------------------
# handle_objection
# ---------------------------------------------------------------------------


def test_handle_objection_returns_all_none_on_no_match():
    result = handle_objection("Sounds great, let's move forward")
    assert result == {"detected": None, "response": None, "pivot": None}


def test_handle_objection_detected_returns_full_dict():
    result = handle_objection("That's too expensive for our budget")
    assert result["detected"] == "price"
    assert isinstance(result["response"], str) and len(result["response"]) > 0
    assert result["pivot"] == PIVOT_TABLE["price"]


def test_handle_objection_response_matches_table():
    for category in OBJECTION_KEYWORDS:
        # Grab the first raw pattern text and strip regex metacharacters for a
        # natural-language probe (the literal words are present in every pattern).
        raw_pattern = OBJECTION_KEYWORDS[category][0]
        # Remove regex quantifiers / groups so we have plain text to probe with.
        plain = raw_pattern.replace("'?", "'").replace(r"\b", "").replace("?", "")
        result = handle_objection(plain)
        if result["detected"] == category:
            assert result["response"] == RESPONSE_TABLE[category]
            assert result["pivot"] == PIVOT_TABLE[category]


def test_handle_objection_pivot_override_via_intel():
    intel = {"recommended_secondary_product": "enterprise_bundle"}
    result = handle_objection("This is way too expensive", intel=intel)
    assert result["detected"] == "price"
    assert result["pivot"] == "enterprise_bundle"
    # Response still comes from the standard table.
    assert result["response"] == RESPONSE_TABLE["price"]


def test_handle_objection_intel_without_override_uses_default_pivot():
    intel = {"some_other_field": "value", "score": 0.8}
    result = handle_objection("Everything works fine, no issues here", intel=intel)
    assert result["detected"] == "no_need"
    assert result["pivot"] == PIVOT_TABLE["no_need"]


def test_handle_objection_intel_none_uses_default_pivot():
    result = handle_objection("We already have someone", intel=None)
    assert result["detected"] == "already_have"
    assert result["pivot"] == PIVOT_TABLE["already_have"]


def test_handle_objection_no_match_ignores_intel():
    intel = {"recommended_secondary_product": "enterprise_bundle"}
    result = handle_objection("Sounds perfect, let's proceed", intel=intel)
    assert result == {"detected": None, "response": None, "pivot": None}
