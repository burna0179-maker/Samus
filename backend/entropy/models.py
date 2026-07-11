"""Pydantic I/O models for the entropy workcell."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class EntropyScanRequest(BaseModel):
    """Instability inputs for one entropy scan.

    The four core signals are weighted into the entropy score. The three
    optional signals refine the countermeasure recommendation.
    """

    model_config = ConfigDict(extra="forbid")

    queue_variance: float = Field(default=0.0, ge=0.0, le=1.0)
    error_velocity: float = Field(default=0.0, ge=0.0, le=1.0)
    task_retry_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    llm_failure_ratio: float = Field(default=0.0, ge=0.0, le=1.0)

    # Optional refinement signals — countermeasure rules skip when omitted.
    efficiency_ema: float | None = Field(default=None, ge=0.0, le=1.0)
    token_success_ratio: float | None = Field(default=None, ge=0.0)
    template_success_rate: float | None = Field(default=None, ge=0.0, le=1.0)


class EntropyScanResponse(BaseModel):
    """Result of an entropy scan — score plus recommended countermeasures."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    entropy_score: float
    countermeasures: list[str] = Field(default_factory=list)
    stable: bool
    persisted: bool = False


__all__ = ["EntropyScanRequest", "EntropyScanResponse"]
