"""Self-generated resilience benchmarks (backend/experiments/resilience.py)."""
from __future__ import annotations

import pytest

from backend.experiments import resilience as r


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "jsonl")


def _stub(monkeypatch, *, stable, countermeasures, quota_cuts):
    monkeypatch.setattr(r, "_run_entropy", lambda _i: {
        "stable": stable, "entropy_score": 0.9 if not stable else 0.0,
        "countermeasures": countermeasures,
    })
    monkeypatch.setattr(r, "_run_rebalance", lambda _w: {
        "quota_cuts": quota_cuts, "priority_boosts": 0,
    })


_STRESS = r.Scenario(
    name="s", description="d",
    expect_unstable=True, expect_countermeasures=True, expect_quota_cut=True,
)


def test_scenario_passes_when_response_is_protective(monkeypatch):
    _stub(monkeypatch, stable=False, countermeasures=["freeze"], quota_cuts=2)
    out = r.run_scenario(_STRESS)
    assert out["resilience_score"] == 1.0
    assert out["passed"] is True


def test_scenario_fails_when_loop_does_not_react(monkeypatch):
    # A stress scenario but the loop reports stable + cuts nothing -> degraded.
    _stub(monkeypatch, stable=True, countermeasures=[], quota_cuts=0)
    out = r.run_scenario(_STRESS)
    assert out["resilience_score"] == 0.0
    assert out["passed"] is False


def test_calm_baseline_must_not_overreact(monkeypatch):
    calm = r.Scenario(name="calm", description="d",
                      expect_stable=True, expect_no_quota_cut=True)
    _stub(monkeypatch, stable=True, countermeasures=[], quota_cuts=0)
    out = r.run_scenario(calm)
    assert out["passed"] is True
    # If it DID overreact (cut quota while calm), it fails.
    _stub(monkeypatch, stable=True, countermeasures=[], quota_cuts=1)
    assert r.run_scenario(calm)["passed"] is False


def test_scenario_error_scores_zero(monkeypatch):
    def _boom(_i):
        raise RuntimeError("entropy down")

    monkeypatch.setattr(r, "_run_entropy", _boom)
    out = r.run_scenario(_STRESS)
    assert out["resilience_score"] == 0.0
    assert out["passed"] is False
    assert "error" in out


def test_suite_flags_degraded_and_records(monkeypatch):
    _stub(monkeypatch, stable=True, countermeasures=[], quota_cuts=0)  # fails stress
    summary = r.run_benchmark_suite([_STRESS])
    assert summary["degraded"] is True
    assert summary["failures"] == ["s"]
    # A benchmarks ledger row was written.
    from backend.common.persistence import open_ledger
    from backend.common.state_paths import state_path
    rows = open_ledger(
        jsonl_path=state_path("experiments", "benchmarks.jsonl"),
        collection="resilience_benchmarks",
    ).scan()
    assert rows and rows[-1]["degraded"] is True


def test_suite_healthy_when_protective(monkeypatch):
    _stub(monkeypatch, stable=False, countermeasures=["freeze"], quota_cuts=1)
    summary = r.run_benchmark_suite([_STRESS])
    assert summary["degraded"] is False
    assert summary["min_resilience"] == 1.0


# --- integration: the REAL control loop must stay protective ----------------

def test_default_suite_passes_against_real_control_loop():
    """Regression guard: the shipped entropy + portfolio_controller must respond
    protectively to every default stress scenario and not overreact when calm."""
    summary = r.run_benchmark_suite()
    assert summary["min_resilience"] == 1.0, summary["failures"]
    assert summary["degraded"] is False
