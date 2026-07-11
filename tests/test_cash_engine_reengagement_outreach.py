"""Cash engine outreach stage — promotional template routing for re-engagement.

The soft-no re-engagement sweep (backend.crm.reengagement_sweep) fires a
``RevenueTriggerRequest`` with ``trigger_source="reengagement"``. That source
flows through ``review_opportunity`` -> the queued payload -> into the
CashEngineState the worker hydrates. The outreach stage in
``backend.cash_engine.stages`` reads ``state.trigger_source`` and MUST route
through ``compose_reengagement_packet`` (promotional template) instead of
``compose_initial_packet`` (cold opener), so a resurfaced prospect doesn't
get a duplicate of the cold pitch they already declined.

This file pins that routing contract.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from backend.cash_engine import stages as stages_mod
from backend.cash_engine.stages import StageContext, _outreach_stage
from backend.cash_engine.state import CashEngineState
from backend.crm.models import Opportunity, Prospect


VALID_STAKE = (
    "Alex picked you because Acme Plumbing has the worst homepage I have "
    "seen and it is costing you real calls every single week."
)


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))


def _opp() -> Opportunity:
    return Opportunity(
        opportunity_id="op-1",
        prospect_id="pr-1",
        stage="qualified",
        stake_sentence=VALID_STAKE,
    )


def _prospect() -> Prospect:
    return Prospect(
        prospect_id="pr-1",
        company_name="Acme Plumbing",
        website_url="https://acme.test",
        industry="plumbing",
        zipcode="95901",
        phone="555-0100",
    )


class _FakeCrm:
    def __init__(self) -> None:
        self.artifacts: list = []
        self.call_states: list = []

    def get_call_state(self, pid: str):  # noqa: ARG002
        return None

    def upsert_call_state(self, state) -> bool:
        self.call_states.append(state)
        return True

    def create_artifact(self, req):
        self.artifacts.append(req)
        return SimpleNamespace(artifact_id=f"art-{len(self.artifacts)}")


def _ctx(*, trigger_source: str) -> tuple[StageContext, _FakeCrm]:
    state = CashEngineState(
        opportunity_id="op-1",
        prospect_id="pr-1",
        task_id="ce-test",
        trigger_source=trigger_source,
        stage="outreach",
    )
    crm = _FakeCrm()
    return StageContext(
        state=state,
        opportunity=_opp(),
        prospect=_prospect(),
        stake_sentence=VALID_STAKE,
        crm=crm,
    ), crm


def _install_compose_spies(monkeypatch):
    """Replace BOTH composers in the cash_engine_entry module with spies.

    The composer the stage imports is in ``backend.outreach.cash_engine_entry``;
    it's imported inside ``_outreach_stage`` (lazy), so a monkeypatch on the
    module attribute is what the stage will see at call time.
    """
    import backend.outreach.cash_engine_entry as entry

    calls: dict[str, list] = {"initial": [], "reengagement": []}

    def initial(*, prospect, stake_sentence, legitimacy_signal, cfg=None):
        calls["initial"].append(
            {
                "prospect_id": getattr(prospect, "prospect_id", ""),
                "legitimacy_signal": legitimacy_signal,
            }
        )
        return {
            "subject": "Quick question about Acme Plumbing",
            "body": f"{stake_sentence}\n\nHi there,\n\nWorth a quick look?",
            "to": "owner@acme.test",
        }

    def reengagement(*, prospect, stake_sentence, legitimacy_signal):
        calls["reengagement"].append(
            {
                "prospect_id": getattr(prospect, "prospect_id", ""),
                "legitimacy_signal": legitimacy_signal,
            }
        )
        return {
            "subject": "Circling back, Acme Plumbing — a different offer",
            "body": (
                f"{stake_sentence}\n\nHi there,\n\n"
                "We spoke a while back ... promotional package ..."
            ),
            "to": "owner@acme.test",
        }

    monkeypatch.setattr(entry, "compose_initial_packet", initial)
    monkeypatch.setattr(entry, "compose_reengagement_packet", reengagement)
    return calls


def _shortcircuit_codex(monkeypatch):
    """Force ``codex_gate`` to allow so the stage doesn't try to call the
    real Codex registry over the contact_artifacts capability."""
    from backend.common.codex.models import Verdict

    monkeypatch.setattr(
        stages_mod,
        "codex_gate",
        lambda **_: Verdict(allowed=True),
    )


def _disable_recipient_index(monkeypatch):
    """Stub the recipient_index write so the test doesn't touch the real
    file/DDB; the stage best-effort-swallows exceptions anyway."""
    import backend.common.recipient_index as ri

    monkeypatch.setattr(ri, "record_recipient", lambda **_: None)


def test_reengagement_trigger_routes_through_promotional_template(monkeypatch):
    """``state.trigger_source == "reengagement"`` -> the outreach stage
    calls ``compose_reengagement_packet`` and NOT ``compose_initial_packet``.
    """
    spies = _install_compose_spies(monkeypatch)
    _shortcircuit_codex(monkeypatch)
    _disable_recipient_index(monkeypatch)

    ctx, _crm = _ctx(trigger_source="reengagement")
    result = _outreach_stage(ctx)

    assert result.ok is True
    assert len(spies["reengagement"]) == 1
    assert spies["initial"] == []
    assert spies["reengagement"][0]["prospect_id"] == "pr-1"
    assert "existing_opportunity:" in spies["reengagement"][0]["legitimacy_signal"]


def test_default_trigger_keeps_cold_template(monkeypatch):
    """Existing non-reengagement triggers (manual_review / signal_decay /
    macro_market_shift) keep going through ``compose_initial_packet`` — the
    promotional template is OPT-IN, soft-no-only."""
    spies = _install_compose_spies(monkeypatch)
    _shortcircuit_codex(monkeypatch)
    _disable_recipient_index(monkeypatch)

    ctx, _crm = _ctx(trigger_source="manual_review")
    result = _outreach_stage(ctx)

    assert result.ok is True
    assert len(spies["initial"]) == 1
    assert spies["reengagement"] == []


def test_reengagement_live_send_uses_distinct_template_id(monkeypatch):
    """When the live-send flag is on, the OutreachMessageRequest emitted
    by the stage MUST carry ``template_id="cash_engine_reengagement"`` so
    the send-side audit trail reads back as a re-engagement, not a cold.

    Asserts via a spied ``send_message`` that captures the request.
    """
    _install_compose_spies(monkeypatch)
    _shortcircuit_codex(monkeypatch)
    _disable_recipient_index(monkeypatch)

    captured: list[Any] = []

    import backend.outreach.service as outreach_service

    def fake_send(req):
        captured.append(req)
        return {"message_id": "msg-reengagement-1"}

    monkeypatch.setattr(outreach_service, "send_message", fake_send)

    # Arm the live-send gate + a configured sender so the stage takes the
    # send branch; cash_engine_live_send_enabled checks ses_from_email OR
    # sendgrid_from_email.
    monkeypatch.setenv("SAMUS_CASH_ENGINE_LIVE_SEND", "true")
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "samus@hustleforge.test")
    from backend.common.config import reload_settings

    reload_settings()

    try:
        ctx, _crm = _ctx(trigger_source="reengagement")
        result = _outreach_stage(ctx)

        assert result.ok is True
        assert len(captured) == 1
        assert captured[0].template_id == "cash_engine_reengagement"
        assert captured[0].prospect_id == "pr-1"
    finally:
        reload_settings()
