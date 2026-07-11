"""Pydantic request/response models for the signal_filter workcell.

All models forbid extra fields (``ConfigDict(extra="forbid")``) so a
malformed payload fails fast with a 422 rather than silently dropping data.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProspectInput(BaseModel):
    """An inbound, un-qualified prospect awaiting an admission decision.

    Everything except ``website_url`` is optional — the gate degrades
    gracefully on sparse input. A prospect with no website at all still
    gets scored (it just scores low and is rejected).
    """

    model_config = ConfigDict(extra="forbid")

    prospect_id: str = ""
    business_name: str = ""
    website_url: str = ""
    # Google Places identity, when the prospect came from a Places search.
    place_id: str = ""
    # Maps-style review signals — strings because Places returns them as such.
    review_rating: str = ""
    review_count: str = ""
    industry: str = ""
    phone: str = ""
    city: str = ""
    state: str = ""


class SignalScores(BaseModel):
    """The seven scored signal axes, each clamped to ``[0.0, 1.0]``.

    Mirrors :class:`~backend.signal_filter.scoring.ProspectSignal` field for
    field — this is its serializable surface for HTTP responses.
    """

    model_config = ConfigDict(extra="forbid")

    domain_health: float = Field(ge=0.0, le=1.0)
    seo_score: float = Field(ge=0.0, le=1.0)
    review_velocity: float = Field(ge=0.0, le=1.0)
    contactability: float = Field(ge=0.0, le=1.0)
    social_activity: float = Field(ge=0.0, le=1.0)
    revenue_estimate: float = Field(ge=0.0, le=1.0)
    infrastructure_maturity: float = Field(ge=0.0, le=1.0)


class EvaluateResponse(BaseModel):
    """The admission decision for one prospect."""

    model_config = ConfigDict(extra="forbid")

    prospect_id: str
    business_name: str = ""
    website_url: str = ""
    admitted: bool
    weighted_score: float = Field(ge=0.0, le=1.0)
    threshold: float = Field(ge=0.0, le=1.0)
    decision_path: str
    signals: SignalScores
    # Raw enrichment output kept for operator inspection / downstream reuse.
    enrichment: dict[str, Any] = Field(default_factory=dict)
    ts: str


__all__ = ["ProspectInput", "SignalScores", "EvaluateResponse"]
