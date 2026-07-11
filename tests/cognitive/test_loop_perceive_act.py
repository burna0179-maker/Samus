"""Phase E — PERCEIVE (read-only domain perception) + ACT (propose-only).

Covers the build-plan Phase-E test bullets:
  * PERCEIVE tolerates an UNAVAILABLE / RAISING domain provider — it returns a
    partial perception (the raising slice degraded to an error stub, the rest
    surfaced) and NEVER raises; the cycle stays ok.
  * PERCEIVE with a STUB provider surfaces the expected perception keys onto
    the PERCEIVE stage output.
  * ACT writes EXACTLY ONE proposal row to a temp ``state_path`` AND invokes NO
    effector — asserted via spies that the cash_engine gate / review_opportunity
    / outreach-send / finance entrypoints are NEVER called (and no network).
  * DORMANCY: optional providers default None => unchanged behaviour; ACT is
    flag-gated and a pure no-op (writes nothing) by default.

Isolation: every test injects STUB domain providers + a spy/temp proposal sink.
NOTHING here touches the real CRM / DDB / finance / LM Studio backends.
"""

from __future__ import annotations

import asyncio
import json


from backend.cognitive.cognitive_loop import CognitiveLoop
from backend.cognitive.cycle_models import CycleInput, CycleResult, StageStatus


def _run(loop, inp) -> CycleResult:
    return asyncio.run(loop.cycle(inp))


def _arm_act(monkeypatch, state_root):
    """Arm the propose-only ACT flag + point state_path at a temp root."""
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(state_root))
    monkeypatch.setenv("SAMUS_COGNITIVE_ACT_PROPOSALS_ENABLED", "true")
    from backend.common.settings import reload_settings

    reload_settings()


def _disarm_act(monkeypatch):
    monkeypatch.setenv("SAMUS_COGNITIVE_ACT_PROPOSALS_ENABLED", "false")
    from backend.common.settings import reload_settings

    reload_settings()


# ---------------------------------------------------------------------------
# Stub domain providers (NO real CRM / DDB / finance — canned dicts only)
# ---------------------------------------------------------------------------
class _StubDomainProvider:
    """A healthy stub provider surfacing every perception slice."""

    def __init__(self):
        self.calls = []

    def follow_ups_due(self):
        self.calls.append("follow_ups_due")
        return {"count": 3, "ddb_error": None}

    def opportunities_pending_stake(self):
        self.calls.append("opportunities_pending_stake")
        return {"count": 2}

    def open_operator_tasks(self):
        self.calls.append("open_operator_tasks")
        return {"count": 1, "ddb_error": None}

    def decay_risk(self, *, threshold=0.6):
        self.calls.append(("decay_risk", threshold))
        return {"assessed": 5, "crossing": 1, "worst_risk": 0.82, "threshold": threshold}

    def finance_runway(self):
        self.calls.append("finance_runway")
        return {"days_of_runway": 41.0, "alert_triggered": False, "available_balance_usd": 1200.0}

    def finance_actions(self):
        self.calls.append("finance_actions")
        return {"open_total": 4, "overdue_count": 1, "due_today_count": 0}

    def cash_distress(self):
        self.calls.append("cash_distress")
        return {"cash_distress": "ok", "distress_reasons": []}


class _PartlyRaisingProvider(_StubDomainProvider):
    """A provider whose finance_runway slice raises (backend unavailable)."""

    def finance_runway(self):
        raise RuntimeError("stripe unreachable / finance backend down")


class _AllRaisingProvider:
    """Every slice raises (DDB + finance both down). PERCEIVE must not raise."""

    def follow_ups_due(self):
        raise RuntimeError("ddb down")

    def opportunities_pending_stake(self):
        raise RuntimeError("ddb down")

    def open_operator_tasks(self):
        raise RuntimeError("ddb down")

    def decay_risk(self, *, threshold=0.6):
        raise RuntimeError("ddb down")

    def finance_runway(self):
        raise RuntimeError("stripe down")

    def finance_actions(self):
        raise RuntimeError("registry unreadable")

    def cash_distress(self):
        raise RuntimeError("registry unreadable")


class _RecordingSink:
    """A spy proposal sink — captures appended rows instead of writing disk."""

    def __init__(self):
        self.rows = []

    def append(self, record):
        self.rows.append(record)


# ===========================================================================
# PERCEIVE — read-only perception, partial on failure, never raises
# ===========================================================================
def test_perceive_falls_back_to_persona_only_without_domain():
    # No domain provider => perception is empty; behaviour unchanged from prior
    # phase (PERCEIVE OK, domain=False).
    res = _run(CognitiveLoop(), CycleInput(user_text="hi"))
    assert res.ok is True
    by_name = {s.name: s for s in res.stages}
    assert by_name["PERCEIVE"].status is StageStatus.OK
    assert by_name["PERCEIVE"].output.get("domain") is False
    assert by_name["PERCEIVE"].output.get("domain_available") == []


def test_perceive_surfaces_expected_keys_with_stub_provider():
    provider = _StubDomainProvider()
    res = _run(CognitiveLoop(domain=provider), CycleInput(user_text="status?"))
    assert res.ok is True
    by_name = {s.name: s for s in res.stages}
    perceive = by_name["PERCEIVE"]
    assert perceive.status is StageStatus.OK
    assert perceive.output.get("domain") is True
    # All seven slices surfaced as available; none degraded.
    available = set(perceive.output.get("domain_available", []))
    assert available == {
        "follow_ups_due",
        "opportunities_pending_stake",
        "open_operator_tasks",
        "decay_risk",
        "finance_runway",
        "finance_actions",
        "cash_distress",
    }
    assert perceive.output.get("domain_degraded") == []
    # The provider was actually consulted for each slice.
    assert "follow_ups_due" in provider.calls
    assert any(isinstance(c, tuple) and c[0] == "decay_risk" for c in provider.calls)


def test_perceive_tolerates_partly_raising_provider_partial_perception():
    # One slice raises (finance backend down): PERCEIVE degrades that slice,
    # surfaces the rest, never raises, cycle stays ok.
    res = _run(CognitiveLoop(domain=_PartlyRaisingProvider()), CycleInput(user_text="hi"))
    assert isinstance(res, CycleResult)  # did not raise
    assert res.ok is True
    by_name = {s.name: s for s in res.stages}
    perceive = by_name["PERCEIVE"]
    assert perceive.status is StageStatus.OK
    # finance_runway degraded; the other six available.
    assert "finance_runway" in perceive.output.get("domain_degraded", [])
    assert "follow_ups_due" in perceive.output.get("domain_available", [])


def test_perceive_tolerates_fully_unavailable_provider():
    # Every slice raises: perception is all-error stubs, PERCEIVE still OK, no raise.
    res = _run(CognitiveLoop(domain=_AllRaisingProvider()), CycleInput(user_text="hi"))
    assert isinstance(res, CycleResult)
    assert res.ok is True
    by_name = {s.name: s for s in res.stages}
    perceive = by_name["PERCEIVE"]
    assert perceive.status is StageStatus.OK
    assert perceive.output.get("domain") is True
    # Nothing came back available; all degraded.
    assert perceive.output.get("domain_available") == []
    assert len(perceive.output.get("domain_degraded", [])) == 7


def test_perceive_raising_provider_object_does_not_break_cycle():
    # Defensive: even a provider whose attribute access itself explodes is
    # absorbed (gather_perception is wrapped) — the cycle never raises.
    class _Hostile:
        def __getattr__(self, _name):
            raise RuntimeError("hostile provider")

    res = _run(CognitiveLoop(domain=_Hostile()), CycleInput(user_text="hi"))
    assert isinstance(res, CycleResult)
    # PERCEIVE may degrade to SKIPPED but the cycle did not raise.
    assert any(s.name == "PERCEIVE" for s in res.stages)


# ===========================================================================
# ACT — propose-only: writes exactly one proposal, invokes NO effector
# ===========================================================================
def test_act_writes_exactly_one_proposal_to_state_path(tmp_path, monkeypatch):
    _arm_act(monkeypatch, tmp_path / "state")
    provider = _StubDomainProvider()
    loop = CognitiveLoop(domain=provider)
    res = _run(loop, CycleInput(user_text="what next?", plan_token="tok-e", channel="chat"))
    assert res.ok is True

    proposals = tmp_path / "state" / "cognition" / "proposals.jsonl"
    assert proposals.exists(), "ACT must append to state_path('cognition','proposals.jsonl')"
    lines = [ln for ln in proposals.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1, "ACT must write EXACTLY ONE proposal row per cycle"
    rec = json.loads(lines[0])
    assert rec["kind"] == "cognition_proposal"
    assert rec["status"] == "proposed"
    assert rec["actioned"] is False
    assert rec["plan_token"] == "tok-e"
    assert "PROPOSAL ONLY" in rec["note"]
    # Intent derived from perception (2 pending-stake deals -> stake_then_outreach).
    assert rec["intent"]["type"] == "stake_then_outreach"

    # ACT stage recorded the proposal (proposed=True), actuated stays False.
    by_name = {s.name: s for s in res.stages}
    assert by_name["ACT"].status is StageStatus.OK
    assert by_name["ACT"].output.get("proposed") is True
    assert by_name["ACT"].output.get("actuated") is False


def test_act_uses_injected_sink_spy(tmp_path, monkeypatch):
    _arm_act(monkeypatch, tmp_path / "state")
    sink = _RecordingSink()
    loop = CognitiveLoop(domain=_StubDomainProvider(), proposal_sink=sink)
    _run(loop, CycleInput(user_text="hi", plan_token="t"))
    # Exactly one proposal captured by the spy.
    assert len(sink.rows) == 1
    assert sink.rows[0]["status"] == "proposed"
    # The default disk ledger was NOT written (sink intercepted it).
    assert not (tmp_path / "state" / "cognition" / "proposals.jsonl").exists()


def test_act_invokes_no_effector_or_gate(tmp_path, monkeypatch):
    """The load-bearing safety property: ACT records a proposal and calls NONE
    of the effector / gate / send / finance entrypoints, and no network."""
    _arm_act(monkeypatch, tmp_path / "state")

    import backend.cash_engine.gate as gate_mod
    import backend.cash_engine.service as cash_service_mod
    import backend.cash_engine.stages as stages_mod
    import backend.crm.service as crm_mod
    import backend.finance.service as finance_mod
    import backend.common.http_client as http_mod

    called: list[str] = []

    def _spy(name):
        def _f(*_a, **_k):
            called.append(name)
            raise AssertionError(f"effector {name} must NOT be called by ACT")

        return _f

    # cash_engine front-door gate + review_opportunity entrypoint + live-send
    # double-gate stage (all real symbols — raising=True so a rename here fails
    # the test loudly rather than letting it pass vacuously).
    monkeypatch.setattr(gate_mod, "evaluate_gate", _spy("cash_engine.evaluate_gate"))
    monkeypatch.setattr(
        cash_service_mod, "review_opportunity", _spy("cash_engine.review_opportunity")
    )
    monkeypatch.setattr(stages_mod, "_outreach_stage", _spy("cash_engine._outreach_stage"))
    # CRM mutating entrypoints.
    monkeypatch.setattr(crm_mod, "advance_opportunity", _spy("crm.advance_opportunity"))
    monkeypatch.setattr(crm_mod, "create_opportunity", _spy("crm.create_opportunity"))
    # finance mutation (webhook handler).
    monkeypatch.setattr(finance_mod, "handle_stripe_webhook", _spy("finance.handle_stripe_webhook"))
    # any signed network call.
    monkeypatch.setattr(http_mod, "signed_post_json_sync", _spy("http.signed_post_json_sync"))

    sink = _RecordingSink()
    loop = CognitiveLoop(domain=_StubDomainProvider(), proposal_sink=sink)
    res = _run(loop, CycleInput(user_text="should I act?", plan_token="t"))

    # The cycle completed, recorded a proposal, and called NO effector.
    assert res.ok is True
    assert called == [], f"ACT illegally invoked effector(s): {called}"
    assert len(sink.rows) == 1
    assert sink.rows[0]["actioned"] is False


# ===========================================================================
# DORMANCY — disabled ACT is a pure no-op (writes nothing)
# ===========================================================================
def test_act_disabled_is_pure_noop_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path / "state"))
    _disarm_act(monkeypatch)
    sink = _RecordingSink()
    loop = CognitiveLoop(domain=_StubDomainProvider(), proposal_sink=sink)
    res = _run(loop, CycleInput(user_text="hi", plan_token="t"))
    assert res.ok is True
    by_name = {s.name: s for s in res.stages}
    # ACT records an inert no-op: nothing proposed, nothing actuated, nothing written.
    assert by_name["ACT"].status is StageStatus.OK
    assert by_name["ACT"].output.get("proposed") is False
    assert by_name["ACT"].output.get("actuated") is False
    assert sink.rows == []
    assert not (tmp_path / "state" / "cognition" / "proposals.jsonl").exists()


def test_bare_loop_unchanged_no_proposal_written(tmp_path, monkeypatch):
    # The bare loop (no domain, no sink) with ACT disabled writes nothing and
    # behaves exactly as the prior phase (echo reply, all stages OK).
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path / "state"))
    _disarm_act(monkeypatch)
    res = _run(CognitiveLoop(), CycleInput(user_text="echo me"))
    assert res.ok is True
    assert res.reply == "echo me"
    assert not (tmp_path / "state" / "cognition").exists()


def test_act_armed_but_no_domain_still_writes_observe_proposal(tmp_path, monkeypatch):
    # ACT armed with NO domain provider: perception is empty, so the proposal's
    # intent is the neutral "observe" — still propose-only, still one row.
    _arm_act(monkeypatch, tmp_path / "state")
    sink = _RecordingSink()
    res = _run(CognitiveLoop(proposal_sink=sink), CycleInput(user_text="hi", plan_token="t"))
    assert res.ok is True
    assert len(sink.rows) == 1
    assert sink.rows[0]["intent"]["type"] == "observe"
