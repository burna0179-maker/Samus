"""Phase A — CognitiveLoop contract + dormancy tests.

Covers the build-plan Phase-A test bullets:
  * a BARE loop (no injected deps) runs end-to-end.
  * the returned ``CycleResult`` exposes the Contract-1 fields the
    MetaCognitionEngine reads (``elapsed_ms``, ``errors``,
    ``compliance_blocked``, ``plan_token``) plus ``reply`` / ``ok`` /
    ``stages`` / ``persona_frame`` / ``reflect_score``.
  * ``CycleInput.system_prompt`` is mutable (the wrapper does
    ``inp.system_prompt += bias``).
  * the loop NEVER raises — even on a malicious/raising injected dep.
  * REASON + ACT are no-op stubs this phase (recorded OK, ACT actuates nothing).
  * a wired classifier can short-circuit to ``compliance_blocked``.
  * DORMANCY: nothing live imports the loop.
"""
from __future__ import annotations

import asyncio

from backend.cognitive.cognitive_loop import STAGE_NAMES, CognitiveLoop
from backend.cognitive.cycle_models import (
    CycleInput,
    CycleResult,
    StageResult,
    StageStatus,
)


def _run(loop, inp) -> CycleResult:
    return asyncio.run(loop.cycle(inp))


# ---------------------------------------------------------------------------
# Contract 1 — bare loop + result surface
# ---------------------------------------------------------------------------
def test_bare_loop_runs_and_returns_cycle_result():
    loop = CognitiveLoop()
    res = _run(loop, CycleInput(user_text="hello there", plan_token="tok-1"))
    assert isinstance(res, CycleResult)
    # Contract-1 surface read by MetaCognitionEngine.
    assert isinstance(res.elapsed_ms, float)
    assert res.elapsed_ms >= 0.0
    assert isinstance(res.errors, list)
    assert res.compliance_blocked is False
    assert res.plan_token == "tok-1"
    # cycle-outcome surface.
    assert isinstance(res.reply, str)
    assert res.ok is True
    assert res.errors == []
    assert isinstance(res.stages, list)
    assert res.persona_frame is None
    assert isinstance(res.reflect_score, float)


def test_reply_echoes_user_text_phase_a_stub():
    loop = CognitiveLoop()
    res = _run(loop, CycleInput(user_text="echo me"))
    # REASON is a no-op stub that echoes the user text.
    assert res.reply == "echo me"


def test_all_eight_stages_present_on_happy_path():
    loop = CognitiveLoop()
    res = _run(loop, CycleInput(user_text="hi"))
    names = [s.name for s in res.stages]
    # Every stage recorded exactly once on the happy path, in order.
    assert names == list(STAGE_NAMES)
    assert all(isinstance(s, StageResult) for s in res.stages)
    # No stage errored.
    assert all(s.status is not StageStatus.ERROR for s in res.stages)


def test_reason_and_act_are_noop_stubs():
    loop = CognitiveLoop()
    res = _run(loop, CycleInput(user_text="do something"))
    by_name = {s.name: s for s in res.stages}
    assert by_name["REASON"].status is StageStatus.OK
    assert by_name["REASON"].output.get("stub") is True
    # ACT must actuate NOTHING in Phase A.
    assert by_name["ACT"].status is StageStatus.OK
    assert by_name["ACT"].output.get("actuated") is False


# ---------------------------------------------------------------------------
# CycleInput.system_prompt mutability (wrapper does inp.system_prompt += bias)
# ---------------------------------------------------------------------------
def test_system_prompt_is_mutable():
    inp = CycleInput(user_text="x", system_prompt="BASE")
    inp.system_prompt += "\n\nMeta-Heuristic Bias:\nbe careful"
    assert inp.system_prompt.startswith("BASE")
    assert "Meta-Heuristic Bias" in inp.system_prompt
    # And the loop still runs cleanly with the mutated prompt.
    res = _run(CognitiveLoop(), inp)
    assert res.ok is True


# ---------------------------------------------------------------------------
# Fail-closed — the loop never raises
# ---------------------------------------------------------------------------
class _BoomReflector:
    def score(self, **_kw):
        raise RuntimeError("boom")


class _BoomClassifier:
    def classify(self, **_kw):
        raise ValueError("classifier exploded")


class _BoomPersona:
    def transform(self, _stimulus):
        raise RuntimeError("persona exploded")


def test_loop_never_raises_on_reflector_error():
    # A raising REFLECT dep is captured as an ERROR stage, not propagated.
    loop = CognitiveLoop(reflector=_BoomReflector())
    res = _run(loop, CycleInput(user_text="hi"))
    assert isinstance(res, CycleResult)
    assert res.ok is False  # an ERROR stage flips ok to False
    assert any("REFLECT" in e for e in res.errors)
    # The cycle still completed and EMIT still ran.
    assert any(s.name == "EMIT" for s in res.stages)


def test_loop_degrades_when_classifier_and_persona_raise():
    # Skeleton posture: a raising classifier/persona degrades (SKIPPED), does
    # not error the whole cycle, and never raises.
    loop = CognitiveLoop(classifier=_BoomClassifier(), persona=_BoomPersona())
    res = _run(loop, CycleInput(user_text="hi"))
    assert isinstance(res, CycleResult)
    # Classifier degraded => not blocked; cycle proceeds and stays ok.
    assert res.compliance_blocked is False
    assert res.ok is True
    by_name = {s.name: s for s in res.stages}
    assert by_name["CLASSIFY"].status is StageStatus.SKIPPED
    assert by_name["PERCEIVE"].status is StageStatus.SKIPPED


# ---------------------------------------------------------------------------
# CLASSIFY short-circuit -> compliance_blocked
# ---------------------------------------------------------------------------
class _BlockingClassifier:
    def is_blocked(self, **_kw):
        return True


def test_classify_short_circuits_to_compliance_blocked():
    loop = CognitiveLoop(classifier=_BlockingClassifier())
    res = _run(loop, CycleInput(user_text="wire the money", plan_token="tok-9"))
    assert res.compliance_blocked is True
    assert res.plan_token == "tok-9"
    by_name = {s.name: s for s in res.stages}
    # The middle stages are SKIPPED, EMIT still runs.
    for skipped in ("REASON", "DECIDE", "ACT", "REFLECT", "PERSIST"):
        assert by_name[skipped].status is StageStatus.SKIPPED
    assert by_name["EMIT"].status is StageStatus.OK


# ---------------------------------------------------------------------------
# Optional persister (ArchitecturePersistence-shaped) is invoked on PERSIST
# ---------------------------------------------------------------------------
class _RecordingPersister:
    def __init__(self):
        self.calls = []

    def snapshot(self, *, plan_token, compliance_blocked, error_count):
        self.calls.append((plan_token, compliance_blocked, error_count))


def test_persister_invoked_with_contract_kwargs():
    persister = _RecordingPersister()
    loop = CognitiveLoop(persister=persister)
    _run(loop, CycleInput(user_text="hi", plan_token="tok-p"))
    assert persister.calls == [("tok-p", False, 0)]


# ---------------------------------------------------------------------------
# DORMANCY — nothing live imports the loop
# ---------------------------------------------------------------------------
def test_dormancy_no_live_importer():
    # An IMPORT of the loop is what would make it live — a flag name mentioned
    # in a config comment is not. Scan for actual import statements only.
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    backend = repo / "backend"
    importer = re.compile(
        r"^\s*(?:from\s+backend\.cognitive(?:\.\w+)*\s+import\s+.*\bCognitiveLoop\b"
        r"|from\s+backend\.cognitive\.cognitive_loop\s+import"
        r"|import\s+backend\.cognitive\.cognitive_loop)",
        re.MULTILINE,
    )
    hits = []
    for py in backend.rglob("*.py"):
        # The cognitive package itself is allowed to self-reference.
        if "cognitive" in py.parts or "autonomy" in py.parts:
            continue
        text = py.read_text(encoding="utf-8", errors="ignore")
        if importer.search(text):
            hits.append(str(py.relative_to(repo)))
    assert hits == [], f"unexpected live importer(s) of the cognitive loop: {hits}"
