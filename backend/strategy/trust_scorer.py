"""Trust-weighted autonomy scorer.

Pure-function port of the "Trust-weighted autonomy" primitive defined in
`Samus/recovery/agent_civilization_blueprint.md`, section
"Trust-weighted autonomy (from chat 44)".

Formula (verbatim from the blueprint)::

    Trust = (SuccessRate        * 0.4)
          + (PolicyCompliance   * 0.3)
          + (ResourceEfficiency * 0.2)
          + (StabilityScore     * 0.1)

The composite score (0.0 - 1.0) maps to one of four access tiers:

==================  =========================  ============================
Score range         Tier (``AccessTier``)      Access description
==================  =========================  ============================
``0.0  .. 0.2``     ``sandbox``                Sandbox only
``0.2+ .. 0.5``     ``restricted_tools``       Restricted tools
``0.5+ .. 0.8``     ``multi_agent``            Multi-agent collaboration
``0.8+ .. 1.0``     ``autonomous``             Autonomous execution
==================  =========================  ============================

Boundary rule
-------------
The upper bound of each tier is *inclusive* and the lower bound of the next
tier is *exclusive*. Concretely:

* ``score == 0.20``         -> ``sandbox``
* ``score == 0.2001`` (>0.2) -> ``restricted_tools``
* ``score == 0.50``         -> ``restricted_tools``
* ``score == 0.5001``        -> ``multi_agent``
* ``score == 0.80``         -> ``multi_agent``
* ``score == 0.8001``        -> ``autonomous``

This module performs *no enforcement* of its own. Downstream gates consume
``TrustResult.tier`` to decide what an agent may do. The module also performs
no I/O, no async work, and has no third-party dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Final


class AccessTier(str, Enum):
    """Privilege tier an agent has earned at its current trust score.

    The ordering ``SANDBOX < RESTRICTED_TOOLS < MULTI_AGENT < AUTONOMOUS``
    is used by :func:`can_access` to compare a held tier against a required
    one.
    """

    SANDBOX = "sandbox"
    RESTRICTED_TOOLS = "restricted_tools"
    MULTI_AGENT = "multi_agent"
    AUTONOMOUS = "autonomous"


# Lower-is-weaker ordering used by can_access().
_TIER_RANK: Final[dict[AccessTier, int]] = {
    AccessTier.SANDBOX: 0,
    AccessTier.RESTRICTED_TOOLS: 1,
    AccessTier.MULTI_AGENT: 2,
    AccessTier.AUTONOMOUS: 3,
}


# Weights from the blueprint. Kept as a module-level constant so callers can
# document them and tests can monkeypatch alternative weightings. The sum is
# asserted at import time so future edits cannot silently drift.
WEIGHTS: Final[dict[str, float]] = {
    "success_rate": 0.4,
    "policy_compliance": 0.3,
    "resource_efficiency": 0.2,
    "stability_score": 0.1,
}

# Use a small tolerance — float arithmetic on 0.4+0.3+0.2+0.1 isn't exact.
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, (
    f"trust_scorer.WEIGHTS must sum to 1.0, got {sum(WEIGHTS.values())!r}"
)


def _validate_unit_interval(name: str, value: float) -> None:
    """Raise ValueError if ``value`` is not in the closed interval ``[0.0, 1.0]``.

    The field ``name`` is embedded in the error message so callers (and tests)
    can pinpoint which input was bad.
    """

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be a real number in [0.0, 1.0]; got {value!r}")
    # NaN compares false to itself; reject explicitly.
    if value != value:  # noqa: PLR0124 - NaN check
        raise ValueError(f"{name} must be in [0.0, 1.0]; got NaN")
    if value < 0.0 or value > 1.0:
        raise ValueError(f"{name} must be in [0.0, 1.0]; got {value!r}")


@dataclass(frozen=True)
class TrustInputs:
    """The four signals that feed the composite trust score.

    All four values must lie in the closed interval ``[0.0, 1.0]``. Out-of-
    range or non-numeric inputs raise :class:`ValueError` at construction
    time with the offending field name in the message.
    """

    success_rate: float
    policy_compliance: float
    resource_efficiency: float
    stability_score: float

    def __post_init__(self) -> None:
        _validate_unit_interval("success_rate", self.success_rate)
        _validate_unit_interval("policy_compliance", self.policy_compliance)
        _validate_unit_interval("resource_efficiency", self.resource_efficiency)
        _validate_unit_interval("stability_score", self.stability_score)


@dataclass(frozen=True)
class TrustResult:
    """Result of :func:`score_trust`.

    Attributes
    ----------
    score:
        The composite trust score, rounded to 4 decimal places to avoid
        float-printing drift in logs / serialised audit trails.
    tier:
        The :class:`AccessTier` corresponding to ``score`` per the boundary
        rule documented in the module docstring.
    breakdown:
        Per-component weighted contributions, keyed by
        ``"<field>_weighted"``. The values sum to the unrounded composite
        (and therefore to ``score`` within float epsilon).
    """

    score: float
    tier: AccessTier
    breakdown: dict[str, float] = field(default_factory=dict)


def tier_for_score(score: float) -> AccessTier:
    """Map a composite trust ``score`` to its :class:`AccessTier`.

    Uses inclusive upper bounds: see module docstring boundary rule.
    """

    if score < 0.0 or score > 1.0:
        raise ValueError(f"score must be in [0.0, 1.0]; got {score!r}")
    if score <= 0.2:
        return AccessTier.SANDBOX
    if score <= 0.5:
        return AccessTier.RESTRICTED_TOOLS
    if score <= 0.8:
        return AccessTier.MULTI_AGENT
    return AccessTier.AUTONOMOUS


def score_trust(inputs: TrustInputs) -> TrustResult:
    """Compute the composite trust score and resolved access tier.

    The score is rounded to 4 decimal places. ``breakdown`` carries the
    per-component weighted contributions for transparency in audit logs.
    """

    contributions: dict[str, float] = {
        "success_rate_weighted": inputs.success_rate * WEIGHTS["success_rate"],
        "policy_compliance_weighted": inputs.policy_compliance * WEIGHTS["policy_compliance"],
        "resource_efficiency_weighted": inputs.resource_efficiency * WEIGHTS["resource_efficiency"],
        "stability_score_weighted": inputs.stability_score * WEIGHTS["stability_score"],
    }
    raw_score = sum(contributions.values())
    # Clamp into [0,1] before rounding to guard against float drift pushing a
    # legitimately-1.0 input to 1.0000000002.
    clamped = max(0.0, min(1.0, raw_score))
    score = round(clamped, 4)
    return TrustResult(
        score=score,
        tier=tier_for_score(score),
        breakdown=contributions,
    )


def can_access(tier_required: AccessTier, agent_tier: AccessTier) -> bool:
    """Return ``True`` if ``agent_tier`` is at least as privileged as
    ``tier_required``.

    Ordering (weakest to strongest):
    ``sandbox < restricted_tools < multi_agent < autonomous``.
    """

    return _TIER_RANK[agent_tier] >= _TIER_RANK[tier_required]


__all__ = [
    "AccessTier",
    "TrustInputs",
    "TrustResult",
    "WEIGHTS",
    "score_trust",
    "tier_for_score",
    "can_access",
]
