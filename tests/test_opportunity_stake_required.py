"""Pydantic validation: CreateOpportunityRequest stake_sentence behavior."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.crm.models import CreateOpportunityRequest, Opportunity


_VALID = (
    "Alex picked you because your Yuba City HVAC ranks for fewer keywords "
    "than two of your neighbors combined."
)


def test_create_opportunity_without_stake_is_allowed():
    # stake_sentence is optional at create time; outreach gate handles enforcement.
    req = CreateOpportunityRequest(prospect_id="pr_x")
    assert req.stake_sentence == ""


def test_create_opportunity_with_valid_stake_passes():
    req = CreateOpportunityRequest(
        prospect_id="pr_x",
        stake_sentence=_VALID,
        stake_sentence_authored_by="alex",
    )
    assert req.stake_sentence == _VALID


def test_create_opportunity_banned_phrase_rejected():
    bad = (
        "Hello Acme Plumbing, we help businesses just like yours close more "
        "deals every quarter."
    )
    with pytest.raises(ValidationError):
        CreateOpportunityRequest(prospect_id="pr_x", stake_sentence=bad)


def test_create_opportunity_too_short_rejected():
    with pytest.raises(ValidationError):
        CreateOpportunityRequest(prospect_id="pr_x", stake_sentence="Too short.")


def test_opportunity_model_validates_stake_sentence():
    bad = "alex picked you because we work with hvac shops in your area daily."
    with pytest.raises(ValidationError):
        Opportunity(opportunity_id="op_x", stake_sentence=bad)


def test_opportunity_model_accepts_empty_stake():
    opp = Opportunity(opportunity_id="op_x")
    assert opp.stake_sentence == ""
    assert opp.stake_sentence_authored_by == ""
