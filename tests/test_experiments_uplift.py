"""Causal uplift overlay — arm-vs-control treatment effect (backend/experiments/uplift.py)."""

from __future__ import annotations

import pytest

from backend.experiments import registry, uplift


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "jsonl")


def _register(arms, *, dimension="opener", control_arm=""):
    exp = registry.register_experiment(dimension=dimension, arms=arms, experiment_id="exp-u")
    if control_arm:
        exp.control_arm = control_arm
        registry.save_experiment(exp)
    return exp


# baseline 10%, aggressive 25% (significant +15pp), weak 8% (negative).
_STATS = {
    "baseline": {"wins": 10, "trials": 100},
    "aggressive": {"wins": 25, "trials": 100},
    "weak": {"wins": 8, "trials": 100},
}


def test_uplift_vs_explicit_control():
    _register(["baseline", "aggressive", "weak"], control_arm="baseline")
    report = uplift.uplift_report("exp-u", control_arm="baseline", stats=_STATS)

    assert report["ok"] is True
    assert report["control_arm"] == "baseline"
    assert report["control_rate"] == 0.10
    # Sorted best-uplift-first: aggressive (+0.15) before weak (-0.02).
    assert [a["arm"] for a in report["arms"]] == ["aggressive", "weak"]

    agg = report["arms"][0]
    assert agg["absolute_uplift"] == 0.15
    assert agg["relative_uplift"] == 1.5
    assert agg["significant"] is True
    assert agg["spurious_risk"] == uplift.SPURIOUS_LOW
    assert agg["confidence"] > 0.99

    weak = report["arms"][1]
    assert weak["absolute_uplift"] == -0.02
    assert weak["significant"] is False  # negative + not significant


def test_control_falls_back_to_highest_trial_arm():
    # No explicit control -> the arm with the most trials is the incumbent.
    _register(["a", "b"])
    stats = {"a": {"wins": 5, "trials": 200}, "b": {"wins": 40, "trials": 50}}
    report = uplift.uplift_report("exp-u", stats=stats)
    assert report["control_arm"] == "a"  # 200 trials > 50


def test_thin_sample_is_high_spurious_risk():
    _register(["baseline", "cand"], control_arm="baseline")
    stats = {"baseline": {"wins": 1, "trials": 10}, "cand": {"wins": 5, "trials": 10}}
    report = uplift.uplift_report("exp-u", control_arm="baseline", stats=stats)
    cand = report["arms"][0]
    assert cand["spurious_risk"] == uplift.SPURIOUS_HIGH  # trials < min_sample


def test_best_causal_arm_requires_significant_low_risk_uplift():
    _register(["baseline", "aggressive", "weak"], control_arm="baseline")
    best = uplift.best_causal_arm("exp-u", stats=_STATS)
    assert best is not None
    assert best["arm"] == "aggressive"


def test_best_causal_arm_none_when_no_arm_beats_control():
    _register(["baseline", "weak"], control_arm="baseline")
    stats = {"baseline": {"wins": 20, "trials": 100}, "weak": {"wins": 18, "trials": 100}}
    assert uplift.best_causal_arm("exp-u", stats=stats) is None


def test_unknown_experiment_returns_error():
    report = uplift.uplift_report("does-not-exist", stats=_STATS)
    assert report["ok"] is False


def test_control_arm_persists_on_experiment_roundtrip():
    _register(["baseline", "cand"], control_arm="baseline")
    reloaded = registry.get_experiment("exp-u")
    assert reloaded is not None
    assert reloaded.control_arm == "baseline"
