"""Phase B — the six backend.autonomy.* subsystems (Contract 2) + dormancy.

Covers the build-plan Phase-B test bullets:
  * each of the six classes constructs no-arg and satisfies its Contract-2
    signature.
  * the wrapper's exact call sequence works end-to-end against all six.
  * ReinforcementHeuristicEngine clamps weight to [0.1, 5.0], applies decay,
    and clamps reward to [-1, 1].
  * the RL weights persist (round-trip) under state_path("autonomy", ...).
  * each FLAGGED subsystem is INERT when its flag is False (no-op / ""/False/
    empty), and a fresh state dir stays empty when the flags are off.
"""

from __future__ import annotations

import json

import pytest

from backend.autonomy import (
    ArchitecturePersistence,
    Autotuner,
    HeuristicRegistry,
    ReinforcementHeuristicEngine,
    SituationIndex,
    UpgradeEngine,
)
from backend.autonomy.reinforcement_heuristics import HeuristicProfile


def _reload():
    from backend.common.settings import reload_settings

    reload_settings()


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Point the writable state root at a per-test tmp dir + reset settings.

    Every autonomy persistence path resolves through
    backend.common.state_paths.state_path, which honours SAMUS_STATE_ROOT.
    """
    root = tmp_path / "state"
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(root))
    yield root


def _set_flags(monkeypatch, *, reinforcement=False, autotuner=False, upgrade=False):
    monkeypatch.setenv("SAMUS_AUTONOMY_REINFORCEMENT_ENABLED", "true" if reinforcement else "false")
    monkeypatch.setenv("SAMUS_AUTONOMY_AUTOTUNER_ENABLED", "true" if autotuner else "false")
    monkeypatch.setenv("SAMUS_AUTONOMY_UPGRADE_ENABLED", "true" if upgrade else "false")
    _reload()


# ===========================================================================
# Construction + Contract-2 signatures
# ===========================================================================
def test_all_six_construct_no_arg():
    assert SituationIndex() is not None
    assert HeuristicRegistry() is not None
    assert ReinforcementHeuristicEngine() is not None
    assert Autotuner() is not None
    assert UpgradeEngine() is not None
    assert ArchitecturePersistence() is not None


def test_situation_index_classify_signature():
    si = SituationIndex()
    out = si.classify(text="please wire $500", channel="email", plan_token="t")
    assert isinstance(out, dict)
    assert set(out.keys()) == {"risk", "type"}
    # high-risk financial term -> high risk
    assert out["type"] == "high_risk"
    assert out["risk"] == "high"
    # generic
    g = si.classify(text="the sky is blue", channel="chat", plan_token="t")
    assert g == {"risk": "low", "type": "generic"}


def test_heuristic_registry_evaluate_pre_signature():
    hr = HeuristicRegistry()
    # high_risk situation -> non-empty conservative bias
    bias = hr.evaluate_pre(text="x", situation={"risk": "high", "type": "high_risk"})
    assert isinstance(bias, str) and bias
    assert "operator" in bias.lower() or "govern" in bias.lower()
    # unknown type -> "" (no bias)
    assert hr.evaluate_pre(text="x", situation={"type": "nope"}) == ""
    # empty situation -> "" (never raises)
    assert hr.evaluate_pre(text="x", situation={}) == ""


def test_architecture_persistence_snapshot_signature(state_dir):
    ap = ArchitecturePersistence()
    # snapshot returns None and never raises
    assert ap.snapshot(plan_token="tok", compliance_blocked=True, error_count=2) is None
    path = state_dir / "autonomy" / "architecture_snapshots.jsonl"
    assert path.exists()
    rec = json.loads(path.read_text(encoding="utf-8").strip())
    assert rec["plan_token"] == "tok"
    assert rec["compliance_blocked"] is True
    assert rec["error_count"] == 2


# ===========================================================================
# Wrapper's exact call sequence works against all six (interface match)
# ===========================================================================
def test_wrapper_call_sequence_end_to_end(state_dir, monkeypatch):
    # Arm all three flagged subsystems so the full path exercises.
    _set_flags(monkeypatch, reinforcement=True, autotuner=True, upgrade=True)

    si = SituationIndex()
    hr = HeuristicRegistry()
    rl = ReinforcementHeuristicEngine()
    at = Autotuner()
    ue = UpgradeEngine()
    ap = ArchitecturePersistence()

    # --- META-PERCEPTION ---
    situation = si.classify(text="run the report", channel="chat", plan_token="tok")
    base_bias = hr.evaluate_pre(text="run the report", situation=situation)
    weight = rl.get_weight(situation.get("type", "generic"))
    assert isinstance(weight, float)
    assert isinstance(base_bias, str)

    # --- a fake CycleResult carrying the Contract-1 surface ---
    class _R:
        elapsed_ms = 6000.0  # over the upgrade latency threshold
        errors = ["REASON:boom"]
        compliance_blocked = False
        plan_token = "tok"

    result = _R()

    # --- META-REFLECTION ---
    at.adjust(
        latency=result.elapsed_ms,
        errors=result.errors,
        compliance_blocked=result.compliance_blocked,
    )
    if ue.evaluate(result=result, situation=situation):
        ue.execute()
    ap.snapshot(
        plan_token=result.plan_token,
        compliance_blocked=result.compliance_blocked,
        error_count=len(result.errors),
    )
    # reinforcement reward math (matches recovery wrapper)
    reward = 0.0
    if result.errors:
        reward -= 0.3
    reward = max(-1.0, min(1.0, reward))
    rl.reinforce(situation.get("type", "generic"), reward)

    # All six produced their artifacts under the state dir.
    assert (state_dir / "autonomy" / "architecture_snapshots.jsonl").exists()
    assert (state_dir / "autonomy" / "upgrade_proposals.jsonl").exists()
    assert (state_dir / "autonomy" / "heuristic_weights.json").exists()
    assert (state_dir / "autonomy" / "autotuner_state.json").exists()


# ===========================================================================
# Reinforcement learning — clamps + decay (ported from recovery)
# ===========================================================================
def test_rl_weight_clamps_and_decay(state_dir, monkeypatch):
    _set_flags(monkeypatch, reinforcement=True)
    rl = ReinforcementHeuristicEngine()

    # Default weight is 1.0
    assert rl.get_weight("command") == pytest.approx(1.0)

    # Reward is clamped to [-1, 1]: a +100 reward acts as +1.
    rl.reinforce("command", 100.0)
    w = rl.get_weight("command")
    # weight = 1.0*0.995 + 1.0*0.05 = 1.045
    assert w == pytest.approx(1.0 * 0.995 + 1.0 * 0.05, abs=1e-6)

    # Drive weight to the MAX clamp with many big positive rewards.
    for _ in range(500):
        rl.reinforce("command", 1.0)
    assert rl.get_weight("command") <= ReinforcementHeuristicEngine.MAX_WEIGHT
    assert rl.get_weight("command") == pytest.approx(
        ReinforcementHeuristicEngine.MAX_WEIGHT, abs=1e-6
    )

    # Drive a different heuristic to the MIN clamp with many negative rewards.
    for _ in range(500):
        rl.reinforce("risky", -1.0)
    assert rl.get_weight("risky") >= ReinforcementHeuristicEngine.MIN_WEIGHT
    assert rl.get_weight("risky") == pytest.approx(
        ReinforcementHeuristicEngine.MIN_WEIGHT, abs=1e-6
    )

    # snapshot exposes per-heuristic stats
    snap = rl.snapshot()
    assert "command" in snap and "risky" in snap
    assert snap["command"]["success"] >= 1
    assert snap["risky"]["failure"] >= 1


def test_rl_persistence_round_trip(state_dir, monkeypatch):
    _set_flags(monkeypatch, reinforcement=True)
    rl1 = ReinforcementHeuristicEngine()
    rl1.reinforce("question", 0.4)
    rl1.reinforce("question", 0.4)
    w1 = rl1.get_weight("question")

    # A fresh engine loads the persisted weights.
    rl2 = ReinforcementHeuristicEngine()
    assert rl2.get_weight("question") == pytest.approx(w1, abs=1e-9)

    # The on-disk file is well-formed.
    path = state_dir / "autonomy" / "heuristic_weights.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "question" in data["heuristics"]


def test_heuristic_profile_defaults():
    p = HeuristicProfile()
    assert p.weight == 1.0
    assert p.decay == 0.995
    assert p.success_count == 0
    assert p.failure_count == 0


# ===========================================================================
# Dormancy — each flagged subsystem is INERT when its flag is False
# ===========================================================================
def test_reinforcement_inert_when_flag_off(state_dir, monkeypatch):
    _set_flags(monkeypatch, reinforcement=False)
    rl = ReinforcementHeuristicEngine()
    # get_weight returns the neutral default and mutates no state.
    assert rl.get_weight("command") == pytest.approx(1.0)
    # reinforce is a no-op: weight unchanged, snapshot empty, NO disk write.
    rl.reinforce("command", 1.0)
    assert rl.get_weight("command") == pytest.approx(1.0)
    assert rl.snapshot() == {}
    assert not (state_dir / "autonomy" / "heuristic_weights.json").exists()


def test_autotuner_inert_when_flag_off(state_dir, monkeypatch):
    _set_flags(monkeypatch, autotuner=False)
    at = Autotuner()
    assert at.adjust(latency=9999.0, errors=["x"], compliance_blocked=True) is None
    assert at.snapshot() == {}
    assert not (state_dir / "autonomy" / "autotuner_state.json").exists()


def test_upgrade_inert_when_flag_off(state_dir, monkeypatch):
    _set_flags(monkeypatch, upgrade=False)
    ue = UpgradeEngine()

    class _R:
        elapsed_ms = 999999.0
        errors = ["a", "b", "c"]
        compliance_blocked = True
        plan_token = "t"

    # evaluate always False when disabled; execute a no-op (no ledger write).
    assert ue.evaluate(result=_R(), situation={"type": "high_risk"}) is False
    ue.execute()
    assert not (state_dir / "autonomy" / "upgrade_proposals.jsonl").exists()


def test_upgrade_execute_writes_proposal_only_never_self_modifies(state_dir, monkeypatch):
    # When armed, execute() writes a PROPOSAL record (status=proposed) and
    # nothing else. This is the load-bearing safety property.
    _set_flags(monkeypatch, upgrade=True)
    ue = UpgradeEngine()

    class _R:
        elapsed_ms = 6000.0
        errors = ["x", "y"]
        compliance_blocked = True
        plan_token = "t"

    assert ue.evaluate(result=_R(), situation={"risk": "high", "type": "high_risk"}) is True
    ue.execute()
    path = state_dir / "autonomy" / "upgrade_proposals.jsonl"
    assert path.exists()
    rec = json.loads(path.read_text(encoding="utf-8").strip())
    assert rec["status"] == "proposed"
    assert rec["kind"] == "architecture_upgrade"
    assert "PROPOSAL ONLY" in rec["note"]
    # execute() is idempotent on the pending slot: a second call writes nothing.
    ue.execute()
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1


# ===========================================================================
# Dormancy — no live importer of the autonomy package
# ===========================================================================
def test_dormancy_no_live_importer():
    # An IMPORT of the package is what would make it live — a flag name in a
    # config comment is not. Scan for actual import statements only.
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    backend = repo / "backend"
    importer = re.compile(
        r"^\s*(?:from\s+backend\.autonomy(?:\.\w+)*\s+import"
        r"|from\s+backend\s+import\s+autonomy\b"
        r"|import\s+backend\.autonomy\b)",
        re.MULTILINE,
    )
    hits = []
    for py in backend.rglob("*.py"):
        if "autonomy" in py.parts or "cognitive" in py.parts:
            continue
        text = py.read_text(encoding="utf-8", errors="ignore")
        if importer.search(text):
            hits.append(str(py.relative_to(repo)))
    assert hits == [], f"unexpected live importer(s) of backend.autonomy: {hits}"
