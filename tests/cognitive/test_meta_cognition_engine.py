"""Phase D — MetaCognitionEngine wrapper.

Covers the build-plan Phase-D test bullets:
  * META-PERCEPTION bias mutates ``inp.system_prompt`` (when enabled).
  * the META-REFLECTION reward math matches the recovery original
    (compliance_blocked -> -0.5; clean + fast -> +0.5).
  * ``autonomy_meta_enabled=False`` == byte-identical to direct ``loop.cycle``
    (pure passthrough — no bias, no meta stage).
  * the engine runs with its subsystems forced to ``None``.

Dormancy/isolation: every test uses a tiny in-process stub ``loop`` (records the
input it received, returns a controllable ``CycleResult``). No real LLM, EFH,
persistence, or LM Studio backend is touched. The reward-math test swaps the
engine's reinforcement collaborator for a recording double so we can read the
exact reward without arming any global flag.
"""
from __future__ import annotations

import asyncio
import copy

from backend.cognitive.cycle_models import CycleInput, CycleResult
from backend.cognitive.meta_cognition_engine import MetaCognitionEngine


class _StubLoop:
    """Records the input it was handed; returns a preset CycleResult."""

    def __init__(self, result: CycleResult | None = None):
        self._result = result if result is not None else CycleResult(ok=True)
        self.received_system_prompt = None
        self.received_inp = None
        self.calls = 0

    async def cycle(self, inp):
        self.calls += 1
        self.received_inp = inp
        self.received_system_prompt = getattr(inp, "system_prompt", None)
        # Echo the plan_token through like the real loop does.
        self._result.plan_token = getattr(inp, "plan_token", "") or self._result.plan_token
        return self._result


class _RecordingReinforcement:
    """Captures (name, reward) pairs so the reward math can be asserted."""

    def __init__(self):
        self.rewards = []

    def get_weight(self, _name):
        return 1.0

    def reinforce(self, name, reward):
        self.rewards.append((name, reward))


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Dormancy — disabled == byte-identical passthrough to loop.cycle
# ---------------------------------------------------------------------------
def test_disabled_is_pure_passthrough_no_bias():
    base_prompt = "SYSTEM BASE PROMPT"
    loop = _StubLoop(CycleResult(ok=True, reply="loop-reply"))
    eng = MetaCognitionEngine(loop, enabled=False)
    inp = CycleInput(user_text="wire the money now", system_prompt=base_prompt, plan_token="p1")
    res = _run(eng.cycle(inp))
    # The wrapped loop saw the UNMODIFIED system prompt (no Meta-Heuristic Bias).
    assert loop.received_system_prompt == base_prompt
    assert "Meta-Heuristic Bias" not in inp.system_prompt
    # The returned result is exactly the object the loop produced.
    assert res is loop._result
    assert res.reply == "loop-reply"


def test_disabled_matches_direct_loop_cycle_byte_identical():
    """Disabled wrapper result == direct loop.cycle result for the same input."""
    # Two equivalent inputs + two identical stub loops.
    inp_direct = CycleInput(user_text="hello", system_prompt="S", plan_token="tok")
    inp_wrapped = copy.deepcopy(inp_direct)
    loop_direct = _StubLoop(CycleResult(ok=True, reply="R", compliance_blocked=False))
    loop_wrapped = _StubLoop(CycleResult(ok=True, reply="R", compliance_blocked=False))

    direct = _run(loop_direct.cycle(inp_direct))
    eng = MetaCognitionEngine(loop_wrapped, enabled=False)
    wrapped = _run(eng.cycle(inp_wrapped))

    # The input the wrapped loop saw is byte-identical to the direct input (the
    # wrapper appended nothing). And the result dicts match modulo timing.
    assert loop_wrapped.received_system_prompt == "S"
    assert inp_wrapped.system_prompt == inp_direct.system_prompt
    d_direct = direct.to_dict()
    d_wrapped = wrapped.to_dict()
    d_direct.pop("elapsed_ms", None)
    d_wrapped.pop("elapsed_ms", None)
    assert d_direct == d_wrapped


def test_enabled_by_default_flag_on():
    # Slice B activation (2026-07-06): the flag default flipped False -> True.
    # No explicit enabled= -> sources the flag (default True now) -> wrapper runs
    # and appends heuristic bias for a high-risk stimulus. The kill-switch path
    # (enabled=False -> passthrough) is still covered by
    # test_disabled_is_pure_passthrough_no_bias above.
    loop = _StubLoop()
    eng = MetaCognitionEngine(loop)
    inp = CycleInput(user_text="wire the money now", system_prompt="BASE", plan_token="p")
    _run(eng.cycle(inp))
    assert loop.received_system_prompt.startswith("BASE")
    assert "Meta-Heuristic Bias" in loop.received_system_prompt


# ---------------------------------------------------------------------------
# META-PERCEPTION — bias mutates system_prompt (enabled)
# ---------------------------------------------------------------------------
def test_enabled_bias_mutates_system_prompt_on_high_risk():
    # The real SituationIndex + HeuristicRegistry are pure (not flag-gated), so an
    # explicitly-enabled engine produces a real bias for a high-risk stimulus.
    loop = _StubLoop(CycleResult(ok=True))
    eng = MetaCognitionEngine(loop, enabled=True)
    inp = CycleInput(user_text="please wire a payment to this bank account",
                     system_prompt="BASE", plan_token="p2")
    _run(eng.cycle(inp))
    # The loop saw a system prompt that BASE + the appended Meta-Heuristic Bias.
    assert loop.received_system_prompt.startswith("BASE")
    assert "Meta-Heuristic Bias" in loop.received_system_prompt
    # High-risk bias text leads to a caution/defer instruction.
    assert "high-risk" in loop.received_system_prompt.lower()


def test_enabled_no_bias_for_neutral_generic_input():
    # A generic stimulus yields no HeuristicRegistry bias -> prompt unchanged
    # (the engine only appends when there is bias text).
    loop = _StubLoop(CycleResult(ok=True))
    eng = MetaCognitionEngine(loop, enabled=True)
    inp = CycleInput(user_text="xyzzy", system_prompt="BASE", plan_token="p3")
    _run(eng.cycle(inp))
    assert loop.received_system_prompt == "BASE"


# ---------------------------------------------------------------------------
# META-REFLECTION — reward math matches recovery
# ---------------------------------------------------------------------------
def _engine_with_recording_rl(result: CycleResult):
    loop = _StubLoop(result)
    eng = MetaCognitionEngine(loop, enabled=True)
    rec = _RecordingReinforcement()
    eng._reinforcement = rec  # swap in the recorder (no global flag armed)
    return eng, rec


def test_reward_compliance_blocked_is_minus_half():
    # compliance_blocked -> -0.5 component. Use a SLOW cycle (>=1500ms) so the
    # unconditional +0.1 fast bonus (recovery math) doesn't apply; net == -0.5.
    result = CycleResult(ok=False, compliance_blocked=True, errors=[], elapsed_ms=2000.0)
    eng, rec = _engine_with_recording_rl(result)
    _run(eng.cycle(CycleInput(user_text="x", plan_token="p")))
    assert rec.rewards, "reinforce must be called in META-REFLECTION"
    _name, reward = rec.rewards[-1]
    assert reward == -0.5


def test_reward_blocked_but_fast_keeps_unconditional_fast_bonus():
    # Recovery math adds +0.1 for elapsed<1500 UNCONDITIONALLY, so a fast blocked
    # cycle nets -0.5 + 0.1 = -0.4 (proves the fast bonus is not gated on success).
    result = CycleResult(ok=False, compliance_blocked=True, errors=[], elapsed_ms=10.0)
    eng, rec = _engine_with_recording_rl(result)
    _run(eng.cycle(CycleInput(user_text="x", plan_token="p")))
    _name, reward = rec.rewards[-1]
    assert reward == -0.4


def test_reward_clean_and_fast_is_plus_half():
    # No errors + not blocked -> +0.4; elapsed < 1500 -> +0.1; total +0.5.
    result = CycleResult(ok=True, compliance_blocked=False, errors=[], elapsed_ms=12.0)
    eng, rec = _engine_with_recording_rl(result)
    _run(eng.cycle(CycleInput(user_text="x", plan_token="p")))
    _name, reward = rec.rewards[-1]
    assert reward == 0.5


def test_reward_errors_present_subtracts_and_clamps():
    # errors present -> -0.3; slow (>1500) -> no +0.1; not blocked so no +0.4 and
    # no -0.5. Net -0.3 (within [-1, 1]).
    result = CycleResult(ok=False, compliance_blocked=False, errors=["REASON:x"], elapsed_ms=4000.0)
    eng, rec = _engine_with_recording_rl(result)
    _run(eng.cycle(CycleInput(user_text="x", plan_token="p")))
    _name, reward = rec.rewards[-1]
    assert reward == -0.3


def test_reward_blocked_and_errored_is_floor_clamped():
    # blocked (-0.5) + errors (-0.3) = -0.8 (still within the floor, not clamped).
    result = CycleResult(ok=False, compliance_blocked=True, errors=["e"], elapsed_ms=9000.0)
    eng, rec = _engine_with_recording_rl(result)
    _run(eng.cycle(CycleInput(user_text="x", plan_token="p")))
    _name, reward = rec.rewards[-1]
    assert reward == -0.8


# ---------------------------------------------------------------------------
# Runs with subsystems None
# ---------------------------------------------------------------------------
def test_runs_with_all_subsystems_none_enabled():
    loop = _StubLoop(CycleResult(ok=True, reply="ok"))
    eng = MetaCognitionEngine(loop, enabled=True)
    # Force every collaborator to None (simulates the autonomy package absent).
    eng._upgrade_engine = None
    eng._autotuner = None
    eng._situation = None
    eng._heuristics = None
    eng._persistence = None
    eng._reinforcement = None
    eng._persona = None
    inp = CycleInput(user_text="anything", system_prompt="BASE", plan_token="p")
    res = _run(eng.cycle(inp))
    # No situation index -> neutral situation -> no bias -> prompt unchanged.
    assert loop.received_system_prompt == "BASE"
    assert res.reply == "ok"
    assert loop.calls == 1


def test_runs_with_subsystems_none_disabled():
    # Disabled + no subsystems is still a clean passthrough.
    loop = _StubLoop(CycleResult(ok=True))
    eng = MetaCognitionEngine(loop, enabled=False)
    eng._situation = eng._heuristics = eng._reinforcement = None
    eng._autotuner = eng._upgrade_engine = eng._persistence = eng._persona = None
    res = _run(eng.cycle(CycleInput(user_text="x", system_prompt="B", plan_token="p")))
    assert loop.received_system_prompt == "B"
    assert res is loop._result


# ---------------------------------------------------------------------------
# never raises even if a subsystem misbehaves (META-REFLECTION is best-effort)
# ---------------------------------------------------------------------------
class _BoomReinforcement:
    def get_weight(self, _n):
        return 1.0

    def reinforce(self, *_a, **_k):
        raise RuntimeError("rl boom")


def test_meta_reflection_swallows_subsystem_errors():
    loop = _StubLoop(CycleResult(ok=True, reply="r"))
    eng = MetaCognitionEngine(loop, enabled=True)
    eng._reinforcement = _BoomReinforcement()
    # The recovery contract: _post_cycle_analysis is wrapped in try/except pass.
    res = _run(eng.cycle(CycleInput(user_text="x", plan_token="p")))
    assert res.reply == "r"  # cycle completed despite the reinforcement error
