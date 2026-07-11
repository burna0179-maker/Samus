"""HTTP-adapter handlers for the social engine — dict in, dict out.

These keep the FastAPI ``/work`` route thin: each handler parses a plain
payload dict defensively (no raise on missing fields — sensible defaults) and
returns a JSON-serializable dict. Dispatch stays DRY-RUN (it routes through
``adapters.dispatch_plan``, which honours ``SAMUS_SOCIAL_DRY_RUN``).
"""
from __future__ import annotations

from dataclasses import asdict

from backend.social.adapters import dispatch_plan
from backend.social.calendar import mix_distribution, plan_month
from backend.social.models import BlogInput, PlannedPost
from backend.social.repurpose import repurpose_blog_post


def handle_repurpose(payload: dict) -> dict:
    blog = BlogInput(
        title=str(payload.get("title") or ""),
        url=str(payload.get("url") or ""),
        summary=str(payload.get("summary") or ""),
        key_points=[str(k) for k in (payload.get("key_points") or [])],
        cluster=str(payload.get("cluster") or ""),
    )
    pkg = repurpose_blog_post(blog, use_llm=bool(payload.get("use_llm", False)))
    return {
        "source_title": pkg.source_title,
        "source_url": pkg.source_url,
        "used_llm": pkg.used_llm,
        "assets": [asdict(a) for a in pkg.assets],
    }


def handle_plan(payload: dict) -> dict:
    plan = plan_month(
        int(payload.get("month") or 1),
        str(payload.get("cluster") or ""),
        weeks=int(payload.get("weeks") or 4),
    )
    return {
        "month": plan.month,
        "theme": plan.theme,
        "cluster": plan.cluster,
        "post_count": len(plan.posts),
        "mix": mix_distribution(plan.posts),
        "posts": [asdict(p) for p in plan.posts],
    }


def _planned_post(d: dict) -> PlannedPost:
    return PlannedPost(
        week=int(d.get("week", 1)),
        day=str(d.get("day", "Mon")),
        platform=d.get("platform", "linkedin"),
        fmt=d.get("fmt", "li_text"),
        pipeline_fn=d.get("pipeline_fn", "educate"),
        theme=str(d.get("theme", "")),
        cluster=str(d.get("cluster", "")),
        brief=str(d.get("brief", "")),
        body=str(d.get("body", "")),
        scheduled_at=str(d.get("scheduled_at", "")),
        stake_sentence=str(d.get("stake_sentence", "")),
        status=d.get("status", "planned"),
    )


def handle_dispatch(payload: dict) -> dict:
    """DRY-RUN by construction — dispatch_plan honours SAMUS_SOCIAL_DRY_RUN."""
    posts = [_planned_post(d) for d in (payload.get("posts") or []) if isinstance(d, dict)]
    limit = payload.get("limit")
    raw_map = payload.get("image_urls_map") or {}
    image_urls_map = {int(k): list(v) for k, v in raw_map.items() if isinstance(v, list)}
    results = dispatch_plan(
        posts,
        limit=int(limit) if limit is not None else None,
        image_urls_map=image_urls_map or None,
    )
    return {
        "dispatched": len(results),
        "dry_run": sum(1 for r in results if r.dry_run),
        "sent": sum(1 for r in results if r.sent),
        "results": [asdict(r) for r in results],
    }
