"""Dataclasses for the social-content engine.

Kept dependency-free (stdlib only) so they can be imported anywhere — the
repurposing generator, the calendar planner, the adapters, the CLI, and the
tests all share these shapes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# A platform we can plan + dispatch to. LinkedIn + Facebook are handled by the
# existing outreach adapter; Instagram + X by backend.social.adapters.
Platform = Literal["linkedin", "instagram", "x", "facebook"]

# Native content formats. The prefix encodes the platform the format belongs to.
SocialFormat = Literal[
    "li_text",      # LinkedIn hook+insight text post
    "li_carousel",  # LinkedIn swipeable carousel (slide outline)
    "li_link",      # LinkedIn link post (summary; link in first comment)
    "ig_carousel",  # Instagram educational carousel (slide outline)
    "ig_reel",      # Instagram reel (60-90s script)
    "ig_story",     # Instagram story (poll / Q&A)
    "x_thread",     # X / Twitter thread (5-10 tweets)
    "x_tweet",      # X single insight tweet
    "fb_post",      # Facebook Page text post (community engagement)
    "fb_link",      # Facebook Page link post (drives traffic to blog)
]

# Every post serves exactly one pipeline function. The calendar enforces the
# 40/25/20/15 mix so the feed never tips past 15% "convert" (over-selling kills
# organic reach).
PipelineFunction = Literal["educate", "prove", "engage", "convert"]

PostStatus = Literal["draft", "planned", "scheduled", "dry_run", "sent", "failed"]


# ---------------------------------------------------------------------------
# Repurposing (blog -> social package)
# ---------------------------------------------------------------------------


@dataclass
class BlogInput:
    """The source material a repurposing run distils into social assets."""

    title: str
    url: str = ""
    summary: str = ""  # 1-3 sentence gist of the post's core argument
    key_points: list[str] = field(default_factory=list)
    cluster: str = ""  # the topic cluster this post belongs to


@dataclass
class RepurposedAsset:
    """A single native social asset derived from a blog post."""

    fmt: SocialFormat
    platform: Platform
    body: str  # the native content (text, slide outline, or script)
    pipeline_fn: PipelineFunction = "educate"
    used_llm: bool = False
    notes: str = ""  # operator guidance, e.g. "link in first comment"


@dataclass
class RepurposePackage:
    """The full 6-asset package produced from one blog post."""

    source_title: str
    source_url: str = ""
    assets: list[RepurposedAsset] = field(default_factory=list)
    used_llm: bool = False


# ---------------------------------------------------------------------------
# Calendar (monthly theme -> weekly rhythm -> planned posts)
# ---------------------------------------------------------------------------


@dataclass
class ContentTheme:
    """A month's anchoring content cluster. All posts that month tie back to it
    so AI systems build a consistent entity association (LEO)."""

    month: int
    name: str
    goal: str
    weekly_focus: list[str]  # one focus line per week (index 0 == week 1)


@dataclass
class RhythmSlot:
    """A recurring weekly slot in the publishing rhythm (platform + format +
    pipeline function), independent of which week it lands in."""

    day: str  # "Mon".."Fri"
    platform: Platform
    fmt: SocialFormat
    pipeline_fn: PipelineFunction
    brief: str  # what to post in this slot (templated against the week focus)


@dataclass
class PlannedPost:
    """A concrete post scheduled into a calendar slot. ``body`` is empty until
    a generator (repurposing or manual) fills it; ``stake_sentence`` is empty
    until an operator vouches for it — both are required before a live send."""

    week: int
    day: str
    platform: Platform
    fmt: SocialFormat
    pipeline_fn: PipelineFunction
    theme: str
    cluster: str
    brief: str = ""
    body: str = ""
    scheduled_at: str = ""
    stake_sentence: str = ""
    status: PostStatus = "planned"


@dataclass
class MonthlyPlan:
    """A full month of planned posts under one theme."""

    month: int
    theme: str
    cluster: str
    posts: list[PlannedPost] = field(default_factory=list)


@dataclass
class SocialDispatchResult:
    """Result of dispatching one planned post. Mirrors the outreach adapter's
    ``SocialSendResult`` but carries the wider :data:`Platform` literal."""

    sent: bool
    platform: Platform
    post_id: str = ""
    error: str = ""
    dry_run: bool = False
