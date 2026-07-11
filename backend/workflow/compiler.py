"""Compile a scope_planner TaskPlan into an n8n workflow.

Layout is a simple left-to-right chain: trigger -> each action -> each
notification, one column (220px) apart. A separate **failure branch** is always
appended (an ``errorTrigger`` wired to a Slack/Discord alert) because every
HustleForge worked example ships failure alerts — it's a quality signature, not
an optional extra.

The compile is deterministic and offline. When ``use_llm`` is set, an optional
budget-gated enrichment pass fills node parameters from the intake text; it
fail-softs to the deterministic defaults on any error.
"""
from __future__ import annotations

import copy
import logging

from backend.services.scope_planner import TaskPlan
from backend.workflow.models import N8nNode, N8nWorkflow
from backend.workflow.node_library import (
    Spec,
    action_spec,
    error_alert_spec,
    notification_spec,
    trigger_spec,
)

_LOG = logging.getLogger("samus.workflow.compiler")

_COL = 220
_ROW = 300
_X0 = 240


def compile_workflow(
    plan: TaskPlan,
    *,
    name: str,
    intake_text: str = "",
    use_llm: bool = False,
    settings=None,
) -> N8nWorkflow:
    """Compile ``plan`` into an :class:`N8nWorkflow`. Always succeeds."""
    wf = N8nWorkflow(name=name or "Hustleforge Workflow")
    used: set[str] = set()

    # --- trigger (default to a webhook if intake parsed none) -------------
    trig_label = plan.triggers[0] if plan.triggers else "form_submission"
    trigger = _node(trigger_spec(trig_label), _label_name(trig_label), "trigger", (_X0, _ROW), used)
    wf.nodes.append(trigger)
    prev = trigger.name
    x = _X0 + _COL

    # --- actions, then notifications, in order ----------------------------
    chain = [("action", lbl) for lbl in plan.actions] + [("notification", lbl) for lbl in plan.notifications]
    for kind, label in chain:
        spec = action_spec(label, plan.tools) if kind == "action" else notification_spec(label, plan.tools)
        node = _node(spec, _label_name(label), kind, (x, _ROW), used)
        wf.nodes.append(node)
        wf.connect(prev, node.name)
        prev = node.name
        x += _COL

    # --- failure branch (always) ------------------------------------------
    err = _node({"type": "n8n-nodes-base.errorTrigger", "type_version": 1.0, "parameters": {}, "credential_hint": ""},
                "On Error", "error_trigger", (_X0, _ROW + 260), used)
    # Reuse the channel the plan already uses for alerts (notifications carry the
    # discord_webhook label; tools carry slack), so the failure branch matches.
    alert = _node(error_alert_spec(plan.tools + plan.notifications), "Failure Alert",
                  "notification", (_X0 + _COL, _ROW + 260), used)
    wf.nodes.append(err)
    wf.nodes.append(alert)
    wf.connect(err.name, alert.name)

    # --- optional LLM enrichment (fail-soft) ------------------------------
    if use_llm:
        try:
            from backend.workflow.enrich import enrich_workflow

            enrich_workflow(wf, plan, intake_text, settings=settings)
        except Exception as exc:  # noqa: BLE001 — never let enrichment break compile
            _LOG.info("workflow enrich skipped (%s)", type(exc).__name__)

    return wf


def _node(spec: Spec, base_name: str, kind: str, pos: tuple[int, int], used: set[str]) -> N8nNode:
    name = _unique(base_name, used)
    return N8nNode(
        name=name,
        type=spec["type"],
        type_version=float(spec.get("type_version", 1.0)),
        position=pos,
        parameters=copy.deepcopy(spec.get("parameters", {})),
        kind=kind,
        credential_hint=spec.get("credential_hint", ""),
    )


def _label_name(label: str) -> str:
    return label.replace("_", " ").title()


def _unique(base: str, used: set[str]) -> str:
    name = base
    i = 2
    while name in used:
        name = f"{base} {i}"
        i += 1
    used.add(name)
    return name


__all__ = ["compile_workflow"]
