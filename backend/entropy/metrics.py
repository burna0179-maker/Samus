"""Workcell-internal entropy math.

NOTE: this is *not* ``backend/common/metrics.py`` (Prometheus instruments).
This module owns the deterministic entropy-score computation only.

The score is a weighted sum of four normalised instability signals, each
expected in ``[0.0, 1.0]``. The weights sum to 1.0 so a well-formed input
maps to a score in ``[0.0, 1.0]``; the output is clamped regardless.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Weights ──────────────────────────────────────────────────────────────────
# Named constants — the contract fixes these exact values; they sum to 1.0.
WEIGHT_QUEUE_VARIANCE: float = 0.25
WEIGHT_ERROR_VELOCITY: float = 0.30
WEIGHT_TASK_RETRY_RATE: float = 0.20
WEIGHT_LLM_FAILURE_RATIO: float = 0.25

# Output clamp bounds.
ENTROPY_SCORE_MIN: float = 0.0
ENTROPY_SCORE_MAX: float = 1.0


@dataclass(frozen=True)
class EntropyInputs:
    """Normalised instability signals feeding the entropy score.

    Each field is expected in ``[0.0, 1.0]``:

    - ``queue_variance``    — spread/instability of queue depth over a window.
    - ``error_velocity``    — rate of new errors per unit time.
    - ``task_retry_rate``   — fraction of tasks requiring at least one retry.
    - ``llm_failure_ratio`` — fraction of LLM calls failing.
    """

    queue_variance: float = 0.0
    error_velocity: float = 0.0
    task_retry_rate: float = 0.0
    llm_failure_ratio: float = 0.0


def compute_entropy_score(inputs: EntropyInputs) -> float:
    """Return the deterministic entropy score for ``inputs``.

    ``entropy_score = queue_variance*0.25 + error_velocity*0.30
                      + task_retry_rate*0.20 + llm_failure_ratio*0.25``

    Pure and deterministic. The result is clamped to ``[0.0, 1.0]``.
    """
    raw = (
        inputs.queue_variance * WEIGHT_QUEUE_VARIANCE
        + inputs.error_velocity * WEIGHT_ERROR_VELOCITY
        + inputs.task_retry_rate * WEIGHT_TASK_RETRY_RATE
        + inputs.llm_failure_ratio * WEIGHT_LLM_FAILURE_RATIO
    )
    return max(ENTROPY_SCORE_MIN, min(ENTROPY_SCORE_MAX, raw))


__all__ = [
    "EntropyInputs",
    "compute_entropy_score",
    "WEIGHT_QUEUE_VARIANCE",
    "WEIGHT_ERROR_VELOCITY",
    "WEIGHT_TASK_RETRY_RATE",
    "WEIGHT_LLM_FAILURE_RATIO",
    "ENTROPY_SCORE_MIN",
    "ENTROPY_SCORE_MAX",
]
