"""Render the operator/customer runbook for a compiled workflow.

This is the "Documented runbook (trigger -> action map, retry behavior, failure
modes)" deliverable promised in ``scope_planner`` — now generated from the actual
compiled workflow rather than written by hand.
"""
from __future__ import annotations

from backend.services.scope_planner import TaskPlan
from backend.workflow.models import N8nWorkflow


def render_runbook(plan: TaskPlan, wf: N8nWorkflow, *, sku_name: str = "", bottleneck: str = "") -> str:
    lines: list[str] = []
    lines.append(f"# Runbook — {wf.name}")
    lines.append("")
    if sku_name:
        lines.append(f"**Service:** {sku_name}  ")
    lines.append(f"**Platform:** n8n  ")
    lines.append(f"**Nodes:** {len(wf.nodes)}")
    lines.append("")
    if bottleneck:
        lines.append("## The problem this solves")
        lines.append("")
        lines.append(f"> {bottleneck}")
        lines.append("")

    lines.append("## What it does")
    lines.append("")
    triggers = [n for n in wf.nodes if n.kind == "trigger"]
    if triggers:
        lines.append(f"1. **Trigger** — `{triggers[0].name}` ({triggers[0].type}) starts the run.")
    step = 2
    for n in wf.nodes:
        if n.kind in ("action", "notification"):
            lines.append(f"{step}. **{n.name}** — `{n.type}`")
            step += 1
    lines.append("")

    lines.append("## Failure handling")
    lines.append("")
    err = [n for n in wf.nodes if n.kind == "error_trigger"]
    alert = [n for n in wf.nodes if n.name.lower().startswith("failure alert")]
    if err and alert:
        lines.append(
            f"An **Error Trigger** (`{err[0].name}`) catches any failed execution and fires "
            f"`{alert[0].name}` so you hear about a break the moment it happens — not when a "
            f"customer complains. This branch runs independently of the main flow."
        )
    lines.append("")

    creds = [(n.name, n.credential_hint) for n in wf.nodes if n.credential_hint]
    if creds:
        lines.append("## Credentials to configure (in n8n)")
        lines.append("")
        for node_name, hint in creds:
            lines.append(f"- **{node_name}** → {hint}")
        lines.append("")

    lines.append("## How to import")
    lines.append("")
    lines.append("1. In n8n: **Workflows → ⋮ → Import from File** and choose `workflow.json`.")
    lines.append("2. Open each node above and connect its credential (see the list).")
    lines.append("3. Fill the placeholder fields (channel id, sheet id, phone number, message text).")
    lines.append("4. Run a test execution, confirm the failure branch fires (disconnect a credential to test).")
    lines.append("5. Toggle the workflow **Active**.")
    lines.append("")
    lines.append("— Hustleforge")
    return "\n".join(lines)


__all__ = ["render_runbook"]
