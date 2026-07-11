"""Adaptive token-quota + priority-weight rebalancing.

The ``rebalance`` rule is intentionally tiny and fully deterministic — it is
the request-driven counterpart to strategy's expensive LLM proposal. For each
workcell:

  * if ``error_velocity > 0.4``      → halve its ``token_quota`` (suppress
    spend on a workcell that is burning calls into errors);
  * if ``throughput_efficiency > 0.8`` → boost its ``priority_weight`` ×1.3
    (lean the queue toward workcells that are actually converting work).

The two rules are independent — a workcell can take both, one, or neither.
The four magic numbers are named constants below so the policy is auditable
in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .signals import PortfolioSignals

__all__ = [
    "ERROR_VELOCITY_CUT_THRESHOLD",
    "TOKEN_QUOTA_CUT_FACTOR",
    "THROUGHPUT_BOOST_THRESHOLD",
    "PRIORITY_WEIGHT_BOOST_FACTOR",
    "WorkcellAllocation",
    "rebalance",
]

# --- policy constants -------------------------------------------------------
# error_velocity above this fraction triggers a token-quota cut.
ERROR_VELOCITY_CUT_THRESHOLD: float = 0.4
# multiplier applied to token_quota when the cut fires.
TOKEN_QUOTA_CUT_FACTOR: float = 0.5
# throughput_efficiency above this fraction triggers a priority boost.
THROUGHPUT_BOOST_THRESHOLD: float = 0.8
# multiplier applied to priority_weight when the boost fires.
PRIORITY_WEIGHT_BOOST_FACTOR: float = 1.3


@dataclass
class WorkcellAllocation:
    """One workcell's mutable allocation row.

    ``token_quota`` and ``priority_weight`` are the levers :func:`rebalance`
    adjusts; ``signals`` is the read-only feature vector the decision is
    derived from. ``quota_cut`` / ``priority_boosted`` are set by
    :func:`rebalance` so the caller (and the audit trail) can see *which*
    rule fired without re-deriving it.
    """

    workcell: str
    token_quota: float = 0.0
    priority_weight: float = 1.0
    signals: PortfolioSignals = field(default_factory=PortfolioSignals)
    quota_cut: bool = False
    priority_boosted: bool = False


def rebalance(workcells: list[WorkcellAllocation]) -> list[WorkcellAllocation]:
    """Apply the adaptive allocation policy in place; return the same list.

    Mutates each :class:`WorkcellAllocation` and returns the list so the
    function composes both as a statement and as an expression. Idempotent
    only at the policy level — calling it twice cuts the quota twice — so
    callers run it once per portfolio cycle.
    """
    for alloc in workcells:
        sig = alloc.signals
        if sig.error_velocity > ERROR_VELOCITY_CUT_THRESHOLD:
            alloc.token_quota *= TOKEN_QUOTA_CUT_FACTOR
            alloc.quota_cut = True
        if sig.throughput_efficiency > THROUGHPUT_BOOST_THRESHOLD:
            alloc.priority_weight *= PRIORITY_WEIGHT_BOOST_FACTOR
            alloc.priority_boosted = True
    return workcells
