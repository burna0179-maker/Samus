"""Hustleforge self-marketing campaign configuration.

Treats Hustleforge as a first-party client through the existing social/content
pipeline.  This module owns the static config only; orchestration lives in
campaign_cycle.py.

ASCII-only output. No new external dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Platform = Literal["linkedin", "instagram", "x"]


# ---------------------------------------------------------------------------
# Campaign config dataclass
# ---------------------------------------------------------------------------


@dataclass
class SelfMarketingCampaign:
    """Configuration for a first-party self-marketing campaign.

    All fields are plain data — no I/O, no imports.  The orchestrator
    (campaign_cycle.py) reads this and drives the pipeline.
    """

    brand: str
    site_url: str
    target_keywords: list[str] = field(default_factory=list)
    content_themes: list[str] = field(default_factory=list)
    social_platforms: list[str] = field(default_factory=list)
    posts_per_month: int = 8       # 2 blogs x 4 social assets each
    blogs_per_month: int = 2
    author_name: str = ""
    author_url: str = ""

    # Derived / computed helpers -----------------------------------------------

    def keyword_for_index(self, index: int) -> str:
        """Return a keyword from the list, cycling if index exceeds length."""
        if not self.target_keywords:
            return self.brand.lower()
        return self.target_keywords[index % len(self.target_keywords)]

    def theme_for_index(self, index: int) -> str:
        """Return a content theme string, cycling as needed."""
        if not self.content_themes:
            return f"{self.brand} overview"
        return self.content_themes[index % len(self.content_themes)]


# ---------------------------------------------------------------------------
# Canonical Hustleforge campaign instance
# ---------------------------------------------------------------------------

HUSTLEFORGE_CAMPAIGN = SelfMarketingCampaign(
    brand="Hustleforge",
    site_url="https://hustleforge.tech",
    target_keywords=[
        "AI business automation",
        "AI agent for small business",
        "AI operations partner",
        "autonomous business AI",
        "AI-powered business growth",
        "small business AI tools",
        "AI sales automation",
        "AI client outreach",
    ],
    content_themes=[
        "How Hustleforge automates client outreach with AI",
        "AI visibility for small business: what works in 2026",
        "How we used AI to replace manual ops tasks",
        "Getting your business cited by AI: a practical guide",
        "Why AI agents are the next hire for your business",
    ],
    social_platforms=["linkedin", "instagram", "x"],
    posts_per_month=8,
    blogs_per_month=2,
    author_name="Hustleforge Team",
    author_url="https://hustleforge.tech/about",
)


__all__ = [
    "SelfMarketingCampaign",
    "HUSTLEFORGE_CAMPAIGN",
]
