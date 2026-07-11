#!/usr/bin/env python3
"""
Governance Drift Detection (GDD) — mathematical scoring framework
Source: ChatGPT recovery chat 45

Canonical relationship:
- [EXPANDS §6 security_extended.anomaly] formal drift detection layer
- [EXPANDS §8 mutation plane] distinguishes approved evolution from unauthorized drift
- [PAIRS WITH] three_plane_authority_model.md (Plane 0 owns drift baseline)
- [PAIRS WITH] system_invariant_defense_scenarios.md (chat 29 — drift triggers SYS_COMPONENT_DESYNC)

Drift ≠ failure. Drift = deviation from authorized constraints.

5 drift categories:
  1. Behavioral — decision pattern shifts (KL/JSD divergence)
  2. Heuristic — weight distribution shifts (cosine + L2)
  3. Schema — runtime data structure deviation (structural hash + JSD)
  4. Policy — enforcement degradation (rate deviation)
  5. Resource — CPU/memory/latency outliers (z-score)

Composite: GDS = w_B*B + w_H*H + w_S*S + w_P*P + w_R*R  (Σw = 1)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


# ----- Severity ladder -----
class DriftSeverity(str, Enum):
    STABLE = "0_stable"                       # GDS 0.00-0.15
    MINOR = "1_minor"                         # 0.15-0.30
    MODERATE = "2_moderate"                   # 0.30-0.50
    STRUCTURAL_RISK = "3_structural_risk"     # 0.50-0.70
    GOVERNANCE_THREAT = "4_governance_threat" # 0.70-0.85
    CONTAINMENT_REQUIRED = "5_containment"    # > 0.85


SEVERITY_THRESHOLDS = [
    (0.15, DriftSeverity.STABLE),
    (0.30, DriftSeverity.MINOR),
    (0.50, DriftSeverity.MODERATE),
    (0.70, DriftSeverity.STRUCTURAL_RISK),
    (0.85, DriftSeverity.GOVERNANCE_THREAT),
    (float("inf"), DriftSeverity.CONTAINMENT_REQUIRED),
]


# ----- Mathematical primitives -----
def kl_divergence(p: Dict[str, float], q: Dict[str, float], eps: float = 1e-12) -> float:
    """KL(p || q) — sum over keys in p, treating missing q values as eps."""
    return sum(p[k] * math.log(p[k] / max(q.get(k, eps), eps)) for k in p if p[k] > 0)


def js_divergence(p: Dict[str, float], q: Dict[str, float]) -> float:
    """Jensen-Shannon — symmetric, bounded by ln(2)."""
    keys = set(p) | set(q)
    m = {k: 0.5 * (p.get(k, 0.0) + q.get(k, 0.0)) for k in keys}
    return 0.5 * kl_divergence(p, m) + 0.5 * kl_divergence(q, m)


def js_normalized(p: Dict[str, float], q: Dict[str, float]) -> float:
    """Returns JS divergence normalized to [0, 1]."""
    return js_divergence(p, q) / math.log(2)


def cosine_distance(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 1.0
    return 1.0 - (dot / (na * nb))


def l2_distance(a: List[float], b: List[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


# ----- Per-dimension scoring -----
def behavioral_drift(current_dist: Dict[str, float], baseline_dist: Dict[str, float]) -> float:
    return min(1.0, js_normalized(current_dist, baseline_dist))


def heuristic_drift(current: List[float], baseline: List[float], alpha: float = 0.7) -> float:
    cos = cosine_distance(current, baseline)
    l2 = l2_distance(current, baseline)
    # Normalize L2 by max possible (≈ √n * 2.0 for clamped [0,1] vectors)
    l2_norm = min(1.0, l2 / (math.sqrt(len(current)) * 2.0))
    return alpha * cos + (1 - alpha) * l2_norm


def schema_drift(current_hash: str, baseline_hash: str,
                 current_freq: Dict[str, float], baseline_freq: Dict[str, float]) -> float:
    structural_flag = 1.0 if current_hash != baseline_hash else 0.0
    statistical = js_normalized(current_freq, baseline_freq)
    return max(structural_flag, statistical)


def policy_drift(current_rate: float, baseline_rate: float,
                 current_overrides: int, baseline_overrides: int) -> float:
    eps = 1e-6
    rate_score = abs(current_rate - baseline_rate) / max(baseline_rate, eps)
    override_inflation = current_overrides / max(baseline_overrides, 1)
    override_score = min(1.0, max(0.0, (override_inflation - 1.0) / 4.0))
    return min(1.0, max(rate_score, override_score))


def resource_drift(current: List[float], mean: List[float], stddev: List[float]) -> float:
    """Max-pooled z-score across dimensions, mapped through sigmoid."""
    scores = []
    for i, x in enumerate(current):
        if stddev[i] == 0:
            scores.append(0.0)
            continue
        z = abs(x - mean[i]) / stddev[i]
        scores.append(sigmoid(z) - 0.5)   # center sigmoid at 0
    return min(1.0, max(scores) * 2.0)    # rescale


# ----- Composite GDS -----
@dataclass
class DriftWeights:
    behavioral: float = 0.25
    heuristic: float = 0.20
    schema: float = 0.25
    policy: float = 0.20
    resource: float = 0.10


@dataclass
class DriftReport:
    behavioral: float
    heuristic: float
    schema: float
    policy: float
    resource: float
    composite: float
    severity: DriftSeverity
    smoothed: float = 0.0


class DriftDetector:
    """Pure-function composite GDS calculator with temporal smoothing."""

    def __init__(self, weights: Optional[DriftWeights] = None, ewma_lambda: float = 0.3):
        self.weights = weights or DriftWeights()
        self.ewma_lambda = ewma_lambda
        self._prev_smoothed: float = 0.0

    def evaluate(self, b: float, h: float, s: float, p: float, r: float) -> DriftReport:
        w = self.weights
        composite = (w.behavioral * b + w.heuristic * h + w.schema * s
                     + w.policy * p + w.resource * r)
        smoothed = (1 - self.ewma_lambda) * self._prev_smoothed + self.ewma_lambda * composite
        self._prev_smoothed = smoothed
        severity = self._classify(smoothed)
        return DriftReport(
            behavioral=b, heuristic=h, schema=s, policy=p, resource=r,
            composite=composite, smoothed=smoothed, severity=severity,
        )

    @staticmethod
    def _classify(score: float) -> DriftSeverity:
        for threshold, severity in SEVERITY_THRESHOLDS:
            if score < threshold:
                return severity
        return DriftSeverity.CONTAINMENT_REQUIRED


# ----- Severity → Governance response -----
def governance_response_for(severity: DriftSeverity) -> List[str]:
    """Returns ordered list of required actions."""
    matrix = {
        DriftSeverity.STABLE: [],
        DriftSeverity.MINOR: ["log", "increase_monitoring_frequency"],
        DriftSeverity.MODERATE: ["restrict_heuristic_adaptation_range", "alert_governance"],
        DriftSeverity.STRUCTURAL_RISK: [
            "freeze_mutation_layer",
            "require_simulation_replay",
            "audit_last_approved_change",
        ],
        DriftSeverity.GOVERNANCE_THREAT: [
            "enter_controlled_mode",
            "lock_self_modification",
            "snapshot_state",
        ],
        DriftSeverity.CONTAINMENT_REQUIRED: [
            "full_containment",
            "rollback_to_last_stable_baseline",
            "disable_adaptive_learning",
            "operator_alert",
        ],
    }
    return matrix.get(severity, [])


# Drift vs Evolution differentiation:
#   Evolution = approved mutation_id + simulated + version_increment
#   Drift     = no approved mutation_id + silent + no version_increment + gradual deviation
# System MUST enforce: legitimate evolution leaves an audit trail.
