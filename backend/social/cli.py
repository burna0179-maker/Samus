"""CLI for the social-content engine — generate, plan, and (DRY-RUN) dispatch.

Mirrors ``backend.outreach.run_campaign``: runs in the host venv, no Docker /
SQS dependency. All dispatch is DRY-RUN unless ``SAMUS_SOCIAL_DRY_RUN=false``
and platform credentials are seeded.

Examples
--------
Repurpose a blog post into the 6-asset package (templated, $0)::

    python -m backend.social.cli repurpose \\
        --title "The 44% rule in AI content" \\
        --url "https://hustleforge.ai/blog/the-44-percent-rule" \\
        --summary "44% of AI citations come from the first 30% of a page." \\
        --key-points "Front-load the answer;Add an FAQ block;Use schema markup" \\
        --cluster "AI visibility"

Add ``--use-llm`` to opt into budget-gated LLM generation.

Plan a month of social content and write it to a JSONL calendar::

    python -m backend.social.cli plan --month 1 --cluster "AI prospecting" \\
        --out ./calendar_month1.jsonl

Dry-run dispatch a saved calendar (writes ledger entries only)::

    python -m backend.social.cli dispatch --plan ./calendar_month1.jsonl --limit 5
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from backend.social.adapters import dispatch_plan
from backend.social.calendar import load_plan, mix_distribution, persist_plan, plan_month
from backend.social.models import BlogInput
from backend.social.repurpose import repurpose_blog_post


def _cmd_repurpose(args: argparse.Namespace) -> int:
    key_points = [p.strip() for p in (args.key_points or "").split(";") if p.strip()]
    blog = BlogInput(
        title=args.title,
        url=args.url or "",
        summary=args.summary or "",
        key_points=key_points,
        cluster=args.cluster or "",
    )
    package = repurpose_blog_post(blog, use_llm=args.use_llm)
    out = {
        "source_title": package.source_title,
        "source_url": package.source_url,
        "used_llm": package.used_llm,
        "assets": [asdict(a) for a in package.assets],
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    plan = plan_month(args.month, args.cluster, weeks=args.weeks)
    mix = mix_distribution(plan.posts)
    if args.out:
        n = persist_plan(plan, args.out)
        print(f"wrote {n} planned posts -> {args.out}")
    print(json.dumps({"month": plan.month, "theme": plan.theme, "cluster": plan.cluster, "post_count": len(plan.posts), "mix": mix}, indent=2, ensure_ascii=False))
    return 0


def _cmd_dispatch(args: argparse.Namespace) -> int:
    plan = load_plan(args.plan)
    results = dispatch_plan(plan.posts, limit=args.limit)
    sent = sum(1 for r in results if r.sent)
    dry = sum(1 for r in results if r.dry_run)
    print(
        json.dumps(
            {
                "dispatched": len(results),
                "sent": sent,
                "dry_run": dry,
                "results": [asdict(r) for r in results],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backend.social.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    rp = sub.add_parser("repurpose", help="blog post -> 6-asset social package")
    rp.add_argument("--title", required=True)
    rp.add_argument("--url", default="")
    rp.add_argument("--summary", default="")
    rp.add_argument("--key-points", default="", help="semicolon-separated")
    rp.add_argument("--cluster", default="")
    rp.add_argument("--use-llm", action="store_true", help="budget-gated LLM (default templated, $0)")
    rp.set_defaults(func=_cmd_repurpose)

    pl = sub.add_parser("plan", help="plan a month of social content")
    pl.add_argument("--month", type=int, required=True, help="1-based month index (cycles every 4)")
    pl.add_argument("--cluster", required=True, help="topic cluster the month anchors to")
    pl.add_argument("--weeks", type=int, default=4)
    pl.add_argument("--out", default="", help="write the plan to this JSONL path")
    pl.set_defaults(func=_cmd_plan)

    di = sub.add_parser("dispatch", help="DRY-RUN dispatch a saved calendar")
    di.add_argument("--plan", required=True, help="path to a JSONL calendar")
    di.add_argument("--limit", type=int, default=None, help="cap posts dispatched this run")
    di.set_defaults(func=_cmd_dispatch)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Social copy carries emoji / typographic unicode (it performs better on
    # feeds). Force utf-8 stdout so the host CLI doesn't crash on a legacy
    # Windows console codepage (cp1252). Containers are utf-8 already.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
