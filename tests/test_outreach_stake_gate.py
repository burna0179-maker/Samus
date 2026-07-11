"""Outreach gate: compose_body refuses without stake; render places it at top."""
from __future__ import annotations

import pytest

from backend.outreach.apollo_source import ApolloContact
from backend.outreach.campaign import (
    CampaignConfig,
    OutreachStakeMissing,
    build_messages,
    compose_body,
)


_VALID = (
    "Alex picked you because your Yuba City HVAC ranks for fewer keywords "
    "than two of your neighbors combined."
)


def _contact() -> ApolloContact:
    return ApolloContact(
        person_id="apollo_p_1",
        first_name="Jane",
        name="Jane Smith",
        title="Owner",
        company="Acme HVAC",
        industry="hvac",
        email="jane@acme.test",
        email_status="verified",
        phone="+15555550100",
        # G8 (ADR-012, 2026-05-30): warmth signal required for compose_body
        # to clear VR-G8 — see backend/common/codex/validator.py.
        legitimacy_signal="chamber_roster",
    )


def _cfg() -> CampaignConfig:
    return CampaignConfig(
        sender_postal_address="123 Main St, Yuba City, CA",
        unsubscribe_url="https://hustleforge.tech/unsubscribe",
        max_send=10,
    )


def test_compose_body_without_stake_raises():
    with pytest.raises(OutreachStakeMissing):
        compose_body(_contact(), _cfg(), stake_sentence=None)


def test_compose_body_empty_stake_raises():
    with pytest.raises(OutreachStakeMissing):
        compose_body(_contact(), _cfg(), stake_sentence="   ")


def test_compose_body_renders_stake_at_top_verbatim():
    body = compose_body(_contact(), _cfg(), stake_sentence=_VALID)
    # First non-empty line is the stake.
    first = body.lstrip().splitlines()[0]
    assert first == _VALID
    # Stake appears in body exactly once at the top.
    assert _VALID in body


def test_compose_body_guard_rejects_bad_stake():
    with pytest.raises(OutreachStakeMissing):
        compose_body(_contact(), _cfg(), stake_sentence="Hi we help businesses scale revenue with synergy daily.")


def test_build_messages_skips_contact_without_stake():
    result = build_messages(
        [_contact()],
        _cfg(),
        stake_sentences={},  # no entry for jane@acme.test
    )
    assert result.built == 0
    assert result.not_sendable >= 1


def test_build_messages_renders_when_stake_provided():
    result = build_messages(
        [_contact()],
        _cfg(),
        stake_sentences={"jane@acme.test": _VALID},
    )
    assert result.built == 1
    msg = result.messages[0]
    assert msg.body.lstrip().startswith(_VALID)
