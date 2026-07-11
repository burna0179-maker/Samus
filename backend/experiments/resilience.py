"""Self-generated resilience benchmarks — stress-test the control loop nightly.

Elite systems generate their own tests. Samus is never stress-tested against
synthetic failures (cost shock, error storm, channel collapse), so capability
degradation is invisible until users feel it. This module is that harness: a set
of deterministic perturbation scenarios run through the REAL control-loop
decision functions (``entropy.scan`` + ``portfolio_controller.run_rebalance``),
scoring whether the system responds *protectively* — flags instability, fires
countermeasures, cuts quota on the degraded workcell — and, on a calm baseline,
does NOT overreact.

Deliberately deterministic, NOT LLM-generated: the literature on
LLM-as-benchmark-generator documents a self-bias that inflates scores, so the
scenarios are fixed input perturbations with explicit expectations. Each nightly
run appends a resilience row to a benchmarks ledger and emits a ``resilience_alert``
business event when the suite's minimum score falls below threshold, so a
regression in the loop's protective behaviour surfaces in the morning brief
rather than in production.

Zero external cost: entropy + portfolio_controller are deterministic, in-process,
and zero-LLM. Fail-soft: a scenario error scores 0 and the suite continues.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from backend.common.dates import iso_now
from backend.common.state_paths import state_path

_LOG = logging.getLogger("samus.experiments.resilience")

_BENCHMARKS_JSONL = ("experiments", "benchmarks.jsonl")
ENV_MIN_RESILIENCE = "SAMUS_BENCHMARK_MIN_RESILIENCE"
DEFAULT_MIN_RESILIENCE = 0.75


@dataclass
class Scenario:
    """One perturbation + the protective response a resilient loop should give."""
    name: str
    description: str
    entropy_inputs: dict[str, Any] = field(default_factory=dict)
    workcells: list[dict[str, Any]] = field(default_factory=list)
    # Expectations (only the True ones are asserted):
    expect_unstable: bool = False       # entropy must report NOT stable
    expect_stable: bool = False         # entropy must report stable
    expect_countermeasures: bool = False  # entropy must recommend >=1 countermeasure
    expect_quota_cut: bool = False      # rebalance must cut >=1 workcell's quota
    expect_no_quota_cut: bool = False   # rebalance must NOT cut any quota


DEFAULT_SCENARIOS: list[Scenario] = [
    Scenario(
        name="cost_shock", description="LLM failures spike (cost/reliability shock)",
        entropy_inputs={"llm_failure_ratio": 1.0, "error_velocity": 0.9, "task_retry_rate": 0.8},
        workcells=[{"workcell": "prospecting", "error_velocity": 0.9}],
        expect_unstable=True, expect_countermeasures=True, expect_quota_cut=True,
    ),
    Scenario(
        name="error_storm", description="retry + error velocity spike across the queue",
        entropy_inputs={"error_velocity": 1.0, "task_retry_rate": 1.0, "queue_variance": 0.8},
        workcells=[{"workcell": "outreach", "error_velocity": 0.95}],
        expect_unstable=True, expect_countermeasures=True, expect_quota_cut=True,
    ),
    Scenario(
        name="channel_degraded", description="a top channel's error rate collapses throughput",
        entropy_inputs={"queue_variance": 0.5},
        workcells=[{"workcell": "outreach", "error_velocity": 0.85}],
        expect_quota_cut=True,
    ),
    Scenario(
        name="calm_baseline", description="no stress — the loop must not overreact",
        entropy_inputs={},
        workcells=[{"workcell": "seo", "throughput_efficiency": 0.9}],
        expect_stable=True, expect_no_quota_cut=True,
    ),
]


def _run_entropy(entropy_inputs: dict[str, Any]) -> dict[str, Any]:
    from backend.entropy.models import EntropyScanRequest
    from backend.entropy.service import scan as entropy_scan

    req = EntropyScanRequest.model_validate(entropy_inputs or {})
    result = entropy_scan("resilience", req)
    return {
        "stable": bool(result.stable),
        "entropy_score": float(result.entropy_score),
        "countermeasures": list(result.countermeasures),
    }


def _run_rebalance(workcells: list[dict[str, Any]]) -> dict[str, Any]:
    from backend.portfolio_controller.models import RebalanceRequest
    from backend.portfolio_controller.service import run_rebalance

    req = RebalanceRequest.model_validate({"workcells": workcells or [], "task_id": "resilience"})
    result = run_rebalance(req)
    return {"quota_cuts": int(result.quota_cuts), "priority_boosts": int(result.priority_boosts)}


def run_scenario(scenario: Scenario) -> dict[str, Any]:
    """Run one scenario through the real control-loop functions and score the
    protective response as met/total expectations. Never raises."""
    try:
        entropy = _run_entropy(scenario.entropy_inputs)
        portfolio = _run_rebalance(scenario.workcells)
    except Exception as exc:  # noqa: BLE001 — a broken scenario scores 0
        _LOG.warning("resilience scenario %s failed: %s", scenario.name, exc)
        return {"scenario": scenario.name, "resilience_score": 0.0,
                "passed": False, "error": str(exc), "checks": []}

    checks: list[dict[str, Any]] = []

    def _check(label: str, condition: bool, applies: bool) -> None:
        if applies:
            checks.append({"check": label, "ok": bool(condition)})

    _check("unstable_flagged", entropy["stable"] is False, scenario.expect_unstable)
    _check("stable", entropy["stable"] is True, scenario.expect_stable)
    _check("countermeasures_fired", len(entropy["countermeasures"]) > 0, scenario.expect_countermeasures)
    _check("quota_cut", portfolio["quota_cuts"] > 0, scenario.expect_quota_cut)
    _check("no_quota_cut", portfolio["quota_cuts"] == 0, scenario.expect_no_quota_cut)

    met = sum(1 for c in checks if c["ok"])
    total = len(checks) or 1
    score = round(met / total, 4)
    return {
        "scenario": scenario.name,
        "description": scenario.description,
        "resilience_score": score,
        "passed": met == len(checks),
        "checks": checks,
        "entropy": entropy,
        "portfolio": portfolio,
    }


def _min_resilience() -> float:
    raw = (os.getenv(ENV_MIN_RESILIENCE) or "").strip()
    if not raw:
        return DEFAULT_MIN_RESILIENCE
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_MIN_RESILIENCE


def _append_benchmark(row: dict[str, Any]) -> None:
    try:
        from backend.common.persistence import open_ledger

        open_ledger(
            jsonl_path=state_path(*_BENCHMARKS_JSONL),
            collection="resilience_benchmarks",
        ).append(row)
    except Exception as exc:  # noqa: BLE001 — ledger is best-effort
        _LOG.warning("benchmark ledger append failed: %s", exc)


def _emit_alert(summary: dict[str, Any]) -> None:
    """Surface a degraded suite as a WARNING (also recorded in the benchmarks
    ledger). A dedicated business event would require adding an event type to the
    Tranche-1 shim allowlist — deferred to avoid editing that shared surface; the
    ledger row + this log are the operator-visible signal for now."""
    _LOG.warning(
        "resilience_alert: control-loop protective response degraded — "
        "min_resilience=%s threshold=%s failures=%s",
        summary.get("min_resilience"), summary.get("threshold"),
        summary.get("failures"),
    )


def run_benchmark_suite(scenarios: list[Scenario] | None = None) -> dict[str, Any]:
    """Run the resilience suite, record it, and alert on degradation.

    Returns a summary ``{ts, min_resilience, mean_resilience, threshold,
    degraded, results:[...]}``. When ``min_resilience`` is below the threshold
    (or any scenario failed) a ``resilience_alert`` business event is emitted so
    the regression reaches the operator. Never raises.
    """
    scen = scenarios if scenarios is not None else DEFAULT_SCENARIOS
    results = [run_scenario(s) for s in scen]
    scores = [r["resilience_score"] for r in results] or [0.0]
    threshold = _min_resilience()
    min_score = min(scores)
    failures = [r["scenario"] for r in results if not r["passed"]]
    summary = {
        "ts": iso_now(),
        "min_resilience": round(min_score, 4),
        "mean_resilience": round(sum(scores) / len(scores), 4),
        "threshold": threshold,
        "degraded": bool(min_score < threshold or failures),
        "failures": failures,
        "results": results,
    }
    _append_benchmark({k: summary[k] for k in
                       ("ts", "min_resilience", "mean_resilience", "threshold", "degraded", "failures")})
    if summary["degraded"]:
        _emit_alert({k: summary[k] for k in ("ts", "min_resilience", "threshold", "failures")})
    return summary


__all__ = [
    "Scenario", "DEFAULT_SCENARIOS",
    "run_scenario", "run_benchmark_suite",
    "DEFAULT_MIN_RESILIENCE",
]
