"""Pydantic request/response models for the path-optimizer workcell."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# The four execution routes the optimizer can return.
ExecutionPath = Literal[
    "autonomous_llm",
    "hybrid_template",
    "deterministic_scaffold",
    "safe_static_fallback",
]


class OutcomeRecord(BaseModel):
    """One recent task outcome for the target workcell.

    ``outcome`` drives the error-spike detector; only ``"error"`` entries
    count toward the spike ratio. Other free-form values (e.g. ``"success"``,
    ``"failure"``) are accepted and simply ignored by the detector.
    """

    model_config = ConfigDict(extra="forbid")

    outcome: str


class SelectPathRequest(BaseModel):
    """Request: which workcell to route, plus its recent outcome history."""

    model_config = ConfigDict(extra="forbid")

    workcell: str = Field(min_length=1)
    history: list[OutcomeRecord] = Field(default_factory=list)


class SelectPathResponse(BaseModel):
    """Response: the chosen route plus the inputs that drove the decision."""

    model_config = ConfigDict(extra="forbid")

    workcell: str
    decision_path: ExecutionPath
    efficiency_ema: float = Field(ge=0.0, le=1.0)
    error_spike_detected: bool
    history_size: int = Field(ge=0)
