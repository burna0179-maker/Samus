"""Queue-admission gate for the signal_filter workcell.

Reduces a :class:`~backend.signal_filter.scoring.ProspectSignal` to a single
weighted viability score and a binary admission decision. A prospect is
admitted to the downstream queue only when its weighted score clears
:data:`ADMISSION_THRESHOLD`.

The weights and threshold are module-level constants so an operator can tune
the gate without touching logic. Five axes carry weight; ``social_activity``
and ``revenue_estimate`` are intentionally *not* weighted — social presence
is noisy and the revenue proxy is neutral whenever paid firmographics are
absent (the local-first default), so neither should move the admission line.
"""

from __future__ import annotations

from backend.signal_filter.scoring import ProspectSignal

# Per-axis weights. The five weighted axes sum to exactly 1.0.
WEIGHT_DOMAIN_HEALTH: float = 0.20
WEIGHT_SEO_SCORE: float = 0.25
WEIGHT_CONTACTABILITY: float = 0.25
WEIGHT_REVIEW_VELOCITY: float = 0.10
WEIGHT_INFRASTRUCTURE_MATURITY: float = 0.20

# A prospect is admitted when its weighted score is >= this value.
ADMISSION_THRESHOLD: float = 0.62


def weighted_score(signal: ProspectSignal) -> float:
    """Compute the weighted viability score for ``signal``.

    Pure function. Because every :class:`ProspectSignal` axis is clamped to
    ``[0.0, 1.0]`` and the weights sum to 1.0, the result is itself in
    ``[0.0, 1.0]``.
    """
    return (
        signal.domain_health * WEIGHT_DOMAIN_HEALTH
        + signal.seo_score * WEIGHT_SEO_SCORE
        + signal.contactability * WEIGHT_CONTACTABILITY
        + signal.review_velocity * WEIGHT_REVIEW_VELOCITY
        + signal.infrastructure_maturity * WEIGHT_INFRASTRUCTURE_MATURITY
    )


def should_enqueue(signal: ProspectSignal) -> bool:
    """Return ``True`` iff the prospect clears the admission threshold.

    The admission decision: ``weighted >= ADMISSION_THRESHOLD`` (0.62).
    """
    return weighted_score(signal) >= ADMISSION_THRESHOLD


__all__ = [
    "ADMISSION_THRESHOLD",
    "WEIGHT_DOMAIN_HEALTH",
    "WEIGHT_SEO_SCORE",
    "WEIGHT_CONTACTABILITY",
    "WEIGHT_REVIEW_VELOCITY",
    "WEIGHT_INFRASTRUCTURE_MATURITY",
    "weighted_score",
    "should_enqueue",
]
