"""Pure-Python pipeline stages for the proposal workcell.

The five stages chain into ``service.generate_proposal``:

    plan_workflow -> select_templates -> compile_workflow -> validate_workflow

Each stage is deterministic and side-effect-free.
"""

from __future__ import annotations

from .models import (
    CompiledWorkflow,
    OnboardingIntake,
    ProposalValidation,
    TaskPlan,
    TemplateDefinition,
    WorkflowEdge,
    WorkflowNode,
)
from .templates import find_template


MAX_WORKFLOW_STEPS = 5
MAX_EXTERNAL_TOOLS = 3
MAX_TEMPLATES = 3


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def plan_workflow(intake: OnboardingIntake) -> TaskPlan:
    """Dedupe the three want-lists into a :class:`TaskPlan`."""
    return TaskPlan(
        triggers=_dedupe(intake.triggers_wanted),
        actions=_dedupe(intake.actions_wanted),
        notifications=_dedupe(intake.notifications_wanted),
    )


def select_templates(
    plan: TaskPlan, registry: dict[str, TemplateDefinition]
) -> list[TemplateDefinition]:
    """Return matching templates for every want; skip wants with no match."""
    out: list[TemplateDefinition] = []
    seen_ids: set[str] = set()

    def _try(type_: str, want: str) -> None:
        tpl = find_template(type_, want)
        if tpl is None:
            for candidate in registry.values():
                if candidate.type != type_:
                    continue
                attr = f"supported_{type_}s"
                if want in getattr(candidate, attr, []):
                    tpl = candidate
                    break
        if tpl is not None and tpl.template_id not in seen_ids:
            seen_ids.add(tpl.template_id)
            out.append(tpl)

    for want in plan.triggers:
        _try("trigger", want)
    for want in plan.actions:
        _try("action", want)
    for want in plan.notifications:
        _try("notification", want)
    return out


def compile_workflow(plan: TaskPlan, templates: list[TemplateDefinition]) -> CompiledWorkflow:
    """Chain trigger -> actions -> notifications into a linear DAG."""
    triggers = [t for t in templates if t.type == "trigger"]
    actions = [t for t in templates if t.type == "action"]
    notifications = [t for t in templates if t.type == "notification"]

    nodes: list[WorkflowNode] = []
    edges: list[WorkflowEdge] = []
    used_tools: list[str] = []
    seen_tools: set[str] = set()

    def _push_tools(tpl: TemplateDefinition) -> None:
        # One tool per node — pick the first supported tool not already
        # claimed. ``supported_tools`` lists candidates the template can
        # bind to (e.g. ses / smtp / gmail for email_send); the workflow
        # actually uses just one of them.
        for tool in tpl.supported_tools:
            if tool not in seen_tools:
                seen_tools.add(tool)
                used_tools.append(tool)
                return

    prev_id: str | None = None
    if triggers:
        t0 = triggers[0]
        node = WorkflowNode(
            node_id="trigger_0",
            kind="trigger",
            template_id=t0.template_id,
            description=t0.description,
        )
        nodes.append(node)
        _push_tools(t0)
        prev_id = node.node_id

    for i, tpl in enumerate(actions):
        nid = f"action_{i}"
        nodes.append(
            WorkflowNode(
                node_id=nid, kind="action", template_id=tpl.template_id, description=tpl.description
            )
        )
        _push_tools(tpl)
        if prev_id is not None:
            edges.append(WorkflowEdge(source_node_id=prev_id, target_node_id=nid))
        prev_id = nid

    for i, tpl in enumerate(notifications):
        nid = f"notify_{i}"
        nodes.append(
            WorkflowNode(
                node_id=nid,
                kind="notification",
                template_id=tpl.template_id,
                description=tpl.description,
            )
        )
        _push_tools(tpl)
        if prev_id is not None:
            edges.append(WorkflowEdge(source_node_id=prev_id, target_node_id=nid))
        prev_id = nid

    return CompiledWorkflow(
        nodes=nodes,
        edges=edges,
        total_steps=len(nodes),
        used_tools=used_tools,
    )


def validate_workflow(workflow: CompiledWorkflow) -> ProposalValidation:
    """Apply the three guardrails + per-node template presence."""
    reasons: list[str] = []
    if workflow.total_steps < 1:
        reasons.append("empty_workflow: total_steps < 1")
    if workflow.total_steps > MAX_WORKFLOW_STEPS:
        reasons.append(f"workflow_steps_exceeded: {workflow.total_steps} > {MAX_WORKFLOW_STEPS}")
    if len(workflow.used_tools) > MAX_EXTERNAL_TOOLS:
        reasons.append(f"tools_exceeded: {len(workflow.used_tools)} > {MAX_EXTERNAL_TOOLS}")
    distinct_templates = {n.template_id for n in workflow.nodes}
    if len(distinct_templates) > MAX_TEMPLATES:
        reasons.append(f"templates_exceeded: {len(distinct_templates)} > {MAX_TEMPLATES}")
    for n in workflow.nodes:
        if not n.template_id:
            reasons.append(f"node_missing_template: {n.node_id}")
    return ProposalValidation(passes=(not reasons), reasons=reasons)
