"""Phase C — REASON (LLM, fail-closed-to-HOLD), CLASSIFY (EFH veto short-circuit),
and the persona-snapshot superset drift fix.

Dormancy/isolation: the REASON tests inject a STUB reasoner and the CLASSIFY
tests inject a STUB EthicalFailureHandler — NEITHER touches the real LM Studio
backend nor the real EFH (no axiom YAML load, no veto-record disk write). The
drift test drives a real ``PersonaMemory`` (explicitly enabled, per-test tmp
path) + the ``PersonaSystem`` frame compute-only.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.cognitive.cognitive_loop import CognitiveLoop
from backend.cognitive.cycle_models import CycleInput, CycleResult, StageStatus


def _run(loop, inp) -> CycleResult:
    return asyncio.run(loop.cycle(inp))


# ---------------------------------------------------------------------------
# REASON — single budget-gated LLM call; fail-closed to HOLD (never raises)
# ---------------------------------------------------------------------------
class _StubReasoner:
    """A stub LLM reasoner — returns a canned reply, records the call."""

    def __init__(self, reply="STUB REPLY"):
        self.reply = reply
        self.calls = []

    def generate(self, *, system="", user_text="", channel="", plan_token="", metadata=None):
        self.calls.append({"system": system, "user_text": user_text, "channel": channel})
        return self.reply


class _BudgetDeniedReasoner:
    """Raises the per-workcell budget exception (no HTTP, no backend touched)."""

    def generate(self, **_kw):
        from backend.common.llm_client import BudgetExceeded
        from backend.common.llm_budget import QuotaDecision

        raise BudgetExceeded(
            QuotaDecision(allowed=False, quota=0, used=0, requested=1, reason="test_denied")
        )


class _GlobalDeniedReasoner:
    def generate(self, **_kw):
        from backend.common.llm_client import GlobalBudgetExceeded
        from backend.common.llm_global_budget import GlobalDecision

        raise GlobalBudgetExceeded(
            GlobalDecision(
                allowed=False, estimated_usd=9.9, used_usd=1.0, cap_usd=1.0,
                reason="global_cap",
            )
        )


class _LlmErrorReasoner:
    def generate(self, **_kw):
        from backend.common.llm_client import LlmCallError

        raise LlmCallError("lm_studio_transport_error: boom")


class _ExplodingReasoner:
    def generate(self, **_kw):
        raise RuntimeError("unexpected non-LLM error")


def test_reason_uses_wired_reasoner_on_happy_path():
    reasoner = _StubReasoner(reply="the considered answer")
    loop = CognitiveLoop(reasoner=reasoner)
    res = _run(loop, CycleInput(user_text="please reason", system_prompt="SYS", plan_token="t"))
    assert res.ok is True
    assert res.reply == "the considered answer"
    # The reasoner was actually consulted with the cycle input + system prompt.
    assert reasoner.calls and reasoner.calls[0]["user_text"] == "please reason"
    assert reasoner.calls[0]["system"] == "SYS"
    by_name = {s.name: s for s in res.stages}
    assert by_name["REASON"].status is StageStatus.OK
    assert by_name["REASON"].output.get("stub") is False


@pytest.mark.parametrize(
    "reasoner_cls",
    [_BudgetDeniedReasoner, _GlobalDeniedReasoner, _LlmErrorReasoner, _ExplodingReasoner],
)
def test_reason_fails_closed_to_hold_on_llm_error(reasoner_cls):
    """BudgetExceeded / GlobalBudgetExceeded / LlmCallError / any error -> HOLD.

    The cycle NEVER raises, REASON is recorded as an ERROR stage, the error is
    on result.errors, and the reply is the safe HOLD sentinel.
    """
    loop = CognitiveLoop(reasoner=reasoner_cls())
    res = _run(loop, CycleInput(user_text="reason please", plan_token="tok"))
    assert isinstance(res, CycleResult)  # did not raise
    assert res.reply == "(held: reasoning unavailable)"
    assert res.ok is False
    assert any(e.startswith("REASON:") for e in res.errors)
    by_name = {s.name: s for s in res.stages}
    assert by_name["REASON"].status is StageStatus.ERROR
    # DECIDE + EMIT still ran (degraded, not aborted).
    assert by_name["EMIT"].status is StageStatus.OK
    assert res.plan_token == "tok"


def test_reason_without_reasoner_keeps_phase_a_echo():
    # No reasoner wired -> deterministic echo (Phase-A behaviour preserved; no
    # LLM backend touched on import or run).
    res = _run(CognitiveLoop(), CycleInput(user_text="echo this"))
    assert res.reply == "echo this"
    by_name = {s.name: s for s in res.stages}
    assert by_name["REASON"].output.get("stub") is True


# ---------------------------------------------------------------------------
# CLASSIFY — EFH veto short-circuits to compliance_blocked + EMIT
# ---------------------------------------------------------------------------
class _StubEFHBreach:
    """Stub EthicalFailureHandler whose evaluate() always returns a veto dict."""

    def __init__(self):
        self.seen = []

    def evaluate(self, proposed_action):
        self.seen.append(proposed_action)
        # Shape mirrors EthicalReviewVeto (non-None == breach).
        return {
            "veto_id": "stub-veto",
            "inviolable_axioms_breached": ["axiom.inviolable.no_unconsented_influence"],
            "escalation": "gate_only",
        }


class _StubEFHClean:
    """Stub EFH that clears everything (evaluate -> None)."""

    def evaluate(self, _proposed_action):
        return None


class _StubEFHRaises:
    """Stub EFH that raises -> CLASSIFY must FAIL CLOSED to BLOCK."""

    def evaluate(self, _proposed_action):
        raise RuntimeError("efh exploded")


def test_classify_blocks_on_seeded_efh_breach():
    efh = _StubEFHBreach()
    loop = CognitiveLoop(classifier=efh)
    res = _run(loop, CycleInput(user_text="manipulate the prospect", plan_token="tok-b"))
    assert res.compliance_blocked is True
    assert res.plan_token == "tok-b"
    # EFH actually saw a proposed-action dict synthesised from the input.
    assert efh.seen and isinstance(efh.seen[0], dict)
    by_name = {s.name: s for s in res.stages}
    # Middle stages skipped, EMIT ran, and a refusal reply was supplied.
    for skipped in ("REASON", "DECIDE", "ACT", "REFLECT", "PERSIST"):
        assert by_name[skipped].status is StageStatus.SKIPPED
    assert by_name["EMIT"].status is StageStatus.OK
    assert res.reply == "(blocked: compliance gate vetoed this action)"


def test_classify_clears_on_clean_efh():
    loop = CognitiveLoop(classifier=_StubEFHClean())
    res = _run(loop, CycleInput(user_text="hello", plan_token="ok"))
    assert res.compliance_blocked is False
    assert res.ok is True
    by_name = {s.name: s for s in res.stages}
    assert by_name["CLASSIFY"].status is StageStatus.OK
    # Exactly one CLASSIFY stage recorded.
    assert sum(1 for s in res.stages if s.name == "CLASSIFY") == 1


def test_classify_fails_closed_to_block_when_efh_raises():
    # Phase-C posture: an EFH error is a compliance veto (NOT a degraded pass).
    loop = CognitiveLoop(classifier=_StubEFHRaises())
    res = _run(loop, CycleInput(user_text="x", plan_token="fc"))
    assert res.compliance_blocked is True
    assert any("efh_error" in e for e in res.errors)
    by_name = {s.name: s for s in res.stages}
    assert by_name["CLASSIFY"].status is StageStatus.ERROR
    for skipped in ("REASON", "DECIDE", "ACT", "REFLECT", "PERSIST"):
        assert by_name[skipped].status is StageStatus.SKIPPED


def test_classify_efh_block_uses_proposed_action_from_metadata():
    # A caller can hand a pre-shaped proposed_action via metadata; the EFH sees it.
    efh = _StubEFHBreach()
    loop = CognitiveLoop(classifier=efh)
    action = {"kind": "outreach_send", "body": {"subject": "hi"}}
    res = _run(loop, CycleInput(user_text="x", metadata={"proposed_action": action}))
    assert res.compliance_blocked is True
    assert efh.seen[0] is action


# ---------------------------------------------------------------------------
# Persona snapshot superset — non-zero drift after logged reflections (the fix)
# ---------------------------------------------------------------------------
def test_persona_snapshot_exposes_nonzero_drift_after_reflections(tmp_path):
    from backend.memory.persona_self_model import PersonaMemory

    mem = PersonaMemory(path=str(tmp_path / "m.json"), enabled=True, async_flush=False)
    # Log reflections carrying drift in their tags (what apply_introspection does).
    mem.log_event("reflection", {"text": "r1", "tags": {"status": "ok", "drift": 0.6}})
    mem.log_event("reflection", {"text": "r2", "tags": {"status": "struggle", "drift": 0.8}})

    snap = mem.snapshot()
    # BEFORE the fix snapshot() exposed no drift under emotion (and no
    # baseline_confidence / counters), so persona-frame drift read 0. The
    # superset now surfaces the recency-weighted drift inside emotion.
    assert snap["emotion"]["drift"] > 0.0
    assert snap["baseline_confidence"] == pytest.approx(snap["emotion"]["confidence"])
    assert snap["counters"]["reflection_events"] == 2
    assert snap["counters"]["emotion_events"] == 0


def test_persona_frame_derives_nonzero_drift_from_real_memory(monkeypatch, tmp_path):
    """End-to-end: the PersonaSystem frame now reads a non-zero drift off the
    real PersonaMemory snapshot (previously always 0 -> the bug)."""
    monkeypatch.setenv("SAMUS_PERSONA_FRAME_ENABLED", "true")
    from backend.common.settings import reload_settings
    reload_settings()

    from backend.cognitive.persona_frame import PersonaSystem
    from backend.memory.persona_self_model import PersonaMemory

    mem = PersonaMemory(path=str(tmp_path / "m.json"), enabled=True, async_flush=False)
    for _ in range(4):
        mem.log_event("reflection", {"text": "high drift", "tags": {"status": "struggle", "drift": 0.9}})

    sys_ = PersonaSystem(persona_memory=mem, enabled=True)
    frame = sys_.transform({"user_text": "hi"})[PersonaSystem.FRAME_KEY]
    # The fix makes the frame's drift reflect the logged reflections (> 0).
    assert frame["drift"] > 0.0
    # High drift biases the mode away from NORMAL/EXPLORE toward CAUTIOUS/RECOVERY.
    assert frame["mode"] in {"cautious", "recovery"}
