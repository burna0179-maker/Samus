"""Pydantic request/response models for the portfolio_controller workcell.

All models forbid extras (``ConfigDict(extra="forbid")``) so a typo in an
inter-service payload surfaces as a 422 rather than being silently dropped.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class WorkcellInput(BaseModel):
    """Per-workcell rebalance input: baseline levers + optional observed signals.

    ``base_token_quota`` / ``base_priority_weight`` are the starting levers
    the allocator mutates. The remaining fields, when supplied, override the
    deterministically-derived signal values (see ``signals.build_signals``);
    when omitted, signals are built purely from the budget store.
    """

    model_config = ConfigDict(extra="forbid")

    workcell: str = Field(min_length=1)
    base_token_quota: float = Field(default=100_000.0, ge=0.0)
    base_priority_weight: float = Field(default=1.0, ge=0.0)
    queue_depth_ratio: float | None = Field(default=None, ge=0.0)
    avg_task_latency_ms: float | None = Field(default=None, ge=0.0)
    token_cost_per_success: float | None = Field(default=None, ge=0.0)
    error_velocity: float | None = Field(default=None, ge=0.0, le=1.0)
    throughput_efficiency: float | None = Field(default=None, ge=0.0, le=1.0)


class RebalanceRequest(BaseModel):
    """Request to rebalance token quotas + priority weights across workcells."""

    model_config = ConfigDict(extra="forbid")

    workcells: list[WorkcellInput] = Field(default_factory=list)
    task_id: str = Field(default="", description="optional caller-supplied task id")


class SignalsModel(BaseModel):
    """Serialised form of ``signals.PortfolioSignals``."""

    model_config = ConfigDict(extra="forbid")

    queue_depth_ratio: float
    avg_task_latency_ms: float
    token_cost_per_success: float
    error_velocity: float
    throughput_efficiency: float


class AllocationResult(BaseModel):
    """One workcell's post-rebalance allocation row."""

    model_config = ConfigDict(extra="forbid")

    workcell: str
    token_quota: float
    priority_weight: float
    quota_cut: bool
    priority_boosted: bool
    signals: SignalsModel


class RebalanceResponse(BaseModel):
    """Structured outcome of a rebalance cycle."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    decision_path: str = "rebalance"
    capability: str = "plan_execution"
    workcell_count: int
    quota_cuts: int
    priority_boosts: int
    allocations: list[AllocationResult] = Field(default_factory=list)
    persisted: bool = False
