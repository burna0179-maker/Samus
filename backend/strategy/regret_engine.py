"""Cumulative-regret tracking per bandit arm.

Regret = the reward you forfeited by not always playing the best arm. The
predictive allocator uses regret-per-token as a budget-efficiency signal:
high cumulative regret for the tokens spent means the bandit is exploring
poorly and a proactive shift is overdue.

Pure-logic — deterministic, no I/O.

Telemetry reconciliation
------------------------
Per-arm token spend is NOT real telemetry today (``portfolio_manager`` keeps
no per-arm cost). :func:`regret_per_token` therefore accepts
``total_token_spend`` as an explicit parameter with a sensible default so the
function stays well-defined; once per-arm cost telemetry lands the caller
just passes the real figure.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "RegretLedger",
    "cumulative_regret",
    "regret_per_token",
    "REGRET_TOKEN_EPSILON",
    "DEFAULT_TOKEN_SPEND",
]

# Small epsilon so regret_per_token never divides by zero.
REGRET_TOKEN_EPSILON: float = 1e-6
# Neutral-default token spend when per-arm cost telemetry is unavailable.
DEFAULT_TOKEN_SPEND: float = 1.0


@dataclass
class RegretLedger:
    """Accumulates per-arm regret across trials.

    For each recorded trial the regret is ``best_possible_reward - reward``;
    a trial that played the best arm contributes 0. Pure in-memory ledger
    with no persistence — mirrors the tier-3 deferral in
    :mod:`backend.strategy.portfolio_manager`.
    """

    per_arm_regret: dict[str, float] = field(default_factory=dict)
    trial_count: int = 0

    def record(self, arm_id: str, reward: float, best_possible_reward: float) -> float:
        """Record one trial; return the incremental regret for it.

        Incremental regret is clamped at >= 0 — a trial can never have
        negative regret (you cannot beat the best possible reward).
        """
        regret = max(best_possible_reward - reward, 0.0)
        self.per_arm_regret[arm_id] = self.per_arm_regret.get(arm_id, 0.0) + regret
        self.trial_count += 1
        return regret

    def total(self) -> float:
        """Cumulative regret summed across every arm."""
        return sum(self.per_arm_regret.values())

    def for_arm(self, arm_id: str) -> float:
        """Cumulative regret for a single arm (0.0 if never recorded)."""
        return self.per_arm_regret.get(arm_id, 0.0)


def cumulative_regret(rewards: list[float], best_possible_reward: float) -> float:
    """Cumulative regret for a series of rewards against a fixed optimum.

    ``regret_i = max(best_possible_reward - reward_i, 0.0)`` summed over the
    series. An empty series yields 0.0. Pure function.
    """
    return sum(max(best_possible_reward - r, 0.0) for r in rewards)


def regret_per_token(
    cumulative_regret_value: float,
    total_token_spend: float = DEFAULT_TOKEN_SPEND,
) -> float:
    """Regret normalised by token spend — a budget-efficiency signal.

    ``regret / max(token_spend, epsilon)``. Per-arm token spend is not real
    telemetry yet (see module docstring); callers pass ``total_token_spend``
    explicitly or accept the neutral default of 1.0.
    """
    return cumulative_regret_value / max(total_token_spend, REGRET_TOKEN_EPSILON)
