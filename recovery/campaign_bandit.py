#!/usr/bin/env python3
"""
CampaignBandit — multi-armed bandit for execution optimization
Source: ChatGPT recovery chat 02 (bandit layer)

Canonical relationship:
- [NEW] exploration/exploitation engine for outreach/fulfillment variant selection
- Paired with llm_portfolio_manager.py (Layer 2 LEARNS in the 3-layer stack)
- [DEFERRED] persistence layer (currently in-memory; needs DDB/SQLite backing)

UCB1 (Upper Confidence Bound) implementation. Pluggable reward shape.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ArmStats:
    wins: int = 0
    trials: int = 0


@dataclass
class RewardWeights:
    reply_weight: float = 1.0
    conversion_weight: float = 5.0
    engagement_weight: float = 0.5


class CampaignBandit:
    """Multi-armed bandit with UCB1 selection."""

    def __init__(self, arms: List[str], reward_weights: Optional[RewardWeights] = None):
        self.stats: Dict[str, ArmStats] = {a: ArmStats() for a in arms}
        self.weights = reward_weights or RewardWeights()
        self._total_trials = 0

    def select(self, exploration_bias: float = 2.0) -> str:
        # Untried arms first (force exploration)
        for arm, s in self.stats.items():
            if s.trials == 0:
                return arm

        # UCB1
        best, best_score = None, -math.inf
        for arm, s in self.stats.items():
            mean = s.wins / s.trials
            confidence = math.sqrt(exploration_bias * math.log(self._total_trials) / s.trials)
            score = mean + confidence
            if score > best_score:
                best, best_score = arm, score
        return best  # type: ignore[return-value]

    def update(self, arm: str, signals: Dict[str, Any]) -> None:
        reward = (
            self.weights.reply_weight * signals.get("email_reply", 0)
            + self.weights.conversion_weight * signals.get("closed_deal", 0)
            + self.weights.engagement_weight * signals.get("click_rate", 0.0)
        )
        s = self.stats.setdefault(arm, ArmStats())
        s.wins += reward
        s.trials += 1
        self._total_trials += 1

    def best_arm(self) -> Optional[str]:
        if not self.stats:
            return None
        return max(self.stats, key=lambda a: (self.stats[a].wins / max(self.stats[a].trials, 1)))

    def snapshot(self) -> Dict[str, Dict[str, int]]:
        return {a: {"wins": s.wins, "trials": s.trials} for a, s in self.stats.items()}
