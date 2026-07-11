"""Tests for backend.marketing self_campaign + campaign_cycle."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.marketing.self_campaign import (
    HUSTLEFORGE_CAMPAIGN,
    SelfMarketingCampaign,
)
from backend.marketing.campaign_cycle import (
    CampaignResult,
    CampaignStep,
    StepFailure,
    _build_plan,
    _cycle_month_str,
    _exec_plan_topics,
    _exec_month_summary,
    run_monthly_campaign,
)


# ---------------------------------------------------------------------------
# SelfMarketingCampaign config
# ---------------------------------------------------------------------------

class TestCampaignConfig:
    def test_hustleforge_campaign_fields(self):
        c = HUSTLEFORGE_CAMPAIGN
        assert c.brand == "Hustleforge"
        assert c.site_url == "https://hustleforge.tech"
        assert len(c.target_keywords) >= 4
        assert len(c.content_themes) >= 3
        assert c.blogs_per_month == 2
        assert c.posts_per_month == 8
        assert "linkedin" in c.social_platforms
        assert "instagram" in c.social_platforms
        assert "x" in c.social_platforms

    def test_keyword_for_index_cycles(self):
        c = HUSTLEFORGE_CAMPAIGN
        kws = c.target_keywords
        assert c.keyword_for_index(0) == kws[0]
        assert c.keyword_for_index(len(kws)) == kws[0]  # wraps

    def test_theme_for_index_cycles(self):
        c = HUSTLEFORGE_CAMPAIGN
        themes = c.content_themes
        assert c.theme_for_index(0) == themes[0]
        assert c.theme_for_index(len(themes)) == themes[0]

    def test_custom_campaign(self):
        c = SelfMarketingCampaign(
            brand="TestBrand",
            site_url="https://testbrand.com",
            target_keywords=["keyword A"],
            content_themes=["theme 1"],
            social_platforms=["linkedin"],
            posts_per_month=4,
            blogs_per_month=1,
            author_name="Test Author",
            author_url="https://testbrand.com/about",
        )
        assert c.keyword_for_index(5) == "keyword A"
        assert c.theme_for_index(5) == "theme 1"


# ---------------------------------------------------------------------------
# Plan builder
# ---------------------------------------------------------------------------

class TestBuildPlan:
    def test_plan_has_expected_step_ids(self):
        plan = _build_plan(HUSTLEFORGE_CAMPAIGN, "2026-06")
        step_ids = {s.id for s in plan.steps}
        assert "plan_topics" in step_ids
        assert "generate_blog_1" in step_ids
        assert "generate_blog_2" in step_ids
        assert "repurpose_blog_1" in step_ids
        assert "repurpose_blog_2" in step_ids
        assert "dispatch_blog_1" in step_ids
        assert "dispatch_blog_2" in step_ids
        assert "geo_audit" in step_ids
        assert "month_summary" in step_ids

    def test_all_steps_start_pending(self):
        plan = _build_plan(HUSTLEFORGE_CAMPAIGN, "2026-06")
        for step in plan.steps:
            assert step.status == "pending"

    def test_plan_id_format(self):
        plan = _build_plan(HUSTLEFORGE_CAMPAIGN, "2026-06")
        assert plan.plan_id == "hustleforge:2026-06"

    def test_generate_blog_depends_on_plan_topics(self):
        plan = _build_plan(HUSTLEFORGE_CAMPAIGN, "2026-06")
        by_id = {s.id: s for s in plan.steps}
        assert "plan_topics" in by_id["generate_blog_1"].depends_on
        assert "plan_topics" in by_id["generate_blog_2"].depends_on

    def test_month_summary_depends_on_both_dispatches(self):
        plan = _build_plan(HUSTLEFORGE_CAMPAIGN, "2026-06")
        by_id = {s.id: s for s in plan.steps}
        deps = by_id["month_summary"].depends_on
        assert "dispatch_blog_1" in deps
        assert "dispatch_blog_2" in deps
        assert "geo_audit" in deps


# ---------------------------------------------------------------------------
# Individual step executors (unit tests, no I/O)
# ---------------------------------------------------------------------------

class TestExecPlanTopics:
    def _make_step(self) -> CampaignStep:
        return CampaignStep(id="plan_topics", type="content.plan_topics", payload={"cycle_month": "2026-06"})

    def test_returns_topics(self):
        plan = _build_plan(HUSTLEFORGE_CAMPAIGN, "2026-06")
        step = next(s for s in plan.steps if s.id == "plan_topics")
        result = _exec_plan_topics(step, plan, HUSTLEFORGE_CAMPAIGN, dry_run=False)
        topics = result["topics"]
        assert len(topics) == HUSTLEFORGE_CAMPAIGN.blogs_per_month
        for t in topics:
            assert "keyword" in t
            assert "title" in t
            assert "summary" in t

    def test_dry_run_returns_same_output(self):
        plan = _build_plan(HUSTLEFORGE_CAMPAIGN, "2026-06")
        step = next(s for s in plan.steps if s.id == "plan_topics")
        result_live = _exec_plan_topics(step, plan, HUSTLEFORGE_CAMPAIGN, dry_run=False)
        result_dry = _exec_plan_topics(step, plan, HUSTLEFORGE_CAMPAIGN, dry_run=True)
        # plan_topics is pure-config so dry_run does not change output.
        assert result_live["count"] == result_dry["count"]


class TestExecMonthSummary:
    def _run_summary(self, plan, dry_run=False):
        step = next(s for s in plan.steps if s.id == "month_summary")
        return _exec_month_summary(step, plan, HUSTLEFORGE_CAMPAIGN, dry_run)

    def test_empty_plan_gives_zeros(self):
        plan = _build_plan(HUSTLEFORGE_CAMPAIGN, "2026-06")
        result = self._run_summary(plan)
        assert result["blogs_published"] == []
        assert result["total_social_posts_dispatched"] == 0
        assert "next_month_topics" in result
        assert len(result["next_month_topics"]) == HUSTLEFORGE_CAMPAIGN.blogs_per_month

    def test_counts_done_blogs(self):
        plan = _build_plan(HUSTLEFORGE_CAMPAIGN, "2026-06")
        # Mark generate_blog_1 as done with a title.
        g1 = next(s for s in plan.steps if s.id == "generate_blog_1")
        g1.status = "done"
        g1.output = {"title": "Test Blog Post 1"}

        result = self._run_summary(plan)
        assert "Test Blog Post 1" in result["blogs_published"]

    def test_next_month_topics_are_strings(self):
        plan = _build_plan(HUSTLEFORGE_CAMPAIGN, "2026-12")
        result = self._run_summary(plan)
        for t in result["next_month_topics"]:
            assert isinstance(t, str)
            assert len(t) > 0


# ---------------------------------------------------------------------------
# run_monthly_campaign (integration, mocked I/O)
# ---------------------------------------------------------------------------

class TestRunMonthlyCampaign:
    def test_dry_run_completes(self, tmp_path: Path):
        """dry_run=True should finish all steps without real I/O."""
        with patch("backend.marketing.campaign_cycle._campaign_dir", return_value=tmp_path):
            result = run_monthly_campaign(6, 2026, campaign=HUSTLEFORGE_CAMPAIGN, dry_run=True)

        assert isinstance(result, CampaignResult)
        assert result.cycle_month == "2026-06"
        assert result.campaign_id == "hustleforge"
        step_ids = {s.id for s in result.steps}
        assert "plan_topics" in step_ids
        assert "generate_blog_1" in step_ids
        assert "month_summary" in step_ids

    def test_dry_run_plan_json_written(self, tmp_path: Path):
        with patch("backend.marketing.campaign_cycle._campaign_dir", return_value=tmp_path):
            run_monthly_campaign(6, 2026, campaign=HUSTLEFORGE_CAMPAIGN, dry_run=True)

        plan_file = tmp_path / "plan.json"
        assert plan_file.exists()
        import json
        data = json.loads(plan_file.read_text())
        assert data["campaign_id"] == "hustleforge"

    def test_idempotent_rerun(self, tmp_path: Path):
        """A second call on a completed plan resumes without re-running done steps."""
        with patch("backend.marketing.campaign_cycle._campaign_dir", return_value=tmp_path):
            r1 = run_monthly_campaign(6, 2026, campaign=HUSTLEFORGE_CAMPAIGN, dry_run=True)
            r2 = run_monthly_campaign(6, 2026, campaign=HUSTLEFORGE_CAMPAIGN, dry_run=True)

        assert r1.plan_id == r2.plan_id

    def test_cycle_month_str(self):
        assert _cycle_month_str(6, 2026) == "2026-06"
        assert _cycle_month_str(12, 2025) == "2025-12"
        assert _cycle_month_str(1, 2027) == "2027-01"

    def test_result_has_summary(self, tmp_path: Path):
        with patch("backend.marketing.campaign_cycle._campaign_dir", return_value=tmp_path):
            result = run_monthly_campaign(6, 2026, campaign=HUSTLEFORGE_CAMPAIGN, dry_run=True)
        assert isinstance(result.summary, dict)
        assert "next_month_topics" in result.summary

    def test_step_failure_is_soft(self, tmp_path: Path):
        """A failing step should not abort the whole run."""
        bad_campaign = SelfMarketingCampaign(
            brand="BadBrand",
            site_url="https://bad.example.com",
            target_keywords=["test"],
            content_themes=["theme"],
            social_platforms=["linkedin"],
            blogs_per_month=0,   # Triggers downstream failures without crashing
        )
        with patch("backend.marketing.campaign_cycle._campaign_dir", return_value=tmp_path):
            result = run_monthly_campaign(6, 2026, campaign=bad_campaign, dry_run=True)
        # Should return a result, not raise.
        assert isinstance(result, CampaignResult)
