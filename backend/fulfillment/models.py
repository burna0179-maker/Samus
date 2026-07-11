"""Pydantic models for the fulfillment workcell (doc §7)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FulfillmentRequest(BaseModel):
    """Flexible request envelope for plan_fulfillment.

    ``actions`` and ``metadata`` are kept loose so upstream callers can pass
    domain-specific payloads (e.g., approvals signatures, risk overrides) without
    schema churn during the migration.
    """

    model_config = ConfigDict(extra="allow")

    task_id: str = Field(min_length=1)
    objective: str = Field(min_length=1, default="execute task")
    actions: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
