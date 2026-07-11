"""UCB1 multi-armed bandit primitives.

Pure functions over a ``stats`` dict so callers can hold the state wherever
makes sense (in-process for tests, GLOBAL_IDEMPOTENCY_STORE for the workcell,
DDB for a future shared-state mode). No module-level state.

Stats dict shape::

    {"arm_a": {"wins": 0.0, "trials": 0}, "arm_b": {"wins": 4.0, "trials": 7}, ...}
"""

from __future__ import annotations

import math

from .models import RewardSignals, RewardWeights


def ucb1_select(
    stats: dict[str, dict[str, float]],
    total_trials: int,
    exploration_bias: float = 2.0,
) -> str:
    """Return the arm with the highest UCB1 score. Untried arms picked first.

    UCB1 score = mean_reward + sqrt(exploration_bias × ln(total_trials) / trials).
    """
    if not stats:
        raise ValueError("ucb1_select requires at least one arm")
    # Force-explore any untried arm first.
    for arm, s in stats.items():
        if (s.get("trials") or 0) == 0:
            return arm

    if total_trials <= 0:
        # Degenerate — every arm has trials but total_trials is 0; pick first.
        return next(iter(stats.keys()))

    best_arm: str | None = None
    best_score = -math.inf
    for arm, s in stats.items():
        trials = max(1, int(s["trials"]))
        wins = float(s.get("wins", 0.0))
        mean = wins / trials
        confidence = math.sqrt(exploration_bias * math.log(total_trials) / trials)
        score = mean + confidence
        if score > best_score:
            best_arm = arm
            best_score = score
    assert best_arm is not None
    return best_arm


def apply_reward(
    stats: dict[str, dict[str, float]],
    arm: str,
    signals: RewardSignals,
    weights: RewardWeights,
) -> tuple[dict[str, dict[str, float]], int]:
    """Return ``(new_stats, new_total_trials)`` after recording one trial.

    Reward = reply_weight × email_reply + conversion_weight × closed_deal
             + engagement_weight × click_rate.
    """
    reward = (
        weights.reply_weight * float(signals.email_reply)
        + weights.conversion_weight * float(signals.closed_deal)
        + weights.engagement_weight * float(signals.click_rate)
    )
    new_stats = {k: dict(v) for k, v in stats.items()}
    entry = new_stats.setdefault(arm, {"wins": 0.0, "trials": 0})
    entry["wins"] = float(entry.get("wins", 0.0)) + reward
    entry["trials"] = int(entry.get("trials", 0)) + 1
    new_total = sum(int(e.get("trials", 0)) for e in new_stats.values())
    return new_stats, new_total


def best_arm(stats: dict[str, dict[str, float]]) -> str | None:
    """Return the arm with the highest mean reward."""
    if not stats:
        return None
    return max(
        stats.keys(),
        key=lambda a: float(stats[a].get("wins", 0.0)) / max(1, int(stats[a].get("trials", 0))),
    )
