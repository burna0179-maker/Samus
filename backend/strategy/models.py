"""Pydantic I/O models for the strategy workcell."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EvaluateRequest(BaseModel):
    """Request to evaluate a prospect and decide the next action."""

    model_config = ConfigDict(extra="forbid")

    prospect_id: str = Field(min_length=1)


class EvaluateResponse(BaseModel):
    """Result of evaluating a prospect."""

    model_config = ConfigDict(extra="forbid")

    prospect_id: str
    action: Literal["escalate_close", "replan_fulfillment", "trigger_outreach", "monitor", "none"]
    score: float
    reason: str = ""


class DispatchRequest(BaseModel):
    """Request to dispatch a strategy action for a prospect."""

    model_config = ConfigDict(extra="forbid")

    prospect_id: str
    action: str
    payload: dict = Field(default_factory=dict)


class RecordOutcomeRequest(BaseModel):
    """Request to record the outcome of a completed strategy cycle.

    ``prospect_id`` + ``won`` are the legacy contract — kept required so
    pre-this-feature callers (and the heuristic pattern-weight path) work
    unchanged. The fields below (strategy-integration build, Unit 3) carry the
    bandit arm + the reward-density signal so a closed CRM deal can be credited
    to the exact ``industry::policy_family`` arm that picked the prospect. They
    are all optional/defaulted: a request without ``industry`` / ``policy_family``
    skips the bandit and only updates the heuristic weights.
    """

    model_config = ConfigDict(extra="forbid")

    prospect_id: str
    won: bool
    # hierarchical-bandit arm — empty on a pre-this-feature Opportunity
    industry: str = ""
    policy_family: str = ""
    # graded reward (closed_won -> 1.0, closed_lost -> 0.0); the bandit prefers
    # this over the boolean ``won`` so partial-credit grading can land later.
    outcome: float = 0.0
    # reward-signal snapshot captured at Opportunity creation. seo_score is the
    # raw 0-100 CRM value — strategy.service normalises it to [0,1].
    seo_score: int = 0
    owner_email: bool = False
    social_facebook: bool = False
    social_instagram: bool = False
    # per-prospect LLM dollars spent during discovery (strategy-integration
    # build, Unit 4). Passed straight into RewardSignal.token_cost_usd so the
    # reward-density model can weigh a cheap win against an expensive one.
    # compute_reward_density floors it at MIN_TOKEN_COST_USD. 0.0 on a request
    # with no discovery-cost provenance — RewardSignal then uses its own
    # neutral default.
    token_cost_usd: float = 0.0


class RecordOutcomeResponse(BaseModel):
    """Confirmation that an outcome was recorded."""

    model_config = ConfigDict(extra="forbid")

    prospect_id: str
    recorded: bool
