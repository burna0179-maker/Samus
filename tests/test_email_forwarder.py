"""Tests for backend.intake.email_forwarder — categorized forward + trash."""
from __future__ import annotations

from email import message_from_bytes, policy as email_policy


def _parse_msg(b: bytes):
    """Parse with the default policy so .get_content() is available."""
    return message_from_bytes(b, policy=email_policy.default)
from unittest.mock import MagicMock

import pytest

from backend.intake.email_forwarder import (
    ForwardCategory,
    build_forward_mime,
    choose_category,
    forward_and_cleanup,
    is_configured,
)
from backend.intake.gmail_poller import ParsedInboundEmail


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _mk_parsed(**over) -> ParsedInboundEmail:
    base = {
        "message_id": "<test@example>",
        "from_addr": "sender@example.com",
        "from_display": "Sender <sender@example.com>",
        "to_addrs": ["samushustleforge@gmail.com"],
        "subject": "Test subject",
        "date_header": "Thu, 10 Jul 2026 10:00:00 -0700",
        "body_text": "Hello world.",
        "body_format": "text",
        "attachment_names": [],
    }
    base.update(over)
    return ParsedInboundEmail(**base)


def _env(monkeypatch, **kv):
    for k, v in kv.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


# ---------------------------------------------------------------------------
# choose_category
# ---------------------------------------------------------------------------

def test_intent_action_prefix_wins_when_provided():
    cat = choose_category(
        classification={"category": "client_correspondence", "confidence": 1.0},
        intent={"intent": "counter_offered"},
        intent_action_prefix="[CLIENT/COUNTER]",
    )
    assert cat.prefix == "[CLIENT/COUNTER]"
    assert cat.is_urgent is False


def test_cs_prefix_is_urgent():
    cat = choose_category(
        classification={"category": "client_correspondence", "confidence": 1.0},
        intent={"intent": "service_issue_reported"},
        intent_action_prefix="[CS/SERVICE]",
    )
    assert cat.is_urgent is True


def test_bill_gets_vendor_suffix():
    cat = choose_category(
        classification={
            "category": "bill",
            "confidence": 0.9,
            "vendor_registry_id": "anthropic",
        },
        intent=None,
    )
    assert cat.prefix == "[BILL/ANTHROPIC]"
    assert cat.is_urgent is False


def test_bill_declined_gets_declined_suffix_when_no_vendor():
    cat = choose_category(
        classification={
            "category": "bill",
            "confidence": 0.6,
            "bill_signal_kind": "payment_declined",
        },
        intent=None,
    )
    assert cat.prefix == "[BILL/DECLINED]"


def test_social_bucket():
    cat = choose_category(
        classification={"category": "social", "confidence": 0.95},
        intent=None,
    )
    assert cat.prefix == "[SOCIAL]"


def test_category_other_falls_to_urgent_unclassified():
    cat = choose_category(
        classification={"category": "other", "confidence": 0.9},
        intent=None,
    )
    assert cat.prefix == "[URGENT/UNCLASSIFIED]"
    assert cat.is_urgent is True


def test_low_confidence_falls_to_urgent_unclassified(monkeypatch):
    monkeypatch.setenv("SAMUS_FORWARD_CLASSIFY_MIN_CONFIDENCE", "0.5")
    cat = choose_category(
        classification={"category": "business", "confidence": 0.3},
        intent=None,
    )
    assert cat.prefix == "[URGENT/UNCLASSIFIED]"


def test_missing_classification_falls_to_urgent():
    assert choose_category(None, None).prefix == "[URGENT/UNCLASSIFIED]"


def test_missing_category_falls_to_urgent():
    cat = choose_category(classification={"confidence": 0.9}, intent=None)
    assert cat.prefix == "[URGENT/UNCLASSIFIED]"


# ---------------------------------------------------------------------------
# build_forward_mime
# ---------------------------------------------------------------------------

def test_build_mime_subject_carries_prefix():
    parsed = _mk_parsed(subject="Re: Question about the plan")
    cat = ForwardCategory(prefix="[CLIENT/COUNTER]", is_urgent=False)
    b = build_forward_mime(
        from_email="samushustleforge@gmail.com",
        to_email="operator@example.com",
        parsed=parsed,
        category=cat,
    )
    msg = _parse_msg(b)
    assert msg["Subject"] == "[CLIENT/COUNTER] Re: Question about the plan"
    assert msg["From"] == "samushustleforge@gmail.com"
    assert msg["To"] == "operator@example.com"
    body = msg.get_content()
    assert "Forwarded message" in body
    assert "sender@example.com" in body
    assert "Hello world." in body


def test_build_mime_body_carries_intent_summary():
    parsed = _mk_parsed()
    cat = ForwardCategory(prefix="[CLIENT/COUNTER]", is_urgent=False)
    b = build_forward_mime(
        from_email="from@x", to_email="to@y", parsed=parsed, category=cat,
        intent_summary="counter_offered -- Client counter-offers $300/mo.",
    )
    body = _parse_msg(b).get_content()
    assert "Samus intent: counter_offered" in body
    assert "$300/mo" in body


def test_build_mime_handles_no_body():
    parsed = _mk_parsed(body_text="")
    cat = ForwardCategory(prefix="[SOCIAL]", is_urgent=False)
    b = build_forward_mime(
        from_email="a@b", to_email="c@d", parsed=parsed, category=cat,
    )
    body = _parse_msg(b).get_content()
    assert "(no body)" in body


def test_build_mime_truncates_long_subject():
    long_subject = "X" * 300
    parsed = _mk_parsed(subject=long_subject)
    cat = ForwardCategory(prefix="[SOCIAL]", is_urgent=False)
    b = build_forward_mime(
        from_email="a@b", to_email="c@d", parsed=parsed, category=cat,
    )
    subj = str(_parse_msg(b)["Subject"])
    # Long subjects get Q-encoded across multiple lines but the decoded
    # form contains the "..." suffix + the "[SOCIAL]" prefix.
    assert "[SOCIAL]" in subj
    # Count X's — decoder-transparent check that we truncated (< 300 X's).
    assert subj.count("X") < 300


# ---------------------------------------------------------------------------
# is_configured
# ---------------------------------------------------------------------------

def test_is_configured_requires_target_and_from(monkeypatch):
    _env(
        monkeypatch,
        SAMUS_FORWARD_ENABLED="1",
        SAMUS_FORWARD_TO_EMAIL="operator@example.com",
        SAMUS_GMAIL_INBOX_EMAIL="samushustleforge@gmail.com",
    )
    assert is_configured() is True


def test_is_configured_false_when_flag_off(monkeypatch):
    _env(
        monkeypatch,
        SAMUS_FORWARD_ENABLED="0",
        SAMUS_FORWARD_TO_EMAIL="operator@example.com",
        SAMUS_GMAIL_INBOX_EMAIL="samushustleforge@gmail.com",
    )
    assert is_configured() is False


def test_is_configured_false_when_target_missing(monkeypatch):
    _env(
        monkeypatch,
        SAMUS_FORWARD_ENABLED="1",
        SAMUS_FORWARD_TO_EMAIL="",
        SAMUS_GMAIL_INBOX_EMAIL="samushustleforge@gmail.com",
    )
    assert is_configured() is False


# ---------------------------------------------------------------------------
# forward_and_cleanup — orchestration + fail-soft
# ---------------------------------------------------------------------------

@pytest.fixture()
def _configured(monkeypatch):
    _env(
        monkeypatch,
        SAMUS_FORWARD_ENABLED="1",
        SAMUS_FORWARD_TO_EMAIL="operator@example.com",
        SAMUS_GMAIL_INBOX_EMAIL="samushustleforge@gmail.com",
    )


def test_forward_short_circuits_when_not_configured(monkeypatch):
    _env(monkeypatch, SAMUS_FORWARD_ENABLED="0", SAMUS_FORWARD_TO_EMAIL="",
         SAMUS_GMAIL_INBOX_EMAIL="samushustleforge@gmail.com")
    result = forward_and_cleanup(
        gmail_client=MagicMock(),
        original_gmail_id="abc",
        parsed=_mk_parsed(),
        classification={"category": "social", "confidence": 0.95},
    )
    assert result.forwarded is False
    assert result.error == "not_configured"


def test_forward_happy_path_sends_and_trashes(_configured):
    client = MagicMock()
    client.send_raw.return_value = "sent-msg-id-123"
    result = forward_and_cleanup(
        gmail_client=client,
        original_gmail_id="gmail-abc",
        parsed=_mk_parsed(),
        classification={"category": "social", "confidence": 0.95},
    )
    assert result.forwarded is True
    assert result.trashed is True
    assert result.forward_msg_id == "sent-msg-id-123"
    assert result.category_prefix == "[SOCIAL]"
    client.send_raw.assert_called_once()
    client.trash.assert_called_once_with("gmail-abc")


def test_forward_skips_trash_when_env_disables(monkeypatch, _configured):
    monkeypatch.setenv("SAMUS_FORWARD_TRASH_ORIGINAL", "0")
    client = MagicMock()
    client.send_raw.return_value = "sent-1"
    result = forward_and_cleanup(
        gmail_client=client,
        original_gmail_id="gmail-abc",
        parsed=_mk_parsed(),
        classification={"category": "social", "confidence": 0.95},
    )
    assert result.forwarded is True
    assert result.trashed is False
    client.trash.assert_not_called()


def test_forward_send_failure_leaves_original_alone(_configured):
    client = MagicMock()
    client.send_raw.side_effect = RuntimeError("network down")
    result = forward_and_cleanup(
        gmail_client=client,
        original_gmail_id="gmail-abc",
        parsed=_mk_parsed(),
        classification={"category": "social", "confidence": 0.95},
    )
    assert result.forwarded is False
    assert result.trashed is False
    assert "send_failed" in result.error
    client.trash.assert_not_called()


def test_forward_trash_failure_still_counts_as_forwarded(_configured):
    client = MagicMock()
    client.send_raw.return_value = "sent-1"
    client.trash.side_effect = RuntimeError("trash failed")
    result = forward_and_cleanup(
        gmail_client=client,
        original_gmail_id="gmail-abc",
        parsed=_mk_parsed(),
        classification={"category": "social", "confidence": 0.95},
    )
    assert result.forwarded is True
    assert result.trashed is False


def test_forward_uses_intent_prefix_when_provided(_configured):
    """Inbound client mail with an intent prefix gets forwarded and
    carries the rich CLIENT EXPECTATIONS block in the body."""
    client = MagicMock()
    client.send_raw.return_value = "sent-1"
    result = forward_and_cleanup(
        gmail_client=client,
        original_gmail_id="gmail-abc",
        parsed=_mk_parsed(from_addr="<client-email>@example.com"),
        classification={
            "category": "client_correspondence",
            "confidence": 1.0,
            "direction": "inbound",
            "client_id": "sample_school",
            "client_role": "approval_contact",
        },
        intent={
            "intent": "counter_offered",
            "summary_sentence": "Kerry counter-offers $300/mo.",
            "requested_action": "Provide thoughts on continuing at $300/mo",
            "sentiment": "neutral",
        },
        intent_action_prefix="[CLIENT/COUNTER]",
    )
    assert result.category_prefix == "[CLIENT/COUNTER]"
    assert result.is_urgent is False
    call_bytes = client.send_raw.call_args[0][0]
    msg = _parse_msg(call_bytes)
    assert "[CLIENT/COUNTER]" in msg["Subject"]
    body = msg.get_content()
    # Rich CLIENT EXPECTATIONS block
    assert "CLIENT" in body
    assert "Sample School" in body
    assert "INTENT" in body and "counter_offered" in body
    assert "URGENCY" in body and "NORMAL" in body
    assert "WHAT THE CLIENT EXPECTS" in body
    assert "Provide thoughts on continuing at $300/mo" in body
    assert "SAMUS SUMMARY" in body
    assert "Kerry counter-offers" in body


def test_forward_skips_outbound_client_correspondence(_configured):
    """Operator forwarded their OWN reply to samus's inbox as an archive.
    That's already in the operator's sent folder — don't re-forward to the operator."""
    client = MagicMock()
    result = forward_and_cleanup(
        gmail_client=client,
        original_gmail_id="gmail-abc",
        parsed=_mk_parsed(from_addr="ahartman@hustleforge.tech"),
        classification={
            "category": "client_correspondence",
            "confidence": 1.0,
            "direction": "outbound",
            "client_id": "sample_school",
            "client_role": "approval_contact",
        },
        intent={"intent": "accepted_counter_offer"},
        intent_action_prefix="[CLIENT/COUNTER]",
    )
    assert result.forwarded is False
    assert result.trashed is False
    assert result.error == "skipped_outbound_client_archive"
    client.send_raw.assert_not_called()
    client.trash.assert_not_called()


def test_forward_urgent_cs_uses_urgent_urgency(_configured):
    client = MagicMock()
    client.send_raw.return_value = "sent-1"
    forward_and_cleanup(
        gmail_client=client,
        original_gmail_id="gmail-abc",
        parsed=_mk_parsed(from_addr="<client-email>@example.com"),
        classification={
            "category": "client_correspondence",
            "confidence": 1.0,
            "direction": "inbound",
            "client_id": "sample_school",
        },
        intent={
            "intent": "service_issue_reported",
            "summary_sentence": "Site is down.",
            "requested_action": "Fix the landing page ASAP",
        },
        intent_action_prefix="[CS/SERVICE]",
    )
    call_bytes = client.send_raw.call_args[0][0]
    body = _parse_msg(call_bytes).get_content()
    assert "URGENCY" in body and "URGENT" in body


def test_forward_strips_html_from_body(_configured):
    """A Titan-style HTML body should arrive at the operator as clean text."""
    client = MagicMock()
    client.send_raw.return_value = "sent-1"
    html_body = (
        '<div style="color:red">'
        'Hi Alex, we need to discuss the timeline.'
        '</div>'
    )
    forward_and_cleanup(
        gmail_client=client,
        original_gmail_id="gmail-x",
        parsed=_mk_parsed(subject="A meeting request", body_text=html_body),
        classification={"category": "social", "confidence": 0.9},
    )
    call_bytes = client.send_raw.call_args[0][0]
    body = _parse_msg(call_bytes).get_content()
    # HTML tags gone
    assert "<div" not in body
    assert "color:red" not in body
    # Content preserved
    assert "Hi Alex, we need to discuss the timeline." in body


def test_urgent_unclassified_when_no_classification(_configured):
    client = MagicMock()
    client.send_raw.return_value = "sent-1"
    result = forward_and_cleanup(
        gmail_client=client,
        original_gmail_id="gmail-x",
        parsed=_mk_parsed(subject="something weird"),
        classification=None,
    )
    assert result.category_prefix == "[URGENT/UNCLASSIFIED]"
    assert result.is_urgent is True
    call_bytes = client.send_raw.call_args[0][0]
    assert b"[URGENT/UNCLASSIFIED]" in call_bytes
