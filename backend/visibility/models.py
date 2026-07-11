"""Dataclasses for the AIO visibility workcell. Stdlib-only."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

AiPlatform = Literal["claude", "openai", "perplexity"]


@dataclass
class CitationProbe:
    """The result of asking one question on one platform."""

    query: str
    platform: AiPlatform
    answered: bool  # did the platform return any text?
    brand_cited: bool = False
    competitors_cited: list[str] = field(default_factory=list)
    cited_domains: list[str] = field(default_factory=list)
    error: str = ""
    ts: str = ""


@dataclass
class ShareOfVoice:
    """Aggregate visibility across a batch of probes."""

    sample_n: int  # number of answered probes
    citation_rate: float  # fraction of answered probes mentioning the brand
    brand_mentions: int
    competitor_mentions: int
    share_of_voice: float  # brand / (brand + competitor) mentions
    competitor_breakdown: dict[str, int] = field(default_factory=dict)
    top_cited_domains: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class VisibilityReport:
    """A full probe run: every probe + the aggregate."""

    brand_terms: list[str]
    competitor_terms: list[str]
    platforms: list[AiPlatform]
    probes: list[CitationProbe] = field(default_factory=list)
    sov: ShareOfVoice | None = None
    generated_at: str = ""
