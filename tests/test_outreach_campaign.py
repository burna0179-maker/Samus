"""Offline tests for the email-campaign builder."""

from __future__ import annotations

import pytest

from backend.outreach.apollo_source import ApolloContact
from backend.outreach.campaign import (
    CampaignConfig,
    build_messages,
    compose_body,
    load_suppression,
)


_STAKE = (
    "I'm reaching out because Acme's new Marysville location is exactly when this matters most."
)


def _cfg(**over) -> CampaignConfig:
    base = dict(
        sender_postal_address="123 Main St, Yuba City, CA 95991",
        unsubscribe_url="https://hustleforge.tech/unsubscribe",
        max_send=25,
    )
    base.update(over)
    return CampaignConfig(**base)


def _contact(email="a@x.com", status="verified", pid="p1", **over) -> ApolloContact:
    base = dict(
        person_id=pid,
        first_name="Dana",
        name="Dana Reyes",
        title="Owner",
        company="Acme",
        email=email,
        email_status=status,
        # G8 (ADR-012, 2026-05-30): every contact entering the outreach
        # pipeline must carry a warmth signal. Tests synthesize one as
        # public_registry so VR-G8 doesn't refuse the compose.
        legitimacy_signal="public_registry",
    )
    base.update(over)
    return ApolloContact(**base)


def _stakes(*emails: str) -> dict[str, str]:
    return {e.lower(): _STAKE for e in emails}


def test_build_requires_compliance_fields():
    cfg = CampaignConfig()  # no postal address / unsubscribe
    with pytest.raises(ValueError):
        build_messages([_contact()], cfg)


def test_build_emits_validated_message_with_footer():
    res = build_messages([_contact()], _cfg(), stake_sentences=_stakes("a@x.com"))
    assert res.built == 1
    msg = res.messages[0]
    assert msg.channel == "email"
    assert msg.to == "a@x.com"
    assert msg.prospect_id == "apollo_p1"
    assert "Unsubscribe:" in msg.body
    assert "123 Main St" in msg.body
    assert "Dana" in msg.body  # first-name personalisation
    assert _STAKE in msg.body  # stake_sentence rendered verbatim at top


def test_unverified_excluded_by_default_but_allowed_when_flag_set():
    contacts = [_contact(email="g@x.com", status="guessed")]
    stakes = _stakes("g@x.com")
    res = build_messages(contacts, _cfg(), stake_sentences=stakes)
    assert res.built == 0
    assert res.not_sendable == 1

    res2 = build_messages(contacts, _cfg(require_verified_email=False), stake_sentences=stakes)
    assert res2.built == 1


def test_suppression_and_already_sent_skip():
    contacts = [_contact(email="dupe@x.com")]
    res = build_messages(contacts, _cfg(), already_sent={"DUPE@x.com"})
    assert res.built == 0
    assert res.suppressed == 1


def test_do_not_contact_via_locked_or_blank_email():
    res = build_messages([_contact(email="")], _cfg())
    assert res.built == 0
    assert res.not_sendable == 1


def test_in_batch_dedup():
    contacts = [_contact(email="same@x.com", pid="p1"), _contact(email="SAME@x.com", pid="p2")]
    res = build_messages(contacts, _cfg(), stake_sentences=_stakes("same@x.com"))
    assert res.built == 1
    assert res.duplicate == 1


def test_max_send_cap():
    contacts = [_contact(email=f"u{i}@x.com", pid=f"p{i}") for i in range(5)]
    stakes = _stakes(*[f"u{i}@x.com" for i in range(5)])
    res = build_messages(contacts, _cfg(max_send=2), stake_sentences=stakes)
    assert res.built == 2
    assert res.capped == 3


def test_compose_body_template_path_no_llm():
    body = compose_body(_contact(), _cfg(use_llm=False), stake_sentence=_STAKE)
    assert body.startswith(_STAKE)
    assert "Hi Dana," in body
    # Footer carries the per-recipient one-click opt-out (2026-07-03): the
    # /unsubscribe page suppresses ?e=<address> directly.
    assert body.rstrip().endswith("https://hustleforge.tech/unsubscribe?e=a%40x.com")


def test_compose_body_llm_greeting_guard_falls_back(monkeypatch):
    """A rewrite that greets the recipient as the SENDER ('Hi Alex') is a real
    observed failure. The greeting guard must discard it and fall back to the
    template's correct greeting."""
    import backend.common.llm_client as llm_client

    def _bad(*a, **k):
        return ("Hi Alex, thanks for reading this rewritten pitch.", {})

    monkeypatch.setattr(llm_client, "anthropic_messages", _bad)
    body = compose_body(_contact(), _cfg(use_llm=True), stake_sentence=_STAKE)
    assert "Hi Dana," in body  # template greeting preserved
    assert "Hi Alex" not in body  # the mis-greeting never ships


def test_compose_body_llm_keeps_correct_greeting(monkeypatch):
    """A rewrite that greets correctly ('Hi Dana') is kept."""
    import backend.common.llm_client as llm_client

    def _good(*a, **k):
        return (f"{_STAKE}\n\nHi Dana,\n\nA sharper, warmer rewrite here.", {})

    monkeypatch.setattr(llm_client, "anthropic_messages", _good)
    body = compose_body(_contact(), _cfg(use_llm=True), stake_sentence=_STAKE)
    assert "A sharper, warmer rewrite here." in body  # the rewrite was used
    assert "Hi Dana," in body


def test_load_suppression_handles_bare_and_json_lines(tmp_path):
    f = tmp_path / "emailed.txt"
    f.write_text(
        'bare@x.com\n{"email": "json@x.com", "status": "sent"}\n\n',
        encoding="utf-8",
    )
    got = load_suppression(str(f))
    assert got == {"bare@x.com", "json@x.com"}


def test_load_suppression_missing_file_is_empty():
    assert load_suppression("/no/such/file/emailed.txt") == set()
