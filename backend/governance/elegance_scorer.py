"""ElegancePlan scorer — plan-complexity-vs-projected-impact.

A plan is "elegant" when it produces a high projected impact for low complexity.
Used by PDC composite + autonomy loop to compare candidate plans before commit,
and by Darwin's mutation-fitness reads (read-only) when comparing variants of a
generated capability.

Scoring is deterministic and pure-stdlib. No LLM. No I/O.

Plan shape (caller's responsibility — duck-typed dict):

    {
        "steps": [ {"kind": "...", "novel_capability": bool, ...}, ... ],
        "branch_count": int,                  # decision branches in the plan
        "llm_calls_required": int,
        "external_api_calls": int,
        "novel_capabilities_required": int,   # capabilities not yet in registry
        "projected_impact": float,            # 0..1 caller-supplied estimate
        "reversibility": float,               # 0..1 caller-supplied estimate
                                              # (1.0 = fully reversible)
    }

Any missing field defaults conservatively (treats unknown complexity as high,
unknown impact as low) so a half-filled plan never scores artificially well.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# Per-component complexity weights — sum is the raw complexity numerator.
_COMPLEXITY_WEIGHT: dict[str, float] = {
    "step_count": 1.0,
    "branch_count": 1.5,
    "llm_calls_required": 2.0,           # LLM use is expensive + non-deterministic
    "external_api_calls": 1.0,
    "novel_capabilities_required": 3.0,  # building new capability is the heaviest
}

# Complexity-saturation point: beyond this raw complexity, denominator caps.
_COMPLEXITY_SATURATION = 30.0


@dataclass
class ElegancePlan:
    """Result of scoring one plan."""

    score: float                          # 0.0..1.0  (higher is more elegant)
    complexity: float                     # raw weighted complexity (pre-clamp)
    normalized_complexity: float          # 0.0..1.0 after clamp
    projected_impact: float               # 0.0..1.0 echoed back
    reversibility: float                  # 0.0..1.0 echoed back
    components: dict[str, int] = field(default_factory=dict)
    grade: str = "A"
    rationale: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "complexity": round(self.complexity, 4),
            "normalized_complexity": round(self.normalized_complexity, 4),
            "projected_impact": round(self.projected_impact, 4),
            "reversibility": round(self.reversibility, 4),
            "components": dict(self.components),
            "grade": self.grade,
            "rationale": list(self.rationale),
        }


def _grade(score: float) -> str:
    if score >= 0.8:
        return "A"
    if score >= 0.6:
        return "B"
    if score >= 0.4:
        return "C"
    if score >= 0.2:
        return "D"
    return "F"


def _clamp01(x: float) -> float:
    if math.isnan(x) or math.isinf(x):
        return 0.0
    return max(0.0, min(1.0, x))


def score_elegance(plan: dict[str, Any]) -> ElegancePlan:
    """Score a candidate plan for elegance.

    Returns an ElegancePlan with score in [0,1]. Higher is better. A plan with
    very high complexity but only modest impact will fall to D/F regardless of
    reversibility; a small reversible plan with strong impact will score A.

    The formula:

        elegance = impact * (1 - normalized_complexity) * (0.5 + 0.5 * reversibility)

    so:
      - reversibility never zeros the score out (a half-bonus floor at 0.5)
      - impact and (1 - complexity) multiply, so either being near zero is fatal
      - the score is bounded [0,1]
    """
    steps = plan.get("steps", []) or []
    step_count = len(steps) if isinstance(steps, (list, tuple)) else 0

    components = {
        "step_count": step_count,
        "branch_count": int(plan.get("branch_count", 0)),
        "llm_calls_required": int(plan.get("llm_calls_required", 0)),
        "external_api_calls": int(plan.get("external_api_calls", 0)),
        "novel_capabilities_required": int(plan.get("novel_capabilities_required", 0)),
    }

    complexity = 0.0
    for name, count in components.items():
        complexity += _COMPLEXITY_WEIGHT[name] * max(0, count)

    normalized = min(1.0, complexity / _COMPLEXITY_SATURATION)
    impact = _clamp01(float(plan.get("projected_impact", 0.0)))
    reversibility = _clamp01(float(plan.get("reversibility", 0.5)))

    rev_factor = 0.5 + 0.5 * reversibility
    score = impact * (1.0 - normalized) * rev_factor

    rationale: list[str] = []
    if impact == 0.0:
        rationale.append("projected_impact is 0 — elegance is 0 regardless of complexity")
    if normalized >= 1.0:
        rationale.append("complexity is at saturation — elegance is 0 regardless of impact")
    if components["novel_capabilities_required"] > 0:
        rationale.append(
            f"plan requires {components['novel_capabilities_required']} novel "
            "capability(ies); each is weighted 3x"
        )
    if components["llm_calls_required"] > 1:
        rationale.append(
            f"{components['llm_calls_required']} LLM calls — exceeds the "
            "zero-or-one-LLM-call-per-job ceiling (axiom.samus.daily_llm_cap)"
        )

    return ElegancePlan(
        score=_clamp01(score),
        complexity=complexity,
        normalized_complexity=normalized,
        projected_impact=impact,
        reversibility=reversibility,
        components=components,
        grade=_grade(_clamp01(score)),
        rationale=rationale,
    )
