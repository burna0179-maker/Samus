"""Tests for backend.content.planner (content calendar + topic planner)."""
from __future__ import annotations

import pytest

from backend.content.planner import (
    plan_content_month,
    BlogTopic,
    ContentPlan,
    _faq_questions_for,
    _blog_title,
    _TOPICS_PER_THEME,
    _NURTURE_TRIGGERS,
)
from backend.seo.models import AuditResult, SeoIssue
from backend.common.dates import iso_now


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_audit_with_issue(issue_id: str) -> AuditResult:
    return AuditResult(
        url="https://example.com",
        seo_score=60,
        issues=[
            SeoIssue(
                id=issue_id,
                severity="high",
                category="technical",
                message="Test issue",
                evidence="",
            )
        ],
        findings={"fetched": True, "schema_types": []},
        ts=iso_now(),
    )


# ---------------------------------------------------------------------------
# plan_content_month
# ---------------------------------------------------------------------------


def test_plan_content_month_returns_content_plan():
    plan = plan_content_month(
        customer_id="cust-001",
        month=1,
        year=2026,
        primary_keywords=["GEO optimization"],
    )
    assert isinstance(plan, ContentPlan)
    assert plan.customer_id == "cust-001"
    assert plan.month == 1
    assert plan.year == 2026


def test_plan_content_month_has_two_to_four_topics():
    for month in range(1, 13):
        plan = plan_content_month(
            customer_id="cust-001",
            month=month,
            year=2026,
            primary_keywords=["SEO"],
        )
        assert 2 <= len(plan.blog_topics) <= 4, (
            f"month {month}: expected 2-4 topics, got {len(plan.blog_topics)}"
        )


def test_plan_content_month_authority_month_has_four_topics():
    # Month 2 maps to theme_month 2 (Authority) -- 4 topics
    plan = plan_content_month(
        customer_id="cust-001",
        month=2,
        year=2026,
        primary_keywords=["GEO"],
    )
    assert len(plan.blog_topics) == 4


def test_plan_content_month_foundation_month_has_two_topics():
    # Month 1 maps to theme_month 1 (Foundation) -- 2 topics
    plan = plan_content_month(
        customer_id="cust-001",
        month=1,
        year=2026,
        primary_keywords=["GEO"],
    )
    assert len(plan.blog_topics) == 2


def test_plan_content_month_topics_have_question_titles():
    plan = plan_content_month(
        customer_id="cust-001",
        month=1,
        year=2026,
        primary_keywords=["local SEO"],
    )
    _question_starters = ("How", "What", "Why", "Which", "When", "Where", "Is", "Are", "Does", "Do", "Can")
    for topic in plan.blog_topics:
        is_question = "?" in topic.title or any(topic.title.startswith(w) for w in _question_starters)
        assert is_question, f"Title not question-formatted: {topic.title!r}"


def test_plan_content_month_topics_have_six_faq_questions():
    plan = plan_content_month(
        customer_id="cust-001",
        month=1,
        year=2026,
        primary_keywords=["GEO"],
    )
    for topic in plan.blog_topics:
        assert len(topic.faq_questions) == 6


def test_plan_content_month_topics_have_social_slots():
    plan = plan_content_month(
        customer_id="cust-001",
        month=1,
        year=2026,
        primary_keywords=["GEO"],
    )
    for topic in plan.blog_topics:
        assert len(topic.social_slots) > 0


def test_plan_content_month_social_slots_have_valid_platforms():
    valid_platforms = {"linkedin", "instagram", "x", "facebook"}
    plan = plan_content_month(
        customer_id="cust-001",
        month=1,
        year=2026,
        primary_keywords=["GEO"],
    )
    for topic in plan.blog_topics:
        for slot in topic.social_slots:
            assert slot.platform in valid_platforms


def test_plan_content_month_outreach_trigger_set():
    plan = plan_content_month(
        customer_id="cust-001",
        month=1,
        year=2026,
        primary_keywords=["GEO"],
    )
    for topic in plan.blog_topics:
        assert topic.outreach_trigger


def test_plan_content_month_publish_week_in_range():
    plan = plan_content_month(
        customer_id="cust-001",
        month=2,
        year=2026,
        primary_keywords=["GEO"],
    )
    for topic in plan.blog_topics:
        assert 1 <= topic.publish_week <= 4


def test_plan_content_month_seo_findings_adds_bonus_keywords():
    audit = _make_audit_with_issue("geo_no_faq_schema")
    plan = plan_content_month(
        customer_id="cust-001",
        month=1,
        year=2026,
        primary_keywords=["SEO"],
        seo_findings=audit,
    )
    # seo_issues_addressed should contain the matched issue
    assert "geo_no_faq_schema" in plan.seo_issues_addressed


def test_plan_content_month_no_keywords_uses_default():
    # Should not raise even with empty keywords
    plan = plan_content_month(
        customer_id="cust-001",
        month=1,
        year=2026,
        primary_keywords=[],
    )
    assert len(plan.blog_topics) >= 2


def test_plan_content_month_ts_set():
    plan = plan_content_month(
        customer_id="cust-001",
        month=6,
        year=2026,
        primary_keywords=["GEO"],
    )
    assert plan.ts


def test_plan_content_month_cycle_repeats():
    # Month 5 should behave like month 1 (theme_month = 1)
    plan_1 = plan_content_month("c", 1, 2026, ["kw"])
    plan_5 = plan_content_month("c", 5, 2026, ["kw"])
    assert len(plan_1.blog_topics) == len(plan_5.blog_topics)


# ---------------------------------------------------------------------------
# _faq_questions_for
# ---------------------------------------------------------------------------


def test_faq_questions_returns_six():
    qs = _faq_questions_for("GEO optimization", "plumbing")
    assert len(qs) == 6


def test_faq_questions_contain_keyword():
    qs = _faq_questions_for("schema markup", "dental")
    joined = " ".join(qs).lower()
    assert "schema markup" in joined


# ---------------------------------------------------------------------------
# _blog_title
# ---------------------------------------------------------------------------


def test_blog_title_is_question_formatted():
    _question_starters = ("How", "What", "Why", "Which", "When", "Where", "Is", "Are", "Does", "Do", "Can")
    for i in range(8):
        title = _blog_title("GEO", i, "plumbing")
        is_question = "?" in title or any(title.startswith(w) for w in _question_starters)
        assert is_question, f"Title index {i} not question-formatted: {title!r}"


def test_blog_title_contains_keyword():
    title = _blog_title("local SEO", 0, "plumbing")
    assert "Local Seo" in title or "local SEO" in title.lower()


# ---------------------------------------------------------------------------
# Social slot structure
# ---------------------------------------------------------------------------


def test_social_slots_have_brief():
    plan = plan_content_month(
        customer_id="cust-001",
        month=1,
        year=2026,
        primary_keywords=["GEO"],
    )
    for topic in plan.blog_topics:
        for slot in topic.social_slots:
            assert slot.brief  # non-empty


def test_social_slots_status_is_planned():
    plan = plan_content_month(
        customer_id="cust-001",
        month=1,
        year=2026,
        primary_keywords=["GEO"],
    )
    for topic in plan.blog_topics:
        for slot in topic.social_slots:
            assert slot.status == "planned"
