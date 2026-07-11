"""Mandatory simulation stage (HOTL Tranche 5, deliverable 1).

Covers:
  * simulation.simulate_action dry-run predictions + durable registry
  * simulation.gate_dispatch refusal semantics
  * the gateway /dispatch gate refusing an un-simulated external-effect action
  * autonomy.run_cycle SIMULATE phase
  * cash_engine stage dry-run paths
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# simulate_action + registry
# ---------------------------------------------------------------------------

def test_simulate_send_predicts_deliverable(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_SIMULATION_LEDGER_PATH", str(tmp_path / "sim.jsonl"))
    from backend.common import simulation

    res = simulation.simulate_action(
        "send_message", decision_id="d1", target="outreach",
        payload={"to": "owner@acme.com", "subject": "hi", "body": "hello"},
    )
    assert res.would_succeed is True
    assert res.predicted_cost_usd > 0
    assert res.predicted_effect["recipient_present"] is True
    # Recorded + retrievable by decision_id.
    assert simulation.has("d1") is True
    assert simulation.get("d1")["action"] == "send_message"


def test_simulate_send_without_recipient_predicts_no_send(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_SIMULATION_LEDGER_PATH", str(tmp_path / "sim.jsonl"))
    from backend.common import simulation

    res = simulation.simulate_action(
        "send_message", decision_id="d2", payload={"subject": "hi", "body": "x"},
    )
    assert res.would_succeed is False
    assert res.predicted_cost_usd == 0.0


def test_simulate_unmodelled_action_is_unknown_but_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_SIMULATION_LEDGER_PATH", str(tmp_path / "sim.jsonl"))
    from backend.common import simulation

    # An external-effect action with no dedicated simulator still records.
    res = simulation.simulate_action("publish", decision_id="d3", payload={})
    assert res.would_succeed is False  # nothing to publish
    assert simulation.has("d3") is True


def test_has_is_fail_closed_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_SIMULATION_LEDGER_PATH", str(tmp_path / "sim.jsonl"))
    from backend.common import simulation
    assert simulation.has("never-simulated") is False


# ---------------------------------------------------------------------------
# gate_dispatch
# ---------------------------------------------------------------------------

def test_gate_passes_non_external_effect(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_SIMULATION_LEDGER_PATH", str(tmp_path / "sim.jsonl"))
    from backend.common import simulation
    # A non-external action is never gated (returns None, no raise).
    assert simulation.gate_dispatch("score", decision_id="d") is None


def test_gate_refuses_missing_simulation(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_SIMULATION_LEDGER_PATH", str(tmp_path / "sim.jsonl"))
    from backend.common import simulation
    with pytest.raises(simulation.SimulationRequired) as ei:
        simulation.gate_dispatch("send_message", decision_id="dX")
    assert ei.value.reason == "missing_simulation"


def test_gate_passes_with_recorded_simulation(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_SIMULATION_LEDGER_PATH", str(tmp_path / "sim.jsonl"))
    from backend.common import simulation
    simulation.simulate_action(
        "send_message", decision_id="dY",
        payload={"to": "a@b.com", "body": "hi"},
    )
    sim = simulation.gate_dispatch("send_message", decision_id="dY")
    assert sim is not None and sim["decision_id"] == "dY"


def test_gate_high_risk_refuses_failed_simulation(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_SIMULATION_LEDGER_PATH", str(tmp_path / "sim.jsonl"))
    from backend.common import simulation
    # Simulation predicts failure (no recipient) — HIGH risk must refuse.
    simulation.simulate_action("send_message", decision_id="dZ", payload={})
    with pytest.raises(simulation.SimulationRequired) as ei:
        simulation.gate_dispatch("send_message", decision_id="dZ", risk_level="high")
    assert ei.value.reason == "simulation_failed"


# ---------------------------------------------------------------------------
# gateway /dispatch gate
# ---------------------------------------------------------------------------

def _gateway_client(monkeypatch):
    monkeypatch.setenv("SAMUS_SHARED_HMAC_KEY", "test-hmac-key")
    from backend.common.settings import reload_settings
    reload_settings()
    from fastapi.testclient import TestClient
    from backend.gateway import sqs_dispatch
    from backend.gateway.app import create_app
    sqs_dispatch.reload_queue_urls()
    return TestClient(create_app())


def test_dispatch_refuses_unsimulated_external_action(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_SIMULATION_LEDGER_PATH", str(tmp_path / "sim.jsonl"))
    client = _gateway_client(monkeypatch)
    resp = client.post("/dispatch/outreach", json={
        "task_id": "t-ext",
        "payload": {"to": "x@y.com"},
        "metadata": {"action": "send_message", "decision_id": "dGate"},
    })
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "simulation_required"
    assert detail["reason"] == "missing_simulation"


def test_dispatch_allows_internal_action(tmp_path, monkeypatch):
    """A non-external action dispatches normally (gate is a no-op)."""
    monkeypatch.setenv("SAMUS_SIMULATION_LEDGER_PATH", str(tmp_path / "sim.jsonl"))
    client = _gateway_client(monkeypatch)
    from backend.gateway import sqs_dispatch
    monkeypatch.setitem(sqs_dispatch.QUEUE_URLS, "leadgen", "https://sqs.example/leadgen")
    fake_sqs = MagicMock()
    fake_sqs.send_message = MagicMock(return_value={"MessageId": "m1"})
    monkeypatch.setattr(sqs_dispatch, "sqs_client", lambda: fake_sqs)

    resp = client.post("/dispatch/leadgen", json={
        "task_id": "t-int",
        "payload": {},
        "metadata": {"action": "score"},
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["queued"] is True


def test_dispatch_allows_simulated_external_action(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_SIMULATION_LEDGER_PATH", str(tmp_path / "sim.jsonl"))
    from backend.common import simulation
    # Pre-record a passing simulation for this decision_id.
    simulation.simulate_action(
        "send_message", decision_id="dOK", payload={"to": "a@b.com", "body": "hi"},
    )
    client = _gateway_client(monkeypatch)
    from backend.gateway import sqs_dispatch
    monkeypatch.setitem(sqs_dispatch.QUEUE_URLS, "outreach", "https://sqs.example/outreach")
    fake_sqs = MagicMock()
    fake_sqs.send_message = MagicMock(return_value={"MessageId": "m2"})
    monkeypatch.setattr(sqs_dispatch, "sqs_client", lambda: fake_sqs)

    resp = client.post("/dispatch/outreach", json={
        "task_id": "t-ok",
        "payload": {"to": "a@b.com"},
        "metadata": {"action": "send_message", "decision_id": "dOK"},
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["queued"] is True


# ---------------------------------------------------------------------------
# autonomy SIMULATE phase
# ---------------------------------------------------------------------------

def test_run_cycle_simulates_external_effect_step(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_SIMULATION_LEDGER_PATH", str(tmp_path / "sim.jsonl"))
    from backend.common import autonomy, simulation

    # An objective that routes to a step whose action we override to external.
    out = autonomy.run_cycle("task-cy", "publish the launch", inputs={"url": "x", "content": "y"})
    assert "simulation" in out
    # Craft a plan with an explicit external-effect step and simulate directly.
    plan = autonomy.Plan(steps=[
        autonomy.PlanStep(name="s1", target="outreach", action="send_message",
                          metadata={"payload": {"to": "a@b.com", "body": "hi"}}),
        autonomy.PlanStep(name="s2", target="leadgen", action="execute"),
    ])
    sim = autonomy.simulate(plan, decision_id="task-cy2")
    assert sim["external_effect_steps"] == 1
    assert sim["simulated"][0]["step"] == "s1"
    assert sim["simulated"][0]["result"]["would_succeed"] is True
    assert simulation.has("task-cy2") is True


# ---------------------------------------------------------------------------
# cash_engine stage dry-run
# ---------------------------------------------------------------------------

def test_outreach_stage_dry_run_predicts_without_sending():
    from backend.cash_engine.stages import StageContext, _outreach_stage

    class _Prospect:
        email = "owner@acme.com"
        company_name = "Acme"

    class _Opp:
        opportunity_id = "opp1"
        stage = "qualified"

    class _State:
        prospect_id = "pr1"
        trigger_source = "manual_review"

    ctx = StageContext(
        state=_State(), opportunity=_Opp(), prospect=_Prospect(),
        stake_sentence="stake", crm=MagicMock(), dry_run=True,
    )
    res = _outreach_stage(ctx)
    assert res.ok is True
    assert res.detail["dry_run"] is True
    assert res.detail["would_send"] is True
    # No CRM artifact / call-state writes happened in dry-run.
    ctx.crm.create_artifact.assert_not_called()


def test_deliver_stage_dry_run_predicts_without_build():
    from backend.cash_engine.stages import StageContext, _deliver_stage

    class _Opp:
        opportunity_id = "opp2"
        stage = "closed_won"

    class _State:
        prospect_id = "pr2"

    ctx = StageContext(
        state=_State(), opportunity=_Opp(), prospect=MagicMock(),
        stake_sentence="stake", crm=MagicMock(), dry_run=True,
    )
    res = _deliver_stage(ctx)
    assert res.ok is True
    assert res.detail["dry_run"] is True
    assert res.detail["would_open_build"] is True
