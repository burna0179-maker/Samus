"""CLI for the n8n workflow engine — compile/validate a workflow from a scope.

``compile`` is free + offline (deterministic mapping). It builds the same
``ScopeArtifact`` the fulfillment path uses (via ``scope_planner.generate_scope``)
so the CLI output matches what a customer would receive.

Examples
--------
Print the n8n JSON + validation for a rescue scope::

    python -m backend.workflow.cli compile \\
        --sku service_workflow_rescue \\
        --bottleneck "Calendly booking -> Stripe invoice -> append to Google Sheet \\
                      -> SMS the tech via Twilio -> Discord alert on failure"

Write the full deliverable (workflow.json + runbook.md) to a dir, and deploy if armed::

    python -m backend.workflow.cli compile --sku service_workflow_rescue \\
        --bottleneck "..." --out ./out --deploy
"""
from __future__ import annotations

import argparse
import json
import sys


def _artifact(args: argparse.Namespace):
    from backend.services.scope_planner import generate_scope

    intake = {
        "email": "cli@hustleforge.local",
        "bottleneck": args.bottleneck or "",
        "needs": [n for n in (args.needs or "").split(";") if n.strip()],
    }
    return generate_scope(intake, args.sku)


def _cmd_compile(args: argparse.Namespace) -> int:
    from backend.common.config import get_settings
    from backend.services.registry import get_sku

    artifact = _artifact(args)
    settings = get_settings()

    if args.out or args.deploy:
        from backend.workflow.service import generate_workflow_deliverable

        report = generate_workflow_deliverable(
            artifact, out_dir=args.out or "./workflow_out", settings=settings,
            sku=get_sku(args.sku), deploy=args.deploy,
        )
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["valid"] else 1

    # stdout-only: compile + validate, no files written.
    from backend.workflow.compiler import compile_workflow
    from backend.workflow.validate import is_valid, validate_workflow

    wf = compile_workflow(artifact.plan, name=f"Hustleforge — {args.sku}",
                          intake_text=artifact.bottleneck_summary,
                          use_llm=args.use_llm, settings=settings)
    issues = validate_workflow(wf)
    print(json.dumps({
        "workflow": wf.to_dict(),
        "valid": is_valid(issues),
        "validation": [i.to_dict() for i in issues],
    }, indent=2, ensure_ascii=False))
    return 0 if is_valid(issues) else 1


def _cmd_validate(args: argparse.Namespace) -> int:
    from backend.common.config import get_settings
    from backend.workflow.compiler import compile_workflow
    from backend.workflow.validate import is_valid, validate_workflow

    artifact = _artifact(args)
    wf = compile_workflow(artifact.plan, name=f"Hustleforge — {args.sku}",
                          intake_text=artifact.bottleneck_summary, settings=get_settings())
    issues = validate_workflow(wf)
    print(json.dumps({"valid": is_valid(issues), "validation": [i.to_dict() for i in issues]},
                     indent=2, ensure_ascii=False))
    return 0 if is_valid(issues) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backend.workflow.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for name, func in (("compile", _cmd_compile), ("validate", _cmd_validate)):
        p = sub.add_parser(name)
        p.add_argument("--sku", required=True, help="service SKU id (e.g. service_workflow_rescue)")
        p.add_argument("--bottleneck", default="", help="free-text description of the manual process")
        p.add_argument("--needs", default="", help="semicolon-separated extra needs")
        p.add_argument("--use-llm", action="store_true", help="budget-gated LLM param enrichment")
        if name == "compile":
            p.add_argument("--out", default="", help="write workflow.json + runbook.md to this dir")
            p.add_argument("--deploy", action="store_true", help="deploy to n8n (only if armed in settings)")
        p.set_defaults(func=func)

    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
