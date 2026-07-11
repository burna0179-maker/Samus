"""The single entry point: a scope artifact -> a runnable n8n workflow deliverable.

Both the CLI and ``fulfill_service`` call :func:`generate_workflow_deliverable`.
It compiles the artifact's ``TaskPlan`` into an n8n workflow, validates it, writes
``workflow.json`` + ``runbook.md`` to the customer's deliverable directory, and
(when armed) deploys it. Returns a structured report; raises only on a genuine IO
error writing the artifact (the fulfillment caller wraps this fail-soft).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from backend.workflow.compiler import compile_workflow
from backend.workflow.deploy import deploy_workflow
from backend.workflow.runbook import render_runbook
from backend.workflow.validate import is_valid, validate_workflow

_LOG = logging.getLogger("samus.workflow.service")


def generate_workflow_deliverable(
    artifact,
    *,
    out_dir: str | Path,
    settings,
    sku=None,
    deploy: bool = True,
) -> dict[str, Any]:
    """Compile + validate + write (+ optionally deploy) the workflow deliverable.

    ``artifact`` is a :class:`backend.services.scope_planner.ScopeArtifact`.
    Returns ``{workflow_path, runbook_path, node_count, valid, validation, deploy}``.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    sku_name = getattr(sku, "display_name", "") or artifact.sku_id
    name = f"Hustleforge — {sku_name}"
    use_llm = bool(getattr(settings, "workflow_n8n_llm_enrich", False))

    wf = compile_workflow(
        artifact.plan,
        name=name,
        intake_text=getattr(artifact, "bottleneck_summary", ""),
        use_llm=use_llm,
        settings=settings,
    )
    issues = validate_workflow(wf)
    valid = is_valid(issues)

    workflow_path = out_dir / "workflow.json"
    runbook_path = out_dir / "runbook.md"
    workflow_path.write_text(
        json.dumps(wf.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    runbook_path.write_text(
        render_runbook(
            artifact.plan,
            wf,
            sku_name=sku_name,
            bottleneck=getattr(artifact, "bottleneck_summary", ""),
        ),
        encoding="utf-8",
    )

    deploy_report: dict[str, Any] = {"status": "skipped", "deployed": False}
    if deploy and valid:
        deploy_report = deploy_workflow(wf, settings=settings)
    elif deploy and not valid:
        deploy_report = {"status": "blocked_invalid", "deployed": False}

    _LOG.info(
        "workflow_deliverable sku=%s nodes=%d valid=%s deploy=%s",
        artifact.sku_id,
        len(wf.nodes),
        valid,
        deploy_report.get("status"),
    )
    return {
        "workflow_path": str(workflow_path),
        "runbook_path": str(runbook_path),
        "node_count": len(wf.nodes),
        "valid": valid,
        "validation": [i.to_dict() for i in issues],
        "deploy": deploy_report,
    }


__all__ = ["generate_workflow_deliverable"]
