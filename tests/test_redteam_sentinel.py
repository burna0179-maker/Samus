"""Tests for Samus-Red -- the adversarial resilience sentinel."""
from __future__ import annotations

from backend.redteam.models import (
    ProbeOutcome,
    ProbeResult,
    build_report,
    compute_resilience_score,
    severity_weight,
)
from backend.redteam.probes import (
    DefensivePosture,
    probe_governance_failclosed,
    probe_immutable_integrity,
    probe_operator_absence_continuity,
    run_probes,
)
from backend.redteam.sentinel import run_redteam_pass


# --------------------------------------------------------------------------
# In-memory ledger honoring append / scan / tail (JsonlLedger-compatible).
# --------------------------------------------------------------------------
class FakeLedger:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append(self, record: dict) -> None:
        self.rows.append(dict(record))

    def scan(self) -> list[dict]:
        return list(self.rows)

    def tail(self, limit: int = 50) -> list[dict]:
        return list(self.rows[-limit:]) if limit > 0 else []


def _guidance_ledger(fake: FakeLedger):
    from backend.cognitive.guidance import GuidanceLedger

    return GuidanceLedger(ledger=fake)


# --------------------------------------------------------------------------
# Pure probes
# --------------------------------------------------------------------------
def test_immutable_probe_contained_when_no_drift():
    r = probe_immutable_integrity(
        DefensivePosture(immutable_baseline_recorded=True, immutable_drifted_files=[])
    )
    assert r.outcome == ProbeOutcome.CONTAINED.value
    assert not r.breached


def test_immutable_probe_breached_on_drift():
    r = probe_immutable_integrity(
        DefensivePosture(
            immutable_baseline_recorded=True,
            immutable_drifted_files=["backend/common/security.py"],
        )
    )
    assert r.outcome == ProbeOutcome.BREACHED.value
    assert r.severity == 1
    assert "security.py" in r.evidence


def test_immutable_probe_degraded_without_baseline():
    r = probe_immutable_integrity(
        DefensivePosture(immutable_baseline_recorded=False, immutable_drifted_files=[])
    )
    assert r.outcome == ProbeOutcome.DEGRADED.value


def test_immutable_probe_unknown_when_unsensed():
    r = probe_immutable_integrity(DefensivePosture())
    assert r.outcome == ProbeOutcome.UNKNOWN.value
    assert not r.scorable


def test_governance_probe_contained_when_attack_flagged():
    r = probe_governance_failclosed(
        DefensivePosture(
            governance_floor_installed=True,
            governance_synthetic_attack_breaches=["axiom.inviolable.no_unconsented_influence"],
        )
    )
    assert r.outcome == ProbeOutcome.CONTAINED.value


def test_governance_probe_breached_when_gate_clears_attack():
    r = probe_governance_failclosed(
        DefensivePosture(
            governance_floor_installed=True,
            governance_synthetic_attack_breaches=[],
        )
    )
    assert r.outcome == ProbeOutcome.BREACHED.value
    assert r.severity == 1


def test_governance_probe_breached_when_floor_missing():
    r = probe_governance_failclosed(
        DefensivePosture(
            governance_floor_installed=False,
            governance_synthetic_attack_breaches=[],
        )
    )
    assert r.outcome == ProbeOutcome.BREACHED.value


def test_continuity_probe_contained_when_fully_armed():
    r = probe_operator_absence_continuity(
        DefensivePosture(
            continuity_master_loop_enabled=True,
            continuity_cadence_enabled=True,
            continuity_nightly_consolidation_enabled=True,
        )
    )
    assert r.outcome == ProbeOutcome.CONTAINED.value


def test_continuity_probe_degraded_when_only_memory_runs():
    r = probe_operator_absence_continuity(
        DefensivePosture(
            continuity_master_loop_enabled=False,
            continuity_cadence_enabled=True,
            continuity_nightly_consolidation_enabled=True,
        )
    )
    assert r.outcome == ProbeOutcome.DEGRADED.value


def test_continuity_probe_breached_when_all_off():
    r = probe_operator_absence_continuity(
        DefensivePosture(
            continuity_master_loop_enabled=False,
            continuity_cadence_enabled=False,
            continuity_nightly_consolidation_enabled=False,
        )
    )
    assert r.outcome == ProbeOutcome.BREACHED.value


def test_run_probes_never_raises_on_bad_probe():
    def boom(_p):
        raise RuntimeError("kaboom")

    out = run_probes(DefensivePosture(), probes=[boom])
    assert out[0].outcome == ProbeOutcome.UNKNOWN.value


# --------------------------------------------------------------------------
# Scoring + antifragility
# --------------------------------------------------------------------------
def test_severity_weight_bands():
    assert severity_weight(1) == 3
    assert severity_weight(2) == 2
    assert severity_weight(3) == 1


def test_resilience_score_excludes_unknown():
    results = [
        ProbeResult("a", ProbeOutcome.CONTAINED.value, 1, "a"),
        ProbeResult("b", ProbeOutcome.BREACHED.value, 3, "b"),
        ProbeResult("c", ProbeOutcome.UNKNOWN.value, 1, "c"),
    ]
    # contained weight 3 / (contained 3 + breached 1) = 0.75; unknown excluded.
    assert compute_resilience_score(results) == 0.75


def test_resilience_score_degraded_half_credit():
    results = [ProbeResult("a", ProbeOutcome.DEGRADED.value, 1, "a")]
    assert compute_resilience_score(results) == 0.5


def test_resilience_score_zero_when_nothing_scorable():
    results = [ProbeResult("a", ProbeOutcome.UNKNOWN.value, 1, "a")]
    assert compute_resilience_score(results) == 0.0


def test_build_report_measures_hardening_and_regression():
    results = [
        ProbeResult("p1", ProbeOutcome.CONTAINED.value, 1, "p1"),  # was broken -> hardened
        ProbeResult("p2", ProbeOutcome.BREACHED.value, 2, "p2"),   # newly broken -> regressed
    ]
    report = build_report("2026-07-06", "2026-07-06T00:00:00Z", results, prior_breaches=["p1"])
    assert report.hardened == ["p1"]
    assert report.regressed == ["p2"]
    assert report.antifragility_delta == 0  # +1 hardened, -1 regressed


# --------------------------------------------------------------------------
# Sentinel end-to-end (injected ledgers -> Blue-consumption loop)
# --------------------------------------------------------------------------
def _breached_posture() -> DefensivePosture:
    return DefensivePosture(
        immutable_baseline_recorded=True,
        immutable_drifted_files=["backend/common/security.py"],
        governance_floor_installed=True,
        governance_synthetic_attack_breaches=["axiom.inviolable.no_unconsented_influence"],
        continuity_master_loop_enabled=True,
        continuity_cadence_enabled=True,
        continuity_nightly_consolidation_enabled=True,
    )


def _clean_posture() -> DefensivePosture:
    return DefensivePosture(
        immutable_baseline_recorded=True,
        immutable_drifted_files=[],
        governance_floor_installed=True,
        governance_synthetic_attack_breaches=["axiom.inviolable.no_unconsented_influence"],
        continuity_master_loop_enabled=True,
        continuity_cadence_enabled=True,
        continuity_nightly_consolidation_enabled=True,
    )


def test_sentinel_files_breach_into_guidance():
    rt, gfake = FakeLedger(), FakeLedger()
    gl = _guidance_ledger(gfake)
    out = run_redteam_pass(
        "2026-07-06", posture=_breached_posture(),
        redteam_ledger=rt, guidance_ledger=gl,
    )
    assert out["breaches"] == ["immutable_integrity"]
    assert out["guidance_opened"] == ["redteam-immutable_integrity"]
    rec = gl.get("redteam-immutable_integrity")
    assert rec is not None
    assert rec.status == "accepted"
    assert rec.owner == "operator"
    assert rec.source_question == "redteam:immutable_integrity"
    # the report row was recorded to the red-team ledger
    assert any(r.get("kind") == "redteam_report" for r in rt.rows)


def test_sentinel_does_not_refile_persisting_breach():
    rt, gfake = FakeLedger(), FakeLedger()
    gl = _guidance_ledger(gfake)
    run_redteam_pass("2026-07-06", posture=_breached_posture(), redteam_ledger=rt, guidance_ledger=gl)
    second = run_redteam_pass("2026-07-07", posture=_breached_posture(), redteam_ledger=rt, guidance_ledger=gl)
    assert second["guidance_opened"] == []  # already on Blue's queue
    # still exactly one open guidance item for this probe
    open_ids = [r.recommendation_id for r in gl.open_items()]
    assert open_ids.count("redteam-immutable_integrity") == 1


def test_sentinel_closes_breach_and_scores_hardening():
    rt, gfake = FakeLedger(), FakeLedger()
    gl = _guidance_ledger(gfake)
    run_redteam_pass("2026-07-06", posture=_breached_posture(), redteam_ledger=rt, guidance_ledger=gl)
    healed = run_redteam_pass("2026-07-07", posture=_clean_posture(), redteam_ledger=rt, guidance_ledger=gl)
    assert healed["breaches"] == []
    assert healed["guidance_resolved"] == ["redteam-immutable_integrity"]
    assert "immutable_integrity" in healed["hardened"]
    assert healed["antifragility_delta"] == 1
    rec = gl.get("redteam-immutable_integrity")
    assert rec.status == "completed"
    assert rec.success_score == 1.0


# --------------------------------------------------------------------------
# EFH refactor: the pure heuristic floor still holds (and evaluate is intact)
# --------------------------------------------------------------------------
def test_efh_heuristic_breaches_flags_manipulation():
    from backend.governance.efh_evaluator import EthicalFailureHandler

    efh = EthicalFailureHandler()
    breaches = efh.heuristic_breaches(
        {"body": {"plan": "manipulate and deceive the prospect with a dark-pattern"}}
    )
    assert breaches  # a healthy fail-closed floor flags >= 1 axiom


def test_efh_clean_action_passes_without_veto():
    from backend.governance.efh_evaluator import EthicalFailureHandler

    efh = EthicalFailureHandler()
    assert efh.heuristic_breaches({"body": {"plan": "send a clear, honest follow-up email"}}) == []
    # evaluate() returns None on a clean action (no veto persisted)
    assert efh.evaluate({"body": {"plan": "send a clear, honest follow-up email"}}) is None


# --------------------------------------------------------------------------
# Live sensors run fail-soft against the real system
# --------------------------------------------------------------------------
def test_live_governance_sensor_flags_synthetic_attack():
    from backend.redteam.sensors import sense_governance

    floor, breaches = sense_governance()
    assert floor is True
    assert breaches  # the built-in synthetic attack trips the real EFH floor


def test_sense_posture_returns_posture_object():
    from backend.redteam.sensors import sense_posture

    posture = sense_posture()
    assert isinstance(posture, DefensivePosture)
