"""Content calendar and topic planner.

Bridges blog generation (backend.seo.blog_gen), social repurposing
(backend.social.calendar), and outreach sequences.

Key entry point: plan_content_month()

Returns a ContentPlan with 2-4 blog topics matched to the social calendar
theme for the requested month. Each topic carries:
  - target keyword
  - blog title (question-formatted)
  - suggested FAQ questions (6 per topic)
  - social repurposing slots (matched to MonthlyPlan shapes)
  - outreach nurture trigger label

Reuses backend.social.models shapes: MonthlyPlan, PlannedPost.
No LLM calls. No new dependencies. ASCII-only output.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from backend.common.dates import iso_now
from backend.seo.models import AuditResult
from backend.social.models import (
    MonthlyPlan,
    PlannedPost,
    Platform,
    SocialFormat,
    PipelineFunction,
)

_LOG = logging.getLogger("samus.content.planner")

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass
class BlogTopic:
    """A single planned blog topic with full content brief."""

    target_keyword: str
    title: str  # question-formatted H1
    summary: str  # 1-2 sentence brief
    faq_questions: list[str]  # 6 suggested FAQ questions
    social_slots: list[PlannedPost]  # repurposing calendar entries
    outreach_trigger: str  # nurture sequence label
    publish_week: int  # week number within the month (1-4)


@dataclass
class ContentPlan:
    """A month of coordinated blog + social + outreach content."""

    customer_id: str
    month: int  # 1-12
    year: int
    primary_keywords: list[str]
    blog_topics: list[BlogTopic]  # 2-4 topics
    social_plan: MonthlyPlan | None  # matched social calendar month
    seo_issues_addressed: list[str]  # issue ids from AuditResult
    ts: str = ""


# ---------------------------------------------------------------------------
# Theme-to-nurture trigger mapping
# ---------------------------------------------------------------------------

# Maps social month index (1-4 cycle) to an outreach nurture trigger label.
# Month 1 = Foundation, Month 2 = Authority, Month 3 = Proof, Month 4 = Convert.
_NURTURE_TRIGGERS: dict[int, str] = {
    1: "problem_awareness",
    2: "solution_education",
    3: "social_proof_followup",
    4: "conversion_offer",
}

# Blog topic count per month theme (authority months get more content)
_TOPICS_PER_THEME: dict[int, int] = {
    1: 2,  # Foundation -- build awareness, 2 topics
    2: 4,  # Authority -- showcase methodology, 4 topics
    3: 3,  # Proof -- case studies + data, 3 topics
    4: 2,  # Convert -- offer-focused, 2 topics
}

# Social formats to assign to each blog repurposing slot
_REPURPOSE_SLOTS: list[tuple[str, Platform, SocialFormat, PipelineFunction]] = [
    ("Mon", "linkedin", "li_text", "educate"),
    ("Wed", "instagram", "ig_carousel", "educate"),
    ("Fri", "linkedin", "li_link", "convert"),
]


# ---------------------------------------------------------------------------
# FAQ question templates
# ---------------------------------------------------------------------------


def _faq_questions_for(keyword: str, industry: str = "") -> list[str]:
    ind = industry or "local service"
    return [
        f"What is {keyword}?",
        f"How does {keyword} work for {ind} businesses?",
        f"How long does {keyword} take to show results?",
        f"How much does {keyword} cost?",
        f"What are the most common {keyword} mistakes to avoid?",
        f"How do I get started with {keyword}?",
    ]


# ---------------------------------------------------------------------------
# Blog title generation (deterministic, question-formatted)
# ---------------------------------------------------------------------------

_TITLE_TEMPLATES: list[str] = [
    "How to Use {kw} to Get More Customers",
    "What Is {kw} and Why Does Your Business Need It?",
    "How Does {kw} Work for {industry} Businesses?",
    "Why {kw} Is the Highest-ROI Marketing Investment for Local Services",
    "What Are the Biggest {kw} Mistakes Local Businesses Make?",
    "How Long Does {kw} Take to Deliver Results?",
    "How Do You Choose the Right {kw} Strategy?",
    "What Is the Difference Between {kw} and Traditional SEO?",
]


def _blog_title(keyword: str, index: int, industry: str = "") -> str:
    template = _TITLE_TEMPLATES[index % len(_TITLE_TEMPLATES)]
    return template.format(kw=keyword.title(), industry=industry or "local service")


# ---------------------------------------------------------------------------
# Social slot builder
# ---------------------------------------------------------------------------


def _social_slots_for(
    topic_title: str,
    keyword: str,
    week: int,
    month: int,
    theme_name: str,
) -> list[PlannedPost]:
    slots: list[PlannedPost] = []
    for day, platform, fmt, fn in _REPURPOSE_SLOTS:
        brief = (
            f"Repurpose blog '{topic_title}' as {fmt} post. "
            f"Focus on '{keyword}'. Theme: {theme_name}."
        )
        slots.append(
            PlannedPost(
                week=week,
                day=day,
                platform=platform,
                fmt=fmt,
                pipeline_fn=fn,
                theme=theme_name,
                cluster=keyword,
                brief=brief,
            )
        )
    return slots


# ---------------------------------------------------------------------------
# SEO finding -> keyword relevance mapping
# ---------------------------------------------------------------------------

_ISSUE_KEYWORD_BOOST: dict[str, str] = {
    "missing_schema_org": "schema markup",
    "missing_local_business_schema": "local business schema",
    "geo_no_faq_schema": "FAQ schema",
    "geo_no_article_schema": "Article schema",
    "geo_content_stale": "content freshness",
    "geo_ai_crawlers_blocked": "AI search visibility",
    "no_analytics_detected": "website analytics",
}


def _seo_bonus_keywords(seo_findings: AuditResult | None) -> list[str]:
    """Extract bonus keywords from SEO audit issues."""
    if seo_findings is None:
        return []
    bonus: list[str] = []
    for issue in seo_findings.issues:
        if issue.id in _ISSUE_KEYWORD_BOOST:
            bonus.append(_ISSUE_KEYWORD_BOOST[issue.id])
    return list(dict.fromkeys(bonus))  # dedupe preserving order


# ---------------------------------------------------------------------------
# Social calendar month import (optional -- no crash if calendar unavailable)
# ---------------------------------------------------------------------------


def _get_social_monthly_plan(month_index: int) -> MonthlyPlan | None:
    """Load the social calendar plan for the given 1-4 theme month.

    month_index is normalized to 1-4 (the cycle length).
    Returns None on any failure -- planner degrades gracefully.
    """
    try:
        from backend.social.calendar import build_monthly_plan

        return build_monthly_plan(month_index)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("social monthly plan unavailable for month %d: %s", month_index, exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def plan_content_month(
    customer_id: str,
    month: int,
    year: int,
    primary_keywords: list[str],
    *,
    seo_findings: AuditResult | None = None,
    industry: str = "",
) -> ContentPlan:
    """Plan 2-4 blog topics and social calendar entries for one calendar month.

    Parameters
    ----------
    customer_id:       CRM customer ID string
    month:             Calendar month 1-12
    year:              Calendar year
    primary_keywords:  List of target keywords (first is primary)
    seo_findings:      Optional AuditResult; used to add GEO-specific keywords
    industry:          Business industry label for topic personalization

    Returns
    -------
    ContentPlan with blog_topics, social_plan, and seo_issues_addressed.
    """
    if not primary_keywords:
        primary_keywords = ["local services"]

    # Normalize month to 1-4 theme cycle
    theme_month = ((month - 1) % 4) + 1

    # Bonus keywords from SEO audit findings
    bonus_kws = _seo_bonus_keywords(seo_findings)
    all_kws = list(dict.fromkeys(primary_keywords + bonus_kws))

    n_topics = _TOPICS_PER_THEME.get(theme_month, 2)
    n_topics = max(2, min(4, n_topics))

    # Build blog topics
    blog_topics: list[BlogTopic] = []
    addressed_issues: list[str] = []

    for i in range(n_topics):
        kw = all_kws[i % len(all_kws)]
        title = _blog_title(kw, i, industry)
        week = (i % 4) + 1  # spread across 4 weeks
        faqs = _faq_questions_for(kw, industry)
        nurture = _NURTURE_TRIGGERS.get(theme_month, "general_followup")

        # Map social theme name
        theme_labels = {
            1: "Foundation",
            2: "Authority",
            3: "Proof",
            4: "Convert",
        }
        theme_name = theme_labels.get(theme_month, "Foundation")

        slots = _social_slots_for(title, kw, week, month, theme_name)

        blog_topics.append(
            BlogTopic(
                target_keyword=kw,
                title=title,
                summary=(
                    f"A question-answering article on '{kw}' structured for AI citation. "
                    f"Targets '{industry or 'local service'}' prospects in month {month}."
                ),
                faq_questions=faqs,
                social_slots=slots,
                outreach_trigger=nurture,
                publish_week=week,
            )
        )

    # Collect addressed SEO issues
    if seo_findings:
        for issue in seo_findings.issues:
            if issue.id in _ISSUE_KEYWORD_BOOST:
                addressed_issues.append(issue.id)

    # Attempt to load social plan for the theme month
    social_plan = _get_social_monthly_plan(theme_month)

    return ContentPlan(
        customer_id=customer_id,
        month=month,
        year=year,
        primary_keywords=primary_keywords,
        blog_topics=blog_topics,
        social_plan=social_plan,
        seo_issues_addressed=list(dict.fromkeys(addressed_issues)),
        ts=iso_now(),
    )
