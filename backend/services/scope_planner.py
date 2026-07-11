"""Scope planner: turn raw onboarding intake into a structured TaskPlan + scope artifact per SKU."""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from backend.services.registry import ServiceSku, get_sku


_LOG = logging.getLogger("samus.services.scope_planner")


# ---------------------------------------------------------------------------
# Plan structures (mirror recovery/fixed_scope_template_pipeline.TaskPlan)
# ---------------------------------------------------------------------------

@dataclass
class TaskPlan:
    triggers: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    notifications: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)


@dataclass
class ScopeArtifact:
    sku_id: str
    customer_email: str
    bottleneck_summary: str
    plan: TaskPlan
    deliverables: list[str]
    out_of_scope: list[str]
    estimated_steps: int
    estimated_templates: int
    scope_gates_enforced: bool
    out_of_scope_reason: Optional[str] = None     # set by validate_scope when gates trip


# ---------------------------------------------------------------------------
# Heuristic intake parser (no LLM call — fast deterministic first cut)
# ---------------------------------------------------------------------------

# Phrase → trigger/action/tool mapping. Conservative on purpose: a wrong inference
# is worse than a missing one (operator catches missing during the manual build).
_TRIGGER_PATTERNS = [
    (re.compile(r"\b(form\s*submission|squarespace|typeform|jotform|webhook)\b", re.I), "form_submission"),
    (re.compile(r"\b(new\s*lead|inbound\s*lead|lead\s*comes?\s*in)\b", re.I), "new_lead"),
    (re.compile(r"\b(email\s*arrives?|email\s*received|inbox)\b", re.I), "email_received"),
    (re.compile(r"\b(invoice\s*paid|stripe\s*payment|payment\s*received)\b", re.I), "payment_received"),
    (re.compile(r"\b(calendar\s*event|meeting\s*booked|booking)\b", re.I), "booking_created"),
    (re.compile(r"\b(schedule|cron|every\s*day|nightly|weekly|hourly)\b", re.I), "schedule_recurring"),
]

_ACTION_PATTERNS = [
    (re.compile(r"\b(send|push|deliver).*?(slack|teams)\b", re.I), "post_to_slack"),
    (re.compile(r"\b(send|deliver|fire).*?email\b", re.I), "send_email"),
    (re.compile(r"\b(create|add|push).*?(hubspot|salesforce|pipedrive|crm|contact)\b", re.I), "create_crm_record"),
    (re.compile(r"\b(generate|create|send).*?invoice\b", re.I), "generate_invoice"),
    (re.compile(r"\b(append|write|log).*?(sheet|airtable|notion)\b", re.I), "append_to_sheet"),
    (re.compile(r"\b(text|sms|twilio)\b", re.I), "send_sms"),
    (re.compile(r"\b(assign|route).*?(rep|agent|owner|sales)\b", re.I), "route_to_owner"),
    (re.compile(r"\b(follow[\s-]*up|nudge|reminder)\b", re.I), "schedule_followup"),
]

_NOTIFICATION_PATTERNS = [
    (re.compile(r"\b(notify|alert|tell|ping).*?(me|team|operator|owner)\b", re.I), "notify_operator"),
    (re.compile(r"\b(discord|webhook)\b", re.I), "discord_webhook"),
]

_TOOL_PATTERNS = [
    (re.compile(r"\bhubspot\b", re.I), "hubspot"),
    (re.compile(r"\bsalesforce\b", re.I), "salesforce"),
    (re.compile(r"\bpipedrive\b", re.I), "pipedrive"),
    (re.compile(r"\bstripe\b", re.I), "stripe"),
    (re.compile(r"\bslack\b", re.I), "slack"),
    (re.compile(r"\bnotion\b", re.I), "notion"),
    (re.compile(r"\bairtable\b", re.I), "airtable"),
    (re.compile(r"\b(google\s*sheets?|gsheet)\b", re.I), "google_sheets"),
    (re.compile(r"\bzapier\b", re.I), "zapier"),
    (re.compile(r"\bmake\.com\b|\bintegromat\b", re.I), "make"),
    (re.compile(r"\bn8n\b", re.I), "n8n"),
    (re.compile(r"\b(gmail|google\s*workspace)\b", re.I), "gmail"),
    (re.compile(r"\bcalendly\b", re.I), "calendly"),
    (re.compile(r"\btwilio\b", re.I), "twilio"),
    (re.compile(r"\bsquarespace\b", re.I), "squarespace"),
]


def _extract(text: str, patterns: list[tuple[re.Pattern[str], str]]) -> list[str]:
    out: list[str] = []
    for pat, label in patterns:
        if pat.search(text) and label not in out:
            out.append(label)
    return out


def _summarize_bottleneck(text: str, max_chars: int = 240) -> str:
    """Single-sentence-ish summary suitable for the customer-facing scope doc."""
    norm = " ".join((text or "").split())
    if len(norm) <= max_chars:
        return norm
    return norm[:max_chars].rsplit(" ", 1)[0] + "…"


# ---------------------------------------------------------------------------
# Per-SKU scope generation
# ---------------------------------------------------------------------------

def _plan_from_intake(intake: dict[str, Any]) -> TaskPlan:
    """Parse free-text bottleneck + needs into a TaskPlan."""
    bottleneck = (intake.get("bottleneck") or "").strip()
    needs = intake.get("needs") or []
    if isinstance(needs, str):
        needs = [needs]
    blob = bottleneck + "\n" + " ".join(str(n) for n in needs)

    return TaskPlan(
        triggers=_extract(blob, _TRIGGER_PATTERNS),
        actions=_extract(blob, _ACTION_PATTERNS),
        notifications=_extract(blob, _NOTIFICATION_PATTERNS),
        tools=_extract(blob, _TOOL_PATTERNS),
    )


def _workflow_rescue_scope(intake: dict[str, Any], sku: ServiceSku) -> ScopeArtifact:
    plan = _plan_from_intake(intake)
    steps_estimate = (
        (1 if plan.triggers else 0)
        + len(plan.actions)
        + len(plan.notifications)
    )
    # Estimate one template per distinct node (matches recovery compile_workflow).
    templates_estimate = steps_estimate
    deliverables = [
        "Single end-to-end automation built from approved templates",
        "Documented runbook (trigger → action map, retry behavior, failure modes)",
        "30-day operator support window for fixes/clarifications",
    ]
    out_of_scope = [
        "Multi-workflow systems (use Workflow System Buildout SKU)",
        "Custom integration code outside the supported tool list",
        "Ongoing monitoring / hosting (use the AI Ops Partner retainer)",
        "Manual data migration or data cleanup",
    ]
    return ScopeArtifact(
        sku_id=sku.sku_id,
        customer_email=(intake.get("email") or "").strip().lower(),
        bottleneck_summary=_summarize_bottleneck(intake.get("bottleneck", "")),
        plan=plan,
        deliverables=deliverables,
        out_of_scope=out_of_scope,
        estimated_steps=steps_estimate,
        estimated_templates=templates_estimate,
        scope_gates_enforced=True,
    )


def _workflow_buildout_scope(intake: dict[str, Any], sku: ServiceSku) -> ScopeArtifact:
    plan = _plan_from_intake(intake)
    steps_estimate = max(
        (1 if plan.triggers else 0) + len(plan.actions) + len(plan.notifications),
        3,
    )
    deliverables = [
        "Discovery + scope-of-work document (Day 1-2)",
        "Multi-workflow system build across the customer's tool stack (Day 3-9)",
        "Integration testing + data flow validation (Day 10-12)",
        "Handoff session + runbooks for each workflow (Day 13-14)",
        "30-day post-launch operator support window",
    ]
    out_of_scope = [
        "Source code custody — workflows live in the customer's accounts (Zapier/Make/n8n)",
        "Custom backend services or hosted code (separate engagement)",
        "Ongoing operations after the 30-day support window (move to retainer)",
    ]
    return ScopeArtifact(
        sku_id=sku.sku_id,
        customer_email=(intake.get("email") or "").strip().lower(),
        bottleneck_summary=_summarize_bottleneck(intake.get("bottleneck", "")),
        plan=plan,
        deliverables=deliverables,
        out_of_scope=out_of_scope,
        estimated_steps=steps_estimate,
        estimated_templates=steps_estimate,
        scope_gates_enforced=False,
    )


def _seo_implementation_scope(intake: dict[str, Any], sku: ServiceSku) -> ScopeArtifact:
    plan = TaskPlan(
        triggers=["seo_audit_delivered"],
        actions=[
            "apply_technical_fixes",
            "apply_on_page_fixes",
            "apply_content_fixes",
            "apply_local_fixes",
        ],
        notifications=["notify_operator"],
        tools=["website_cms", "google_search_console"],
    )
    deliverables = [
        "Prioritized fix list grouped by category (technical / on-page / content / local)",
        "Applied fixes (when site access provided) OR versioned change set + apply-instructions",
        "Before/after audit snapshot showing score delta",
        "Reindex submission to Google Search Console for changed pages",
    ]
    out_of_scope = [
        "Content writing beyond title/meta/H1 rewrites (use SEO Optimization retainer)",
        "Link building / outreach (separate engagement)",
        "Site redesign or theme changes",
    ]
    return ScopeArtifact(
        sku_id=sku.sku_id,
        customer_email=(intake.get("email") or "").strip().lower(),
        bottleneck_summary=_summarize_bottleneck(intake.get("bottleneck", "")),
        plan=plan,
        deliverables=deliverables,
        out_of_scope=out_of_scope,
        estimated_steps=len(plan.actions) + 1,
        estimated_templates=len(plan.actions),
        scope_gates_enforced=False,
    )


def _ai_ops_partner_build_scope(intake: dict[str, Any], sku: ServiceSku) -> ScopeArtifact:
    plan = TaskPlan(
        triggers=["discovery_call_completed"],
        actions=[
            "audit_current_ops_stack",
            "design_automation_blueprint",
            "build_core_automations",
            "wire_monitoring_and_alerts",
            "write_runbooks_and_handoff_package",
        ],
        notifications=["notify_operator", "weekly_status_to_customer"],
        tools=["customer_systems_inventory", "automation_platform", "monitoring_stack"],
    )
    deliverables = [
        "Discovery deliverable: current-state audit of operations stack + automation opportunity map",
        "Automation blueprint with prioritized build order + dependency graph",
        "Built + tested automations covering the agreed scope (3-8 workflows typical)",
        "Monitoring + alerting wired for every built workflow (failure visibility = mandatory)",
        "Runbook per workflow (operator + customer copies) and the full handoff package",
        "Handoff call to walk the customer through everything that was built",
    ]
    out_of_scope = [
        "Ongoing tuning / triage / new builds — that's what the AI Ops Partner Retainer covers",
        "Building automations against systems the customer doesn't own / can't grant access to",
        "Acting as the customer's IT helpdesk during the build window",
    ]
    return ScopeArtifact(
        sku_id=sku.sku_id,
        customer_email=(intake.get("email") or "").strip().lower(),
        bottleneck_summary=_summarize_bottleneck(intake.get("bottleneck", "")),
        plan=plan,
        deliverables=deliverables,
        out_of_scope=out_of_scope,
        estimated_steps=len(plan.actions),
        estimated_templates=len(plan.actions),
        scope_gates_enforced=False,
    )


_GENERATORS = {
    "service_workflow_rescue": _workflow_rescue_scope,
    "service_workflow_buildout": _workflow_buildout_scope,
    "service_seo_implementation": _seo_implementation_scope,
    "service_ai_ops_partner_build": _ai_ops_partner_build_scope,
}


def generate_scope(intake_payload: dict[str, Any], sku_id: str) -> ScopeArtifact:
    """Per-SKU scope artifact. Raises KeyError on unknown sku_id."""
    sku = get_sku(sku_id)
    gen = _GENERATORS.get(sku_id)
    if gen is None:
        raise KeyError(f"no scope generator wired for sku: {sku_id!r}")
    artifact = gen(intake_payload, sku)
    _LOG.info(
        "scope_generated",
        extra={
            "sku_id": sku_id,
            "email": artifact.customer_email,
            "steps": artifact.estimated_steps,
            "templates": artifact.estimated_templates,
        },
    )
    return artifact


# ---------------------------------------------------------------------------
# Markdown rendering (the customer-facing scope acknowledgment artifact)
# ---------------------------------------------------------------------------

def render_scope_markdown(artifact: ScopeArtifact, *, sku: ServiceSku) -> str:
    """Format the scope artifact as the customer-facing scope.md."""
    if sku.price_usd_cents is not None:
        price_line = f"${sku.price_usd_cents / 100:.2f}"
    else:
        price_line = "Price set at scope confirmation"
    sla_days = sku.sla_hours / 24
    sla_label = f"{sku.sla_hours}h" if sku.sla_hours < 48 else f"{sla_days:.0f}-day"

    lines: list[str] = []
    lines.append(f"# Scope Confirmation — {sku.display_name}")
    lines.append("")
    lines.append(f"**SKU:** `{sku.sku_id}`  ")
    lines.append(f"**Price:** {price_line}  ")
    lines.append(f"**SLA:** {sla_label} from scope confirmation  ")
    lines.append(f"**Customer:** {artifact.customer_email}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## What you told us")
    lines.append("")
    lines.append(f"> {artifact.bottleneck_summary or '(no bottleneck text provided)'}")
    lines.append("")
    lines.append("## What we're building")
    lines.append("")
    for d in artifact.deliverables:
        lines.append(f"- {d}")
    lines.append("")
    if artifact.plan.triggers or artifact.plan.actions or artifact.plan.notifications:
        lines.append("## Workflow plan (parsed from your intake)")
        lines.append("")
        if artifact.plan.triggers:
            lines.append(f"- **Triggers:** {', '.join(artifact.plan.triggers)}")
        if artifact.plan.actions:
            lines.append(f"- **Actions:** {', '.join(artifact.plan.actions)}")
        if artifact.plan.notifications:
            lines.append(f"- **Notifications:** {', '.join(artifact.plan.notifications)}")
        if artifact.plan.tools:
            lines.append(f"- **Tools:** {', '.join(artifact.plan.tools)}")
        lines.append("")
        lines.append(
            f"_Estimated complexity: {artifact.estimated_steps} step(s) across "
            f"{artifact.estimated_templates} template(s)._"
        )
        lines.append("")
    lines.append("## Not included")
    lines.append("")
    for o in artifact.out_of_scope:
        lines.append(f"- {o}")
    lines.append("")
    if artifact.out_of_scope_reason:
        lines.append("## Scope-gate flag")
        lines.append("")
        lines.append(
            f"_Heads up:_ initial parse triggered a scope guard "
            f"(`{artifact.out_of_scope_reason}`). We may split this into multiple "
            f"workflows or recommend the Workflow System Buildout SKU instead. "
            f"We'll confirm before any billing change."
        )
        lines.append("")
    lines.append("## Next step")
    lines.append("")
    lines.append(
        "Reply to this email with **\"confirm\"** to start the SLA clock. Reply with "
        "any edits and we'll resend the scope before kicking off."
    )
    lines.append("")
    lines.append("— Hustleforge")
    return "\n".join(lines)


def artifact_to_dict(artifact: ScopeArtifact) -> dict[str, Any]:
    """JSON-safe dict for logging + persistence."""
    return {
        **{k: v for k, v in asdict(artifact).items() if k != "plan"},
        "plan": asdict(artifact.plan),
    }
