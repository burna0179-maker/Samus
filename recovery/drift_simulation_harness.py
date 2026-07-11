#!/usr/bin/env python3
"""
Drift Simulation Harness (DSH) — adversarial drift injection for governance testing
Source: ChatGPT recovery chat 45

Canonical relationship:
- [PAIRS WITH] governance_drift_detection.py (validates GDD sensitivity)
- [PAIRS WITH] system_invariant_defense_scenarios.md (chat 29 — adversarial scenarios)
- [EXPANDS §15 acceptance gates] runtime sensitivity validation

Purpose:
  Inject controlled perturbations into a sandboxed clone of the system to validate:
    - governance drift detection sensitivity
    - false positive rate
    - containment reliability
    - rollback determinism
    - recovery latency

CRITICAL: The harness must NEVER mutate production directly.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class InjectionType(str, Enum):
    BEHAVIORAL = "behavioral"
    HEURISTIC = "heuristic"
    SCHEMA = "schema"
    POLICY = "policy"
    RESOURCE = "resource"


class InjectionIntensity(str, Enum):
    MICRO = "1_micro"        # 5-10%
    MILD = "2_mild"          # 10-20%
    MODERATE = "3_moderate"  # 20-35%
    SEVERE = "4_severe"      # 35-60%
    CRITICAL = "5_critical"  # > 60%


INTENSITY_RANGES = {
    InjectionIntensity.MICRO: (0.05, 0.10),
    InjectionIntensity.MILD: (0.10, 0.20),
    InjectionIntensity.MODERATE: (0.20, 0.35),
    InjectionIntensity.SEVERE: (0.35, 0.60),
    InjectionIntensity.CRITICAL: (0.60, 0.95),
}


@dataclass
class InjectionStep:
    type: InjectionType
    intensity: InjectionIntensity
    duration_sec: int = 60
    target: Optional[str] = None    # specific heuristic / field / endpoint


@dataclass
class InjectionProfile:
    simulation_id: str
    random_seed: int
    timeline: List[InjectionStep] = field(default_factory=list)
    description: str = ""


@dataclass
class SimulationResult:
    simulation_id: str
    drift_type: str
    injection_level: str
    detection_time_sec: Optional[float] = None
    containment_time_sec: Optional[float] = None
    rollback_success: bool = False
    data_integrity_preserved: bool = False
    governance_response_valid: bool = False
    overall_pass: bool = False
    false_positive: bool = False
    false_negative: bool = False
    risk_classification: str = "unclassified"
    notes: List[str] = field(default_factory=list)


# ----- Injection functions (sandbox-side state mutators) -----
def inject_behavioral_drift(decision_dist: Dict[str, float], delta_bias: float) -> Dict[str, float]:
    """Bias decision sampling — preserve normalization."""
    biased = {k: v * (1 + delta_bias) if i == 0 else v
              for i, (k, v) in enumerate(decision_dist.items())}
    total = sum(biased.values())
    return {k: v / total for k, v in biased.items()}


def inject_heuristic_drift(weights: List[float], target_idx: int, delta: float) -> List[float]:
    """Inflate one heuristic weight, preserve total magnitude shape."""
    out = list(weights)
    out[target_idx] = max(0.0, min(5.0, out[target_idx] * (1 + delta)))
    return out


def inject_schema_drift(schema: Dict[str, Any], mode: str = "add_phantom_field") -> Dict[str, Any]:
    """Add phantom field / remove required / change type / version mismatch."""
    out = dict(schema)
    if mode == "add_phantom_field":
        out[f"_phantom_{int(time.time())}"] = "injected_field"
    elif mode == "remove_required" and out:
        first_key = next(iter(out))
        out.pop(first_key)
    elif mode == "version_mismatch":
        out["_schema_version"] = "999.999.999"
    return out


def inject_policy_drift(enforcement_rate: float, delta: float) -> float:
    """Reduce enforcement frequency."""
    return max(0.0, enforcement_rate * (1 - delta))


def inject_resource_drift(current_resources: Dict[str, float], dimension: str,
                          delta_multiplier: float) -> Dict[str, float]:
    """Inflate one resource dimension (CPU / memory / latency)."""
    out = dict(current_resources)
    if dimension in out:
        out[dimension] = out[dimension] * (1 + delta_multiplier)
    return out


# ----- Harness orchestrator -----
class DriftSimulationHarness:
    """
    Pure / sandbox-side. Records metrics, never touches production state.

    Required isolation:
      - container or VM clone
      - CPU/memory caps
      - deterministic random seed
      - signed result output
      - append-only reporting
    """

    def __init__(self, sandbox_state_cloner: Callable, drift_detector: Any,
                 governance_responder: Callable):
        self.clone_baseline = sandbox_state_cloner
        self.drift_detector = drift_detector
        self.responder = governance_responder

    def run(self, profile: InjectionProfile) -> SimulationResult:
        random.seed(profile.random_seed)
        state = self.clone_baseline()
        result = SimulationResult(
            simulation_id=profile.simulation_id,
            drift_type=",".join(s.type.value for s in profile.timeline),
            injection_level=",".join(s.intensity.value for s in profile.timeline),
        )

        start_ts = time.time()
        first_detection_ts = None
        first_containment_ts = None

        for step in profile.timeline:
            state = self._inject_step(state, step)
            report = self.drift_detector.evaluate(*state["dimension_scores"])

            if first_detection_ts is None and report.severity.value >= "2_moderate":
                first_detection_ts = time.time() - start_ts

            if report.severity.value >= "3_structural_risk":
                actions = self.responder(report.severity)
                if "freeze_mutation_layer" in actions and first_containment_ts is None:
                    first_containment_ts = time.time() - start_ts

        result.detection_time_sec = first_detection_ts
        result.containment_time_sec = first_containment_ts
        result.rollback_success = state.get("rollback_ok", False)
        result.data_integrity_preserved = state.get("integrity_ok", False)
        result.governance_response_valid = first_containment_ts is not None
        result.overall_pass = (
            result.detection_time_sec is not None
            and result.governance_response_valid
            and result.data_integrity_preserved
        )
        return result

    def _inject_step(self, state: Dict[str, Any], step: InjectionStep) -> Dict[str, Any]:
        lo, hi = INTENSITY_RANGES[step.intensity]
        delta = random.uniform(lo, hi)
        if step.type == InjectionType.BEHAVIORAL:
            state["decisions"] = inject_behavioral_drift(state["decisions"], delta)
        elif step.type == InjectionType.HEURISTIC:
            state["heuristics"] = inject_heuristic_drift(state["heuristics"], 0, delta)
        elif step.type == InjectionType.SCHEMA:
            state["schema"] = inject_schema_drift(state["schema"])
        elif step.type == InjectionType.POLICY:
            state["enforcement_rate"] = inject_policy_drift(state["enforcement_rate"], delta)
        elif step.type == InjectionType.RESOURCE:
            state["resources"] = inject_resource_drift(state["resources"], "latency", delta)
        return state


# ----- Compound scenarios (chat 45) -----
COMPOUND_SCENARIOS = {
    "long_run_slow_drift": "1-2% drift over hours — test subtle detection sensitivity",
    "sudden_shock": "60% spike instantly — test immediate containment",
    "oscillating_drift": "alternate safe/unsafe zones — test FP handling",
    "silent_structural_drift": "schema mutation without version change",
    "compound_5_domain": "all 5 drift types simultaneously",
}


# Calibration target (chat 45):
#   Minimize: false_positive_rate + false_negative_rate
#   Subject to: mean_time_to_detection < target_threshold
