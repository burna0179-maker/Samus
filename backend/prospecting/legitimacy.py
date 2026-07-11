"""G8 — Pre-flight legitimacy signal types (Chapter 04 / ADR-010).

Outreach is forbidden by Chapter 04 G8 from firing on a "cold-cold" prospect.
A prospect is OUT of cold-cold the moment we hold at least one of:

  * an open RFP / public request for proposal
  * a Chamber of Commerce roster membership
  * a prior inbound (form fill, opened email, website visit)
  * a deterministic public-registry hit (CA SOS, etc.)
  * an open job listing

This module declares the typed signal + the boolean helper. The collectors
that produce signals live in :mod:`backend.prospecting.sources`; the
aggregator + per-prospect assessment in :mod:`backend.prospecting.legitimacy_check`.

There is no "low" confidence tier on purpose: a signal that cannot be
deterministically tagged "high" or "medium" does not count as warmth.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

LegitimacySignalKind = Literal[
    "rfp",
    "chamber_roster",
    "prior_inbound",
    "public_registry",
    "open_job_listing",
]

LegitimacyConfidence = Literal["high", "medium"]


class LegitimacySignal(BaseModel):
    """One piece of evidence that a prospect is NOT cold-cold."""

    model_config = ConfigDict(extra="forbid")

    kind: LegitimacySignalKind
    source: str = Field(min_length=1)
    discovered_at: datetime
    evidence: dict[str, Any] = Field(default_factory=dict)
    confidence: LegitimacyConfidence


class LegitimacyAssessment(BaseModel):
    """Aggregator output for one prospect."""

    model_config = ConfigDict(extra="forbid")

    prospect_id: str = ""
    signals: list[LegitimacySignal] = Field(default_factory=list)
    has_warmth: bool = False
    assessed_at: datetime


def has_warmth(signals: list[LegitimacySignal]) -> bool:
    """True iff at least one valid LegitimacySignal exists."""
    return bool(signals) and any(isinstance(s, LegitimacySignal) for s in signals)


__all__ = [
    "LegitimacyAssessment",
    "LegitimacyConfidence",
    "LegitimacySignal",
    "LegitimacySignalKind",
    "has_warmth",
]
