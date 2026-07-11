"""Monthly self-marketing campaign orchestrator.

Runs Hustleforge's own content marketing cycle as a 4-week DAG, treating
Hustleforge as a first-party client through the existing social/content
pipeline.

Week 1  -- plan topics, generate 2 blog posts + schema
Week 2  -- repurpose blog 1 -> social package, dispatch
Week 3  -- repurpose blog 2 -> social package, dispatch
Week 4  -- geo audit self-site, produce month summary, queue next topics

Design rules (mirror retainer/monthly_cycle.py):
  * Idempotency  -- each step keyed by (campaign_id, month, year, step_name);
                    re-runs skip steps whose status is "done".
  * Fail-soft    -- step failure is logged + execution continues to next step.
  * Token budget -- reuses existing workcell budgets (seo / outreach).
  * ASCII-only output.
  * No new external dependencies.

Entry point::

    from backend.marketing.campaign_cycle import run_monthly_campaign
    result = await run_monthly_campaign(month=6, year=2026)
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from backend.marketing.self_campaign import HUSTLEFORGE_CAMPAIGN, SelfMarketingCampaign

_LOG = logging.getLogger("samus.marketing.campaign_cycle")


# ---------------------------------------------------------------------------
# Plan / step shapes (same pattern as retainer/monthly_cycle.py)
# ---------------------------------------------------------------------------

StepStatus = Literal["pending", "running", "done", "failed", "skipped"]
PlanStatus = Literal["planned", "running", "complete", "failed"]


@dataclass
class CampaignStep:
    """One DAG node in the campaign plan."""
    id: str
    type: str
    depends_on: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    status: StepStatus = "pending"
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    output: dict[str, Any] = field(default_factory=dict)


@dataclass
class CampaignPlan:
    """Full 4-week DAG + state for one campaign cycle."""
    plan_id: str          # f"{campaign_id}:{YYYY-MM}"
    campaign_id: str
    cycle_month: str      # YYYY-MM
    steps: list[CampaignStep] = field(default_factory=list)
    status: PlanStatus = "planned"
    created_at: str = ""
    updated_at: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)


@dataclass
class CampaignStepResult:
    """Per-step rollup in the public CampaignResult."""
    id: str
    type: str
    status: StepStatus
    detail: str = ""
    elapsed_ms: int = 0


@dataclass
class CampaignResult:
    """Full audit trail of one run_monthly_campaign() invocation."""
    campaign_id: str
    cycle_month: str
    plan_id: str
    plan_path: str
    ok: bool
    steps: list[CampaignStepResult] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    ts: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms_since(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _cycle_month_str(month: int, year: int) -> str:
    return f"{year:04d}-{month:02d}"


def _campaign_dir(campaign_id: str, cycle_month: str) -> Path:
    """Return (and create) the artifact directory for this campaign-month."""
    try:
        from backend.common import storage
        base = storage.root()
    except Exception:  # noqa: BLE001
        base = Path(os.getenv("SAMUS_DATA_ROOT", "data"))
    target = base / "marketing" / "campaigns" / campaign_id / cycle_month
    target.mkdir(parents=True, exist_ok=True)
    return target


def _persist_plan(plan: CampaignPlan) -> Path:
    """Write plan.json idempotently."""
    target = _campaign_dir(plan.campaign_id, plan.cycle_month) / "plan.json"
    plan.updated_at = _now_iso()
    target.write_text(plan.to_json(), encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Plan builder
# ---------------------------------------------------------------------------

def _build_plan(campaign: SelfMarketingCampaign, cycle_month: str) -> CampaignPlan:
    """Construct the 4-week DAG for one month."""
    cid = campaign.brand.lower().replace(" ", "_")
    plan_id = f"{cid}:{cycle_month}"
    return CampaignPlan(
        plan_id=plan_id,
        campaign_id=cid,
        cycle_month=cycle_month,
        created_at=_now_iso(),
        steps=[
            # Week 1
            CampaignStep(
                id="plan_topics",
                type="content.plan_topics",
                payload={"cycle_month": cycle_month},
            ),
            CampaignStep(
                id="generate_blog_1",
                type="content.generate_blog",
                depends_on=["plan_topics"],
                payload={"blog_index": 0},
            ),
            CampaignStep(
                id="generate_blog_2",
                type="content.generate_blog",
                depends_on=["plan_topics"],
                payload={"blog_index": 1},
            ),
            # Week 2
            CampaignStep(
                id="repurpose_blog_1",
                type="social.repurpose",
                depends_on=["generate_blog_1"],
                payload={"blog_index": 0},
            ),
            CampaignStep(
                id="dispatch_blog_1",
                type="social.dispatch",
                depends_on=["repurpose_blog_1"],
                payload={"blog_index": 0},
            ),
            # Week 3
            CampaignStep(
                id="repurpose_blog_2",
                type="social.repurpose",
                depends_on=["generate_blog_2"],
                payload={"blog_index": 1},
            ),
            CampaignStep(
                id="dispatch_blog_2",
                type="social.dispatch",
                depends_on=["repurpose_blog_2"],
                payload={"blog_index": 1},
            ),
            # Week 4
            CampaignStep(
                id="geo_audit",
                type="seo.geo_audit",
                payload={"url": campaign.site_url},
            ),
            CampaignStep(
                id="month_summary",
                type="campaign.month_summary",
                depends_on=[
                    "dispatch_blog_1",
                    "dispatch_blog_2",
                    "geo_audit",
                ],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Step executors
# ---------------------------------------------------------------------------

class StepFailure(RuntimeError):
    """Raised by a step executor to mark its step as failed."""


def _exec_plan_topics(
    step: CampaignStep,
    plan: CampaignPlan,
    campaign: SelfMarketingCampaign,
    dry_run: bool,
) -> dict[str, Any]:
    """Week 1: derive 2 blog topics from the campaign config."""
    year_str, month_str = plan.cycle_month.split("-")
    month = int(month_str)

    # Use campaign keywords + themes to build topic briefs.
    topics: list[dict[str, str]] = []
    for i in range(campaign.blogs_per_month):
        kw = campaign.keyword_for_index(month + i)
        theme = campaign.theme_for_index(month + i)
        topics.append({
            "index": str(i),
            "keyword": kw,
            "theme": theme,
            "title": f"How to Use {kw.title()} for Your Business in {year_str}",
            "summary": (
                f"A practical guide to {kw} for small business owners. "
                f"Covers {theme.lower()}."
            ),
        })
    return {"topics": topics, "count": len(topics)}


def _exec_generate_blog(
    step: CampaignStep,
    plan: CampaignPlan,
    campaign: SelfMarketingCampaign,
    dry_run: bool,
) -> dict[str, Any]:
    """Week 1: generate one structured blog post."""
    blog_index = int(step.payload.get("blog_index", 0))

    # Retrieve topics from the plan_topics step output.
    topics_step = next((s for s in plan.steps if s.id == "plan_topics"), None)
    if topics_step is None or not topics_step.output.get("topics"):
        raise StepFailure("plan_topics output missing")

    topics = topics_step.output["topics"]
    if blog_index >= len(topics):
        raise StepFailure(f"blog_index {blog_index} out of range ({len(topics)} topics)")

    topic = topics[blog_index]
    keyword = topic["keyword"]
    title = topic["title"]
    summary = topic["summary"]

    if dry_run:
        return {
            "blog_index": blog_index,
            "title": title,
            "keyword": keyword,
            "used_llm": False,
            "word_count": 0,
            "dry_run": True,
            "url": f"{campaign.site_url}/blog/{keyword.lower().replace(' ', '-')}",
        }

    try:
        from backend.seo.blog_gen import generate_blog_post
        post = generate_blog_post(
            primary_kw=keyword,
            secondary_kws=[campaign.keyword_for_index(blog_index + 1)],
            author=campaign.author_name,
            site_url=campaign.site_url,
        )
        slug = keyword.lower().replace(" ", "-")
        return {
            "blog_index": blog_index,
            "title": post.title,
            "keyword": keyword,
            "used_llm": post.used_llm,
            "word_count": post.word_count,
            "url": f"{campaign.site_url}/blog/{slug}",
            "author": post.author,
            "date_published": post.date_published,
        }
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("blog gen failed for index=%d: %s", blog_index, exc)
        slug = keyword.lower().replace(" ", "-")
        # Fail-soft: return template placeholder so downstream steps can proceed.
        return {
            "blog_index": blog_index,
            "title": title,
            "keyword": keyword,
            "used_llm": False,
            "word_count": 0,
            "url": f"{campaign.site_url}/blog/{slug}",
            "fallback": True,
            "error": str(exc),
        }


def _exec_repurpose(
    step: CampaignStep,
    plan: CampaignPlan,
    campaign: SelfMarketingCampaign,
    dry_run: bool,
) -> dict[str, Any]:
    """Repurpose a generated blog into the 6-asset social package."""
    blog_index = int(step.payload.get("blog_index", 0))

    gen_step_id = f"generate_blog_{blog_index + 1}"
    gen_step = next((s for s in plan.steps if s.id == gen_step_id), None)
    if gen_step is None or not gen_step.output:
        raise StepFailure(f"{gen_step_id} output missing")

    out = gen_step.output
    title = out.get("title", "")
    url = out.get("url", "")
    keyword = out.get("keyword", "")

    if dry_run:
        return {
            "blog_index": blog_index,
            "source_title": title,
            "asset_count": 6,
            "used_llm": False,
            "dry_run": True,
        }

    from backend.marketing.brand_brief import load_brand_brief, to_voice_prompt
    from backend.social.models import BlogInput
    from backend.social.repurpose import repurpose_blog_post

    blog_input = BlogInput(
        title=title,
        url=url,
        summary=out.get("summary", title),
        key_points=[],
        cluster=keyword,
    )
    brief = load_brand_brief(campaign.brand.lower().replace(" ", "_"))
    voice_prompt = to_voice_prompt(brief) if brief is not None else None
    allowed_numbers = list(brief.proof_points) if brief is not None else []
    pkg = repurpose_blog_post(
        blog_input,
        use_llm=True,
        brand_voice_prompt=voice_prompt,
        allowed_numeric_sources=allowed_numbers,
    )
    return {
        "blog_index": blog_index,
        "source_title": pkg.source_title,
        "asset_count": len(pkg.assets),
        "used_llm": pkg.used_llm,
        "used_brand_brief": brief is not None,
        "assets": [asdict(a) for a in pkg.assets],
    }


def _exec_dispatch(
    step: CampaignStep,
    plan: CampaignPlan,
    campaign: SelfMarketingCampaign,
    dry_run: bool,
) -> dict[str, Any]:
    """Schedule and dispatch the social assets for one blog."""
    blog_index = int(step.payload.get("blog_index", 0))

    repurpose_step_id = f"repurpose_blog_{blog_index + 1}"
    rep_step = next((s for s in plan.steps if s.id == repurpose_step_id), None)
    if rep_step is None or not rep_step.output:
        raise StepFailure(f"{repurpose_step_id} output missing")

    assets_raw = rep_step.output.get("assets") or []
    source_title = rep_step.output.get("source_title", "")
    asset_count = len(assets_raw)

    if dry_run or not assets_raw:
        return {
            "blog_index": blog_index,
            "source_title": source_title,
            "dispatched": 0,
            "dry_run": True,
        }

    # Attempt dispatch via the social adapter (fail-soft per asset).
    dispatched = 0
    errors: list[str] = []
    try:
        from dataclasses import fields as dc_fields
        from backend.social.models import RepurposedAsset, PlannedPost
        from backend.social.adapters import dispatch_plan
        from backend.social.models import MonthlyPlan

        # Wrap assets as PlannedPost objects for the dispatch_plan adapter.
        planned_posts: list[PlannedPost] = []
        for a_raw in assets_raw:
            planned_posts.append(PlannedPost(
                week=_week_for_index(blog_index),
                day="Mon",
                platform=a_raw.get("platform", "linkedin"),
                fmt=a_raw.get("fmt", "li_text"),
                pipeline_fn=a_raw.get("pipeline_fn", "educate"),
                theme="Self Marketing",
                cluster=a_raw.get("notes", ""),
                brief=a_raw.get("body", "")[:120],
                body=a_raw.get("body", ""),
                status="scheduled",
            ))

        result = dispatch_plan(planned_posts)
        dispatched = sum(1 for r in result if r.sent)
        errors = [r.error for r in result if not r.sent and r.error]
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("dispatch failed for blog_index=%d: %s", blog_index, exc)
        errors.append(str(exc))

    return {
        "blog_index": blog_index,
        "source_title": source_title,
        "asset_count": asset_count,
        "dispatched": dispatched,
        "errors": errors[:5],
    }


def _week_for_index(blog_index: int) -> int:
    """Map blog index to its dispatch week (0->2, 1->3)."""
    return blog_index + 2


def _exec_geo_audit(
    step: CampaignStep,
    plan: CampaignPlan,
    campaign: SelfMarketingCampaign,
    dry_run: bool,
) -> dict[str, Any]:
    """Week 4: run GEO audit on the Hustleforge site."""
    url = step.payload.get("url") or campaign.site_url

    if dry_run:
        return {"url": url, "dry_run": True, "seo_score": 0, "issue_count": 0}

    try:
        from backend.seo.models import AuditRequest
        from backend.seo.service import audit_site
        audit = audit_site(AuditRequest(url=url))
        return {
            "url": url,
            "seo_score": audit.seo_score,
            "issue_count": len(audit.issues),
            "issues": [i.model_dump() for i in audit.issues[:10]],
        }
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("geo audit failed: %s", exc)
        return {"url": url, "error": str(exc), "seo_score": 0, "issue_count": 0}


def _exec_month_summary(
    step: CampaignStep,
    plan: CampaignPlan,
    campaign: SelfMarketingCampaign,
    dry_run: bool,
) -> dict[str, Any]:
    """Week 4: produce month summary and queue next topics."""
    blogs_published: list[str] = []
    total_dispatched = 0
    platforms_hit: set[str] = set()

    for s in plan.steps:
        if s.id.startswith("generate_blog_") and s.status == "done":
            title = s.output.get("title", "")
            if title:
                blogs_published.append(title)
        if s.id.startswith("dispatch_blog_") and s.status == "done":
            total_dispatched += s.output.get("dispatched", 0)
            for a in (s.output.get("assets") or []):
                plat = a.get("platform")
                if plat:
                    platforms_hit.add(plat)

    geo_step = next((s for s in plan.steps if s.id == "geo_audit"), None)
    seo_score = (geo_step.output.get("seo_score") if geo_step and geo_step.output else 0) or 0

    year_str, month_str = plan.cycle_month.split("-")
    next_month = int(month_str) % 12 + 1
    next_year = int(year_str) + (1 if next_month == 1 else 0)
    next_topics = [
        campaign.keyword_for_index(next_month + i) for i in range(campaign.blogs_per_month)
    ]

    return {
        "cycle_month": plan.cycle_month,
        "blogs_published": blogs_published,
        "total_social_posts_dispatched": total_dispatched,
        "platforms_hit": sorted(platforms_hit),
        "seo_score": seo_score,
        "next_month_topics": next_topics,
    }


# Step executor registry
_EXECUTORS: dict[str, Callable[..., dict[str, Any]]] = {
    "content.plan_topics": _exec_plan_topics,
    "content.generate_blog": _exec_generate_blog,
    "social.repurpose": _exec_repurpose,
    "social.dispatch": _exec_dispatch,
    "seo.geo_audit": _exec_geo_audit,
    "campaign.month_summary": _exec_month_summary,
}


# ---------------------------------------------------------------------------
# Public DAG runner
# ---------------------------------------------------------------------------

def run_monthly_campaign(
    month: int,
    year: int,
    *,
    campaign: SelfMarketingCampaign | None = None,
    dry_run: bool = False,
) -> CampaignResult:
    """Plan + execute one monthly self-marketing cycle end-to-end.

    Steps execute in dependency order.  Each step's status + output persist
    to plan.json so a partial run resumes on the next call (idempotency by
    step id + status).  Step failures are fail-soft: logged and recorded but
    execution continues for independent downstream steps.

    Parameters
    ----------
    month:
        Calendar month (1-12).
    year:
        Four-digit year.
    campaign:
        Campaign config to use.  Defaults to HUSTLEFORGE_CAMPAIGN.
    dry_run:
        When True, all side-effecting steps (blog gen, dispatch, audit) are
        skipped and return placeholder output.  Useful for testing the DAG.
    """
    started = _now_iso()
    campaign = campaign or HUSTLEFORGE_CAMPAIGN
    cycle_month = _cycle_month_str(month, year)
    campaign_id = campaign.brand.lower().replace(" ", "_")

    plan_path = _campaign_dir(campaign_id, cycle_month) / "plan.json"
    plan: CampaignPlan

    if plan_path.exists():
        try:
            raw = json.loads(plan_path.read_text(encoding="utf-8"))
            plan = CampaignPlan(
                plan_id=raw["plan_id"],
                campaign_id=raw["campaign_id"],
                cycle_month=raw["cycle_month"],
                created_at=raw.get("created_at", started),
                updated_at=raw.get("updated_at", ""),
                status=raw.get("status", "planned"),
                steps=[CampaignStep(**s) for s in raw.get("steps", [])],
            )
            _LOG.info("campaign cycle resumed plan_id=%s", plan.plan_id)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("campaign plan reload failed, rebuilding: %s", exc)
            plan = _build_plan(campaign, cycle_month)
    else:
        plan = _build_plan(campaign, cycle_month)

    plan.status = "running"
    _persist_plan(plan)

    # DAG walk: iterate until no further progress is possible.
    by_id = {s.id: s for s in plan.steps}
    result_steps: list[CampaignStepResult] = []

    def _deps_ok(step: CampaignStep) -> bool:
        return all(by_id[d].status in ("done", "failed") for d in step.depends_on)

    made_progress = True
    while made_progress:
        made_progress = False
        for step in plan.steps:
            if step.status != "pending":
                continue
            if not _deps_ok(step):
                continue
            executor = _EXECUTORS.get(step.type)
            if executor is None:
                step.status = "failed"
                step.error = f"no executor for type={step.type}"
                result_steps.append(CampaignStepResult(
                    id=step.id, type=step.type, status="failed", detail=step.error,
                ))
                _persist_plan(plan)
                made_progress = True
                continue

            t0 = time.monotonic()
            step.status = "running"
            step.started_at = _now_iso()
            _persist_plan(plan)
            try:
                output = executor(step, plan, campaign, dry_run) or {}
                step.output = output
                step.status = "done"
                step.finished_at = _now_iso()
                detail = ", ".join(
                    f"{k}={v}" for k, v in output.items()
                    if k not in ("assets", "issues", "topics")
                )[:200]
                result_steps.append(CampaignStepResult(
                    id=step.id, type=step.type, status="done",
                    detail=detail, elapsed_ms=_ms_since(t0),
                ))
                made_progress = True
            except Exception as exc:  # noqa: BLE001
                step.status = "failed"
                step.error = str(exc)
                step.finished_at = _now_iso()
                result_steps.append(CampaignStepResult(
                    id=step.id, type=step.type, status="failed",
                    detail=str(exc), elapsed_ms=_ms_since(t0),
                ))
                made_progress = True
            finally:
                _persist_plan(plan)

    # Mark still-pending steps as skipped (all deps blocked by failures).
    for step in plan.steps:
        if step.status == "pending":
            step.status = "skipped"
            step.error = "dependency not met"
            result_steps.append(CampaignStepResult(
                id=step.id, type=step.type, status="skipped",
                detail="dependency not met",
            ))
    _persist_plan(plan)

    plan.status = "complete" if all(s.status == "done" for s in plan.steps) else "failed"
    _persist_plan(plan)

    summary_step = next(
        (s for s in reversed(plan.steps) if s.id == "month_summary"), None
    )
    summary = (summary_step.output if summary_step and summary_step.output else {})

    return CampaignResult(
        campaign_id=campaign_id,
        cycle_month=cycle_month,
        plan_id=plan.plan_id,
        plan_path=str(plan_path),
        ok=plan.status == "complete",
        steps=result_steps,
        summary=summary,
        ts=started,
    )


__all__ = [
    "SelfMarketingCampaign",
    "CampaignStep",
    "CampaignPlan",
    "CampaignStepResult",
    "CampaignResult",
    "StepFailure",
    "run_monthly_campaign",
]
