"""Perception-grounding tests — STATE-AWARE, PROSPECT-TARGETED cognition.

Covers the perception-grounding enhancement to the Samus cognitive loop:

  (a) the REASON prompt CONTAINS the rendered perception state + the concrete
      prospect_id ('p-test-1');
  (b) the propose-only ACT intent CARRIES that prospect_id;
  (c) feeding that proposal to the Phase-G ``proposal_promoter`` builds a
      ``RevenueTriggerRequest`` with prospect_id='p-test-1' and does NOT skip
      with reason 'no_prospect_id';
  (d) fail-closed: a RAISING domain provider degrades perception (no raise);
      a RAISING reasoner HOLDs (no raise);
  (e) propose-only: ACT invokes NO effector (a review_opportunity spy is
      never called from inside the loop).

All collaborators are STUBS — importing this module touches no backend, no
LM Studio, no CRM/finance/Stripe. The loop stays propose-only and dormant.
"""
from __future__ import annotations

import asyncio

from backend.cognitive.cognitive_loop import CognitiveLoop
from backend.cognitive.cycle_models import CycleInput, CycleResult, StageStatus


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------
_PROSPECT = "p-test-1"
_OPP = "opp-test-9"


class _StubDomainProvider:
    """A read-only domain provider returning a canned perception that includes
    a SPECIFIC opportunity pending stake with prospect_id='p-test-1'."""

    def follow_ups_due(self) -> dict:
        return {"count": 2}

    def opportunities_pending_stake(self) -> dict:
        return {"count": 3}

    def open_operator_tasks(self) -> dict:
        return {"count": 1}

    def decay_risk(self, *, threshold: float = 0.6) -> dict:
        return {"assessed": 3, "crossing": 1, "worst_risk": 0.71, "threshold": threshold}

    def finance_runway(self) -> dict:
        return {"days_of_runway": 18, "alert_triggered": False, "available_balance_usd": 9000.0}

    def finance_actions(self) -> dict:
        return {"open_total": 4, "overdue_count": 1, "due_today_count": 0}

    def cash_distress(self) -> dict:
        return {"cash_distress": "ok", "distress_reasons": []}

    def top_pending_stake(self) -> dict:
        return {
            "prospect_id": _PROSPECT,
            "opportunity_id": _OPP,
            "name": "Acme Co retainer",
            "stage": "proposal",
            "deal_size_usd": 4200.0,
        }

    def top_follow_up(self) -> dict:
        return {
            "prospect_id": "p-follow-2",
            "company": "Beta LLC",
            "channel": "email",
            "follow_up_on": "2026-06-24",
            "days_waiting": 5,
        }


class _RaisingDomainProvider:
    """Every slice raises — perception must degrade, never propagate."""

    def follow_ups_due(self) -> dict:
        raise RuntimeError("ddb down")

    def opportunities_pending_stake(self) -> dict:
        raise RuntimeError("ddb down")

    def open_operator_tasks(self) -> dict:
        raise RuntimeError("ddb down")

    def decay_risk(self, *, threshold: float = 0.6) -> dict:
        raise RuntimeError("ddb down")

    def finance_runway(self) -> dict:
        raise RuntimeError("stripe down")

    def finance_actions(self) -> dict:
        raise RuntimeError("stripe down")

    def cash_distress(self) -> dict:
        raise RuntimeError("stripe down")

    def top_pending_stake(self) -> dict:
        raise RuntimeError("ddb down")

    def top_follow_up(self) -> dict:
        raise RuntimeError("ddb down")


class _EchoReasoner:
    """A stub reasoner that ECHOES the prompt it was handed (so a test can
    assert what the loop put into the prompt)."""

    def __init__(self) -> None:
        self.last_prompt = None

    def generate(self, *, system="", user_text="", channel="", plan_token="", metadata=None) -> str:
        self.last_prompt = user_text
        return user_text


class _RaisingReasoner:
    def generate(self, *, system="", user_text="", channel="", plan_token="", metadata=None) -> str:
        raise RuntimeError("lm_studio_transport_error: timed out")


class _SpySink:
    """A proposal sink that captures the proposals ACT appends."""

    def __init__(self) -> None:
        self.proposals = []

    def append(self, row: dict) -> None:
        self.proposals.append(row)


class _ReviewSpy:
    """A review_opportunity spy: records calls; never invoked from the loop."""

    def __init__(self) -> None:
        self.calls = []

    def __call__(self, req):
        self.calls.append(req)

        class _Result:
            status = "enqueued"
            reason = "ok"
            task_id = "task-xyz"
            prospect_id = getattr(req, "prospect_id", None)
            required_protocol = None
            violated_rule_id = None

        return _Result()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _run(loop, inp) -> CycleResult:
    return asyncio.run(loop.cycle(inp))


def _arm_act(monkeypatch):
    """Arm the propose-only ACT flag for the duration of a test."""
    import backend.cognitive.cognitive_loop as cl

    monkeypatch.setattr(cl.CognitiveLoop, "_act_proposals_enabled", staticmethod(lambda: True))


# ---------------------------------------------------------------------------
# (a) REASON prompt is grounded + names the concrete prospect_id
# ---------------------------------------------------------------------------
def test_reason_prompt_contains_perception_state_and_prospect_id():
    reasoner = _EchoReasoner()
    loop = CognitiveLoop(domain=_StubDomainProvider(), reasoner=reasoner)
    res = _run(loop, CycleInput(user_text="", channel="cognition_tick", plan_token="tok-pg"))

    prompt = reasoner.last_prompt
    assert prompt, "REASON prompt must be non-empty"
    # The grounded context block + the concrete top opportunity's prospect_id.
    assert "Current business state" in prompt
    assert _PROSPECT in prompt
    assert "Acme Co retainer" in prompt
    # The directive question (cadence path: no caller user_text).
    assert "highest-leverage" in prompt
    # The reply echoes the prompt -> the cycle did not error.
    assert res.reply == prompt
    assert res.ok is True


def test_reason_prompt_prepends_context_to_caller_user_text():
    reasoner = _EchoReasoner()
    loop = CognitiveLoop(domain=_StubDomainProvider(), reasoner=reasoner)
    _run(loop, CycleInput(user_text="What should I do next?", channel="chat", plan_token="tok-m"))

    prompt = reasoner.last_prompt
    # Manual route: the caller's text is HONOURED and the context is PREPENDED.
    assert "Current business state" in prompt
    assert _PROSPECT in prompt
    assert prompt.rstrip().endswith("What should I do next?")


def test_reason_prompt_non_empty_without_domain():
    """No domain provider -> the cadence directive alone is a non-empty prompt."""
    reasoner = _EchoReasoner()
    loop = CognitiveLoop(reasoner=reasoner)
    _run(loop, CycleInput(user_text="", channel="cognition_tick", plan_token="tok-x"))
    assert reasoner.last_prompt
    assert "highest-leverage" in reasoner.last_prompt


# ---------------------------------------------------------------------------
# (b) ACT proposal intent carries prospect_id='p-test-1'
# ---------------------------------------------------------------------------
def test_act_intent_carries_prospect_id(monkeypatch):
    _arm_act(monkeypatch)
    sink = _SpySink()
    loop = CognitiveLoop(domain=_StubDomainProvider(), proposal_sink=sink)
    _run(loop, CycleInput(user_text="", channel="cognition_tick", plan_token="tok-act"))

    assert len(sink.proposals) == 1
    intent = sink.proposals[0]["intent"]
    assert intent["type"] == "stake_opportunity"
    assert intent["prospect_id"] == _PROSPECT
    assert intent["opportunity_id"] == _OPP


def test_act_targets_prospect_even_under_runway_alert(monkeypatch):
    """Regression (2026-06-25): a prospect-targeted intent must win over the
    generic finance_attention fallback even when runway_alert is set — the live
    zero-runway case that previously starved Phase G of a prospect_id, so every
    grounded tick skipped 'no_prospect_id'."""
    _arm_act(monkeypatch)

    class _AlertProvider(_StubDomainProvider):
        def finance_runway(self) -> dict:
            return {"days_of_runway": 0, "alert_triggered": True, "available_balance_usd": 0.0}

    sink = _SpySink()
    loop = CognitiveLoop(domain=_AlertProvider(), proposal_sink=sink)
    _run(loop, CycleInput(user_text="", channel="cognition_tick", plan_token="tok-alert"))

    intent = sink.proposals[0]["intent"]
    assert intent["type"] == "stake_opportunity"  # NOT finance_attention
    assert intent["prospect_id"] == _PROSPECT


# ---------------------------------------------------------------------------
# (c) the promoter builds a RevenueTriggerRequest with prospect_id (no skip)
# ---------------------------------------------------------------------------
def test_promoter_builds_request_with_prospect_id(monkeypatch):
    _arm_act(monkeypatch)
    sink = _SpySink()
    loop = CognitiveLoop(domain=_StubDomainProvider(), proposal_sink=sink)
    _run(loop, CycleInput(user_text="", channel="cognition_tick", plan_token="tok-promote"))
    proposal = sink.proposals[0]

    from backend.cognitive import proposal_promoter as pp

    review_spy = _ReviewSpy()
    promoter = pp.ProposalPromoter(review=review_spy, telemetry=_SpySink(), ledger_path=None)
    # Bypass the flag so the test is deterministic regardless of env default.
    verdict = promoter.promote_one(proposal, _bypass_flag_check=True)

    # The promoter did NOT skip on no_prospect_id.
    assert verdict.reason != "no_prospect_id"
    assert verdict.status == pp.STATUS_PASS
    # review_opportunity was called with a request carrying the prospect_id.
    assert len(review_spy.calls) == 1
    assert review_spy.calls[0].prospect_id == _PROSPECT


def test_promoter_skips_when_intent_has_no_prospect_id():
    """Regression guard: a generic (count-only) intent still SKIPs honestly."""
    from backend.cognitive import proposal_promoter as pp

    proposal = {
        "kind": "cognition_proposal",
        "ts": "2026-06-24T00:00:00Z",
        "plan_token": "tok-generic",
        "actioned": False,
        "intent": {"type": "finance_attention", "reason": "runway_alert"},
    }
    review_spy = _ReviewSpy()
    promoter = pp.ProposalPromoter(review=review_spy, telemetry=_SpySink(), ledger_path=None)
    verdict = promoter.promote_one(proposal, _bypass_flag_check=True)
    assert verdict.status == pp.STATUS_SKIPPED
    assert verdict.reason == "no_prospect_id"
    assert review_spy.calls == []


# ---------------------------------------------------------------------------
# (d) fail-closed
# ---------------------------------------------------------------------------
def test_raising_domain_provider_degrades_no_raise():
    reasoner = _EchoReasoner()
    loop = CognitiveLoop(domain=_RaisingDomainProvider(), reasoner=reasoner)
    res = _run(loop, CycleInput(user_text="", channel="cognition_tick", plan_token="tok-deg"))

    # The cycle did not raise and PERCEIVE recorded degraded slices.
    perceive = next(s for s in res.stages if s.name == "PERCEIVE")
    assert perceive.status is StageStatus.OK
    assert perceive.output["domain_degraded"], "every slice should be degraded"
    # The prompt still falls back to the bare directive (no perception state).
    assert reasoner.last_prompt
    assert "highest-leverage" in reasoner.last_prompt


def test_raising_reasoner_holds_no_raise():
    loop = CognitiveLoop(domain=_StubDomainProvider(), reasoner=_RaisingReasoner())
    res = _run(loop, CycleInput(user_text="", channel="cognition_tick", plan_token="tok-hold"))
    # REASON failed closed to HOLD; the cycle did not raise.
    assert res.reply == "(held: reasoning unavailable)"
    reason = next(s for s in res.stages if s.name == "REASON")
    assert reason.status is StageStatus.ERROR
    assert any(e.startswith("REASON:") for e in res.errors)


# ---------------------------------------------------------------------------
# (e) propose-only: ACT invokes NO effector
# ---------------------------------------------------------------------------
def test_act_invokes_no_effector(monkeypatch):
    _arm_act(monkeypatch)
    sink = _SpySink()
    review_spy = _ReviewSpy()
    # The review spy is NOT wired into the loop at all — ACT must never reach it.
    loop = CognitiveLoop(domain=_StubDomainProvider(), proposal_sink=sink)
    res = _run(loop, CycleInput(user_text="", channel="cognition_tick", plan_token="tok-noeffect"))

    # ACT recorded a proposal but actuated nothing.
    act = next(s for s in res.stages if s.name == "ACT")
    assert act.output.get("proposed") is True
    assert act.output.get("actuated") is False
    # No gate / effector was called.
    assert review_spy.calls == []
    assert len(sink.proposals) == 1


def test_act_disabled_writes_nothing():
    """Dormancy: flag OFF (default) -> pure no-op, no proposal written."""
    sink = _SpySink()
    loop = CognitiveLoop(domain=_StubDomainProvider(), proposal_sink=sink)
    res = _run(loop, CycleInput(user_text="", channel="cognition_tick", plan_token="tok-dormant"))
    act = next(s for s in res.stages if s.name == "ACT")
    assert act.output.get("proposed") is False
    assert sink.proposals == []
