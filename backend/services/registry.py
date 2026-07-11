"""Service-tier SKU registry. Stream 4 (catalog/) consumes this to populate the public catalog."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ServiceSku:
    sku_id: str
    display_name: str
    price_usd_cents: Optional[int]   # None => price TBD (operator-configurable)
    sla_hours: int
    description: str
    delivery_template_path: str      # relative to backend/services/
    stripe_product_id: Optional[str] = None
    stripe_price_id: Optional[str] = None
    scope_gates_enforced: bool = False    # only Workflow Rescue runs hard scope gates
    onboarding_label: str = ""            # label as shown on the public onboarding form
    tags: tuple[str, ...] = field(default_factory=tuple)


SERVICE_SKUS: dict[str, ServiceSku] = {
    "service_workflow_rescue": ServiceSku(
        sku_id="service_workflow_rescue",
        display_name="48-Hour Workflow Rescue",
        price_usd_cents=50000,
        sla_hours=48,
        description=(
            "Fixed-scope, single-workflow replacement. Operator picks one bottleneck "
            "(lead routing, invoice send, follow-up sequence, etc.), builds it from "
            "approved templates, delivers in 48 hours. Hard scope gates: max 5 steps, "
            "3 external tools, 3 templates."
        ),
        delivery_template_path="workflow_rescue/delivery_template.md",
        scope_gates_enforced=True,
        onboarding_label="48-Hour Workflow Rescue",
        tags=("fixed_scope", "single_workflow", "48h"),
    ),
    "service_workflow_buildout": ServiceSku(
        sku_id="service_workflow_buildout",
        display_name="Workflow System Buildout",
        # Quote-based: $2,500-$3,000 typical range. Final price is set per
        # engagement based on spec + difficulty; operator quotes the customer
        # in conversation before generating a custom Stripe invoice. The
        # cents value here is the floor used in upsell composer messaging
        # ("starting from $2,500"); the static Stripe price ($2,500) is the
        # buy-link price for customers who self-serve at the floor.
        price_usd_cents=250000,
        sla_hours=14 * 24,
        description=(
            "Multi-step, multi-workflow system integrated end-to-end to replace "
            "broken or unlinked systems. Discovery → build → integrate → validate "
            "→ handoff over 14 days. Pricing is human-in-the-loop ($2,500-$3,000 "
            "typical, scoped per engagement)."
        ),
        delivery_template_path="workflow_buildout/scope_template.md",
        scope_gates_enforced=False,
        onboarding_label="Workflow System Buildout",
        tags=("custom_scope", "multi_workflow", "14d", "quote_based"),
    ),
    "service_ai_ops_partner_build": ServiceSku(
        sku_id="service_ai_ops_partner_build",
        display_name="AI Ops Partner — Build",
        # Quote-based: $2,000-$5,000 typical range, scoped per engagement.
        # No static Stripe product — every customer is invoiced manually
        # after the scoping conversation. The floor is documented in the
        # description and used in upsell composer messaging.
        price_usd_cents=200000,
        sla_hours=30 * 24,        # 30 days, multi-phase build
        description=(
            "One-time engagement to build a standing operations engine: "
            "integrated automations, monitoring, runbooks, and the handoff "
            "package for ongoing AI Ops Partner retainer. Pricing is "
            "human-in-the-loop ($2,000-$5,000 typical, scoped per engagement)."
        ),
        delivery_template_path="ai_ops_partner_build/scope_template.md",
        scope_gates_enforced=False,
        onboarding_label="AI Ops Partner Program",
        tags=("custom_scope", "ops_engine", "30d", "quote_based"),
    ),
    "service_seo_implementation": ServiceSku(
        sku_id="service_seo_implementation",
        display_name="SEO Implementation",
        price_usd_cents=None,     # TBD — operator sets per-engagement after audit results land
        sla_hours=7 * 24,
        description=(
            "Apply prioritized fix list from an existing SEO Audit deliverable. "
            "Technical, on-page, content, and local fixes applied to the customer "
            "site (or delivered as a versioned change set + apply-instructions when "
            "we don't have site access). 7-day SLA from scope confirmation."
        ),
        delivery_template_path="seo_implementation/delivery_template.md",
        scope_gates_enforced=False,
        onboarding_label="SEO Audit & Fix",   # form bundles audit+impl as one option
        tags=("seo", "audit_followon", "7d"),
    ),
}


def get_sku(sku_id: str) -> ServiceSku:
    """Lookup; raises KeyError on unknown sku_id."""
    if sku_id not in SERVICE_SKUS:
        raise KeyError(f"unknown service sku: {sku_id!r}")
    return SERVICE_SKUS[sku_id]


def list_skus() -> list[ServiceSku]:
    """Stable iteration order matches dict insertion order."""
    return list(SERVICE_SKUS.values())


def sku_for_onboarding_label(label: str) -> Optional[ServiceSku]:
    """Reverse-lookup from public onboarding form checkbox label to SKU. Tolerant to whitespace/case."""
    norm = (label or "").strip().lower()
    if not norm:
        return None
    for sku in SERVICE_SKUS.values():
        if sku.onboarding_label.strip().lower() == norm:
            return sku
    return None
