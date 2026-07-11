"""Pydantic models for the optimizer workcell."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RewardSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email_reply: int = 0
    closed_deal: int = 0
    click_rate: float = 0.0


class RewardWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply_weight: float = 1.0
    conversion_weight: float = 5.0
    engagement_weight: float = 0.5


class ArmStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wins: float = 0.0
    trials: int = 0


class BanditState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_id: str
    arms: dict[str, ArmStats] = Field(default_factory=dict)
    total_trials: int = 0
    weights: RewardWeights = Field(default_factory=RewardWeights)


class SelectArmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_id: str
    arms: list[str] = Field(min_length=1)
    exploration_bias: float = 2.0
    weights: RewardWeights | None = None


class SelectArmResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_id: str
    selected_arm: str
    snapshot: dict[str, ArmStats]
    total_trials: int


class UpdateArmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_id: str
    arm: str
    signals: RewardSignals
    weights: RewardWeights | None = None


class UpdateArmResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_id: str
    arm: str
    snapshot: dict[str, ArmStats]
    total_trials: int
    best_arm: str | None = None


class Opportunity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prospect_id: str
    expected_value: float = 1000.0
    conversion_prob: float = 0.1
    time_to_close: int = 7
    execution_cost: float = 100.0
    stage: str = "new"
    momentum: float = 0.0
    signals: list[str] = Field(default_factory=list)


class PortfolioAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prospect_id: str
    decision: Literal["accelerate", "maintain", "defer", "deprioritize"]
    score: float
    projected_cost: float


class OptimizePortfolioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_id: str
    total_budget: float = 5000.0
    top_tier: int = 5
    mid_tier: int = 15
    opportunities: list[Opportunity] = Field(default_factory=list)


class OptimizePortfolioResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_id: str
    actions: list[PortfolioAction] = Field(default_factory=list)
    total_estimated_cost: float = 0.0
    budget_remaining: float = 0.0
