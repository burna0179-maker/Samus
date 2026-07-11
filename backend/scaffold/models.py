"""Pydantic models for the scaffold workcell (doc §6)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


AssetType = Literal[
    "proposal_pack",
    "implementation_plan",
    "operating_brief",
    "campaign_brief",
]


class ScaffoldRequest(BaseModel):
    """Inbound asset-generation request."""

    asset_type: AssetType
    title: str = Field(min_length=3)
    client: str = Field(min_length=2)
    brand_voice: str = Field(min_length=2)
    offer: str = Field(min_length=2)
    goals: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title", "client", "brand_voice", "offer", mode="before")
    @classmethod
    def _strip_strings(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value
