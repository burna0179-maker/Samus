"""The Codex Gate — stake-sentence + check_action, both fail-closed."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.cash_engine import gate as gate_mod
from backend.cash_engine.gate import evaluate_gate
from backend.cash_engine.models import RevenueTriggerRequest
from backend.common.codex.models import Verdict
from backend.crm.models import Opportunity

VALID_STAKE = (
    "Alex picked you because Acme Plumbing has the worst homepage I have "
    "seen and it is costing you real calls every single week."
)


class FakeCRM:
    def __init__(self, opp=None, call_state=None, opps=None):
        self._opp = opp
        self._call_state = call_state
        self._opps = opps or []

    def get_opportunity_for_prospect(self, prospect_id):
        return self._opp

    def get_call_state(self, prospect_id):
        return self._call_state

    def list_opportunities(self, *, limit=50):
        return SimpleNamespace(opportunities=list(self._opps))


def _req(**kw):
    return RevenueTriggerRequest(
        prospect_id=kw.get("prospect_id", "pr-1"),
        trigger_source=kw.get("trigger_source", "manual_review"),
    )


def _opp(stake: str = VALID_STAKE, stage: str = "proposal"):
    return Opportunity(
        opportunity_id="op-1",
        prospect_id="pr-1",
        stage=stage,
        stake_sentence=stake,
    )


def test_staked_opportunity_passes_the_gate():
    outcome = evaluate_gate(_req(), crm=FakeCRM(opp=_opp()))
    assert outcome.allowed is True
    assert outcome.stake_present is True
    assert outcome.opportunity_id == "op-1"
    assert outcome.violated_rule_id is None


def test_missing_stake_sentence_escalates():
    outcome = evaluate_gate(_req(), crm=FakeCRM(opp=_opp(stake="")))
    assert outcome.allowed is False
    assert outcome.stake_present is False
    assert outcome.required_protocol == "stake_sentence"
    assert outcome.opportunity_id == "op-1"


def test_no_opportunity_blocks_on_opportunity_protocol():
    outcome = evaluate_gate(_req(), crm=FakeCRM(opp=None))
    assert outcome.allowed is False
    assert outcome.required_protocol == "opportunity"
    assert outcome.opportunity_id == ""


def test_codex_block_surfaces_rule_and_keeps_stake(monkeypatch):
    blocked = Verdict(
        allowed=False,
        violated_rule_id="VR-G2",
        reason="banned phrase in stake_sentence",
        drafted_adr_path="/tmp/ADR-099.draft.md",
    )
    monkeypatch.setattr(gate_mod, "check_action", lambda action: blocked)
    outcome = evaluate_gate(_req(), crm=FakeCRM(opp=_opp()))
    assert outcome.allowed is False
    assert outcome.stake_present is True            # stake was present...
    assert outcome.violated_rule_id == "VR-G2"      # ...but Codex blocked it
    assert outcome.drafted_adr_path == "/tmp/ADR-099.draft.md"


def test_codex_unavailable_fails_closed(monkeypatch):
    from backend.common.codex.exceptions import CodexUnavailable

    def _boom(action):
        raise CodexUnavailable("registry not loaded")

    monkeypatch.setattr(gate_mod, "check_action", _boom)
    outcome = evaluate_gate(_req(), crm=FakeCRM(opp=_opp()))
    assert outcome.allowed is False
    assert outcome.required_protocol == "codex"
