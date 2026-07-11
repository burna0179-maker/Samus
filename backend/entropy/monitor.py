"""Orchestrator-side entropy monitor.

Assembles :class:`EntropyInputs` from supplied data, computes the entropy
score, derives countermeasures, and returns a structured stability report.
Pure deterministic logic — no I/O, no LLM.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .controller import recommend_countermeasures
from .metrics import EntropyInputs, compute_entropy_score

# A score at or above this is considered systemically unstable.
INSTABILITY_THRESHOLD: float = 0.50


@dataclass(frozen=True)
class StabilityReport:
    """Structured outcome of one entropy scan."""

    entropy_score: float
    countermeasures: list[str] = field(default_factory=list)
    stable: bool = True
    inputs: EntropyInputs = field(default_factory=EntropyInputs)


def run_monitor(
    *,
    queue_variance: float = 0.0,
    error_velocity: float = 0.0,
    task_retry_rate: float = 0.0,
    llm_failure_ratio: float = 0.0,
    efficiency_ema: float | None = None,
    token_success_ratio: float | None = None,
    template_success_rate: float | None = None,
) -> StabilityReport:
    """Assemble inputs, score them, recommend countermeasures, report.

    Returns a :class:`StabilityReport`. ``stable`` is ``False`` once the
    entropy score reaches :data:`INSTABILITY_THRESHOLD`.
    """
    inputs = EntropyInputs(
        queue_variance=queue_variance,
        error_velocity=error_velocity,
        task_retry_rate=task_retry_rate,
        llm_failure_ratio=llm_failure_ratio,
    )
    score = compute_entropy_score(inputs)
    measures = recommend_countermeasures(
        inputs,
        efficiency_ema=efficiency_ema,
        token_success_ratio=token_success_ratio,
        template_success_rate=template_success_rate,
    )
    return StabilityReport(
        entropy_score=score,
        countermeasures=measures,
        stable=score < INSTABILITY_THRESHOLD,
        inputs=inputs,
    )


__all__ = ["StabilityReport", "run_monitor", "INSTABILITY_THRESHOLD"]
