"""Social content calendar — monthly theme architecture + weekly rhythm.

Each month anchors to one content cluster; every post that month ties back to
it so AI systems build a consistent entity association (LEO). Within the month
a fixed Mon-Fri rhythm fills slots across LinkedIn, Instagram, X, and Facebook.

The planner is pure: it produces :class:`MonthlyPlan` objects (briefs, not live
posts) and can persist them to an auditable JSONL ledger. Dispatch is a
separate, DRY-RUN-by-default step in :mod:`backend.social.adapters`.

It also reports the educate/prove/engage/convert mix and flags when "convert"
exceeds the 15% ceiling (over-selling kills organic reach).
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from backend.social.models import (
    ContentTheme,
    MonthlyPlan,
    PipelineFunction,
    PlannedPost,
    RhythmSlot,
)

_LOG = logging.getLogger("samus.social.calendar")

# Target content mix (Opinly): never let "convert" tip past 15%.
TARGET_MIX: dict[PipelineFunction, float] = {
    "educate": 0.40,
    "prove": 0.25,
    "engage": 0.20,
    "convert": 0.15,
}
CONVERT_CEILING = 0.15

# ---------------------------------------------------------------------------
# The four-month theme cycle. After month 4 the cycle repeats (the problem
# narrative stays relevant to audience members who arrive later).
# ---------------------------------------------------------------------------

MONTHLY_THEMES: tuple[ContentTheme, ...] = (
    ContentTheme(
        month=1,
        name="Foundation — The Problem You're Not Solving",
        goal="Establish the problem narrative; build audience around the pain point.",
        weekly_focus=[
            "Problem definition",
            "Cost of inaction",
            "Why existing solutions fall short",
            "A new frame on the problem",
        ],
    ),
    ContentTheme(
        month=2,
        name="Authority — Here's What Actually Works",
        goal="Establish the methodology as the right solution.",
        weekly_focus=[
            "Framework introduction",
            "Step 1 deep-dive",
            "Steps 2-3 deep-dive",
            "The framework in action",
        ],
    ),
    ContentTheme(
        month=3,
        name="Proof — Results People Like You Are Getting",
        goal="Remove risk; trigger trial/demo conversions.",
        weekly_focus=[
            "Case study #1",
            "The data across customers",
            "Case study #2 (different ICP)",
            "You could do this too",
        ],
    ),
    ContentTheme(
        month=4,
        name="Competitive — Why the Old Way Is Dead",
        goal="Capture decision-stage prospects; differentiate from alternatives.",
        weekly_focus=[
            "The industry shift",
            "Why common alternatives fail",
            "Our differentiators",
            "A decision guide",
        ],
    ),
)


def theme_for_month(month: int) -> ContentTheme:
    """Return the theme for a 1-based month index, cycling every 4 months."""
    if month < 1:
        raise ValueError("month must be >= 1")
    return MONTHLY_THEMES[(month - 1) % len(MONTHLY_THEMES)]


# ---------------------------------------------------------------------------
# The weekly rhythm: 15 slots (Mon-Fri x LinkedIn/Instagram/X). Mirrors the
# Opinly "Weekly Rhythm" so each platform plays to its strength.
# ---------------------------------------------------------------------------

WEEKLY_RHYTHM: tuple[RhythmSlot, ...] = (
    # Monday — open the week's theme.
    RhythmSlot("Mon", "linkedin", "li_text", "educate", "Week-opener insight on {focus}."),
    RhythmSlot(
        "Mon",
        "instagram",
        "ig_carousel",
        "educate",
        "Educational carousel repurposed from the week's blog post on {focus}.",
    ),
    RhythmSlot(
        "Mon", "x", "x_thread", "educate", "Thread expanding the Monday insight on {focus}."
    ),
    RhythmSlot(
        "Mon",
        "facebook",
        "fb_post",
        "educate",
        "Community post: accessible explainer on {focus} for the HustleForge audience.",
    ),
    # Tuesday — framework depth.
    RhythmSlot(
        "Tue", "linkedin", "li_carousel", "educate", "Framework/checklist carousel for {focus}."
    ),
    RhythmSlot(
        "Tue", "instagram", "ig_reel", "educate", "60s reel: one specific tactic for {focus}."
    ),
    RhythmSlot("Tue", "x", "x_tweet", "engage", "Single insight tweet on {focus}."),
    # Wednesday — proof + engagement.
    RhythmSlot(
        "Wed",
        "linkedin",
        "li_text",
        "prove",
        "Social-proof post (Before/After/How) tied to {focus}.",
    ),
    RhythmSlot(
        "Wed",
        "instagram",
        "ig_story",
        "engage",
        "Story poll/Q&A surfacing objections about {focus}.",
    ),
    RhythmSlot(
        "Wed", "x", "x_tweet", "engage", "Reply to 3-5 industry conversations about {focus}."
    ),
    RhythmSlot(
        "Wed",
        "facebook",
        "fb_link",
        "convert",
        "Link post driving to this week's blog on {focus}; native preview.",
    ),
    # Thursday — drive to the blog + demo.
    RhythmSlot(
        "Thu",
        "linkedin",
        "li_link",
        "convert",
        "Link post to this week's blog on {focus}; link in first comment.",
    ),
    RhythmSlot(
        "Thu", "instagram", "ig_reel", "prove", "Reel: product demo / result showcase for {focus}."
    ),
    RhythmSlot(
        "Thu",
        "x",
        "x_thread",
        "educate",
        "Thread expanding the blog post's core argument on {focus}.",
    ),
    # Friday — humanize + roundup.
    RhythmSlot(
        "Fri",
        "linkedin",
        "li_text",
        "engage",
        "'Wins & lessons' post, lighter tone, around {focus}.",
    ),
    RhythmSlot(
        "Fri",
        "instagram",
        "ig_carousel",
        "educate",
        "'Best insight of the week' roundup carousel on {focus}.",
    ),
    RhythmSlot("Fri", "x", "x_tweet", "engage", "Quotable stat or contrarian take on {focus}."),
    RhythmSlot(
        "Fri",
        "facebook",
        "fb_post",
        "engage",
        "Community question or discussion prompt about {focus}; invite comments.",
    ),
)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan_month(
    month: int,
    cluster: str,
    *,
    weeks: int = 4,
    theme: ContentTheme | None = None,
) -> MonthlyPlan:
    """Build a full month of :class:`PlannedPost` slots under one theme.

    ``cluster`` is the topic the month concentrates on. Each week's slots are
    briefed against that week's focus from the theme. Posts are emitted with
    ``status='planned'`` and empty bodies/stake sentences — generation and
    operator vouching happen downstream.
    """
    theme = theme or theme_for_month(month)
    posts: list[PlannedPost] = []
    for week in range(1, weeks + 1):
        focus = theme.weekly_focus[(week - 1) % len(theme.weekly_focus)]
        for slot in WEEKLY_RHYTHM:
            posts.append(
                PlannedPost(
                    week=week,
                    day=slot.day,
                    platform=slot.platform,
                    fmt=slot.fmt,
                    pipeline_fn=slot.pipeline_fn,
                    theme=theme.name,
                    cluster=cluster,
                    brief=slot.brief.format(focus=focus),
                    status="planned",
                )
            )
    return MonthlyPlan(month=month, theme=theme.name, cluster=cluster, posts=posts)


def mix_distribution(posts: list[PlannedPost]) -> dict[str, float]:
    """Return the fraction of posts in each pipeline function."""
    if not posts:
        return {fn: 0.0 for fn in TARGET_MIX}
    counts = Counter(p.pipeline_fn for p in posts)
    total = len(posts)
    return {fn: round(counts.get(fn, 0) / total, 4) for fn in TARGET_MIX}


def convert_over_ceiling(posts: list[PlannedPost]) -> bool:
    """True when "convert" posts exceed the 15% ceiling."""
    return mix_distribution(posts).get("convert", 0.0) > CONVERT_CEILING + 1e-9


# ---------------------------------------------------------------------------
# Persistence (auditable JSONL ledger)
# ---------------------------------------------------------------------------


def persist_plan(plan: MonthlyPlan, path: str | Path) -> int:
    """Write the plan to a JSONL file (one PlannedPost per line). Returns the
    number of posts written. Overwrites any existing file at ``path``."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with p.open("w", encoding="utf-8") as fh:
        header = {
            "_kind": "social_calendar",
            "month": plan.month,
            "theme": plan.theme,
            "cluster": plan.cluster,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "post_count": len(plan.posts),
            "mix": mix_distribution(plan.posts),
        }
        fh.write(json.dumps(header, ensure_ascii=False, default=str) + "\n")
        for post in plan.posts:
            fh.write(json.dumps(asdict(post), ensure_ascii=False, default=str) + "\n")
            written += 1
    _LOG.info("persisted social calendar month=%s posts=%d -> %s", plan.month, written, p)
    return written


def load_plan(path: str | Path) -> MonthlyPlan:
    """Read a plan back from a JSONL ledger written by :func:`persist_plan`."""
    p = Path(path)
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        raise ValueError(f"empty calendar ledger: {p}")
    header = json.loads(lines[0])
    posts = [PlannedPost(**json.loads(ln)) for ln in lines[1:]]
    return MonthlyPlan(
        month=int(header.get("month", 0)),
        theme=str(header.get("theme", "")),
        cluster=str(header.get("cluster", "")),
        posts=posts,
    )
