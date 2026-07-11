"""Reply-handling pod chain — classifier, drafter, and poller wiring."""

from __future__ import annotations


import pytest

from backend.intake import follow_up_drafter as drafter
from backend.intake import reply_classifier as rc


# ---------------------------------------------------------------------------
# IntentClassifier
# ---------------------------------------------------------------------------


def test_opt_out_wins_over_other_signals():
    # Legal-first: an unsubscribe must win even alongside "interested".
    out = rc.classify_reply("Re: pricing", "Yes I'm interested but please unsubscribe me.")
    assert out.intent == rc.INTENT_OPT_OUT
    assert out.confidence >= 0.6


@pytest.mark.parametrize(
    "text,intent",
    [
        ("Let's schedule a call next week", rc.INTENT_MEETING_BOOKED),
        ("what times work for you?", rc.INTENT_MEETING_BOOKED),
        ("This is interesting, send me pricing", rc.INTENT_INTERESTED),
        ("tell me more about how much it costs", rc.INTENT_INTERESTED),
        ("No thanks, we're all set", rc.INTENT_NOT_INTERESTED),
        ("not interested", rc.INTENT_NOT_INTERESTED),
        ("ok", rc.INTENT_UNKNOWN),
        ("", rc.INTENT_UNKNOWN),
    ],
)
def test_intent_classification(text, intent):
    assert rc.classify_reply("", text).intent == intent


def test_unknown_has_zero_confidence():
    assert rc.classify_reply("", "thanks").confidence == 0.0


def test_signals_recorded():
    out = rc.classify_reply("", "please unsubscribe and remove me")
    assert out.intent == rc.INTENT_OPT_OUT
    assert len(out.signals) >= 2  # both patterns matched


# ---------------------------------------------------------------------------
# FollowUpDrafter (+ ComplianceGuard integration)
# ---------------------------------------------------------------------------


@pytest.fixture
def draft_env(monkeypatch):
    monkeypatch.setenv("SAMUS_SENDER_POSTAL_ADDRESS", "HustleForge LLC, Marysville, CA 95901")
    monkeypatch.setenv("SAMUS_UNSUBSCRIBE_URL", "https://hustleforge.tech/unsubscribe")
    monkeypatch.setenv("SAMUS_COMPLIANCE_GUARD_MODE", "off")
    from backend.common.settings import reload_settings

    reload_settings()
    # Nobody suppressed during drafting checks.
    monkeypatch.setattr("backend.common.compliance_guard.is_email_suppressed", lambda e: False)


def test_opt_out_produces_no_draft(draft_env):
    d = drafter.draft_follow_up(rc.INTENT_OPT_OUT, from_addr="x@y.com")
    assert d.body == ""
    assert d.send_recommended is False
    assert "opt-out" in d.note.lower()


def test_interested_draft_is_compliant_and_recommended(draft_env):
    d = drafter.draft_follow_up(
        rc.INTENT_INTERESTED,
        original_subject="quick question",
        from_addr="lead@y.com",
        company="Acme",
    )
    assert d.subject.startswith("Re:")
    assert "Unsubscribe:" in d.body  # footer injected -> passes guard
    assert d.compliance["ok"] is True
    assert d.send_recommended is True


def test_interested_draft_not_recommended_without_compliance(monkeypatch):
    # No postal/unsubscribe configured -> guard verdict not ok -> not recommended.
    monkeypatch.setenv("SAMUS_SENDER_POSTAL_ADDRESS", "")
    monkeypatch.setenv("SAMUS_UNSUBSCRIBE_URL", "")
    from backend.common.settings import reload_settings

    reload_settings()
    monkeypatch.setattr("backend.common.compliance_guard.is_email_suppressed", lambda e: False)
    d = drafter.draft_follow_up(rc.INTENT_INTERESTED, from_addr="lead@y.com")
    assert d.compliance["ok"] is False
    assert d.send_recommended is False  # intent-appropriate but non-compliant


def test_not_interested_draft_not_auto_recommended(draft_env):
    d = drafter.draft_follow_up(rc.INTENT_NOT_INTERESTED, from_addr="x@y.com")
    assert d.send_recommended is False  # courtesy close is operator-optional


def test_unknown_produces_no_draft(draft_env):
    d = drafter.draft_follow_up(rc.INTENT_UNKNOWN, from_addr="x@y.com")
    assert d.body == ""


# ---------------------------------------------------------------------------
# Poller wiring (_handle_reply_intent)
# ---------------------------------------------------------------------------


def _parsed(subject: str, body: str, from_addr: str = "lead@y.com"):
    from backend.intake.gmail_poller import ParsedInboundEmail

    return ParsedInboundEmail(
        message_id="m1",
        from_addr=from_addr,
        from_display="Lead",
        to_addrs=["samus@hustleforge.tech"],
        subject=subject,
        date_header="",
        body_text=body,
        body_format="text",
        attachment_names=[],
    )


@pytest.fixture
def wiring_env(draft_env, monkeypatch):
    signals: list = []
    sup: list = []
    arts: list = []
    monkeypatch.setattr(
        "backend.feedback.handlers.fire_cash_engine_signal",
        lambda **kw: signals.append(kw) or {"ok": True},
    )
    monkeypatch.setattr(
        "backend.common.recipient_index.lookup_recipient",
        lambda email, **kw: {"prospect_id": "pr_1", "opportunity_id": "op_1"},
    )

    class _Tbl:
        def put_item(self, Item=None):  # noqa: N803
            sup.append(Item)

    monkeypatch.setattr("backend.common.aws.table", lambda *a, **k: _Tbl())
    monkeypatch.setattr(
        "backend.crm.service.create_artifact",
        lambda req: (
            arts.append(req)
            or type("R", (), {"artifact_id": "a1", "status": "created", "error": None})()
        ),
    )
    return signals, sup, arts


def test_opt_out_fires_unsubscribe_suppresses_and_drafts(wiring_env):
    signals, sup, arts = wiring_env
    from backend.intake.gmail_poller import _handle_reply_intent

    _handle_reply_intent(_parsed("Re: hi", "please unsubscribe me"), "op_1")
    assert any(s["event"] == "unsubscribe" for s in signals)
    assert sup and sup[0]["email"] == "lead@y.com"  # suppressed
    assert sup[0]["reason"] == "reply_opt_out"
    assert arts and arts[0].kind == "content_draft"
    assert arts[0].inline_data.get("artifact_subtype") == "follow_up_draft"


def test_interested_fires_reply_and_drafts(wiring_env):
    signals, sup, arts = wiring_env
    from backend.intake.gmail_poller import _handle_reply_intent

    _handle_reply_intent(_parsed("Re: hi", "interested, send pricing"), "op_1")
    assert any(s["event"] == "reply" for s in signals)
    assert not sup  # no suppression for interested
    assert arts and arts[0].kind == "content_draft"
    assert arts[0].inline_data.get("artifact_subtype") == "follow_up_draft"


def test_not_interested_fires_no_signal_but_drafts(wiring_env):
    signals, sup, arts = wiring_env
    from backend.intake.gmail_poller import _handle_reply_intent

    _handle_reply_intent(_parsed("Re: hi", "no thanks, not a fit"), "op_1")
    assert signals == []  # soft no -> no state signal
    assert arts and arts[0].kind == "content_draft"
    assert arts[0].inline_data.get("artifact_subtype") == "follow_up_draft"
