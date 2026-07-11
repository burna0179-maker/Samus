"""Pydantic models for the leadgen workcell (doc §5)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class LeadRequest(BaseModel):
    """Inbound lead-qualification request."""

    company: str = Field(min_length=2)
    domain: str = Field(min_length=3)
    industry: str = Field(min_length=2)
    employee_count: int = Field(ge=1, le=1_000_000)
    annual_revenue_usd: int = Field(ge=0)
    geo: str = Field(min_length=2)
    signals: list[str] = Field(default_factory=list)

    @field_validator("company", "domain", "industry", "geo", mode="before")
    @classmethod
    def _strip_strings(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


class LeadScore(BaseModel):
    """Outbound scoring result."""

    company: str
    normalized_domain: str
    segment: Literal["micro", "smb", "midmarket", "enterprise"]
    score: int = Field(ge=0, le=100)
    tier: Literal["low", "medium", "high", "priority"]
    matched_signals: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
