#!/usr/bin/env python3
"""
Onboarding form schema — hustleforge.tech /onboarding intake
Source: ChatGPT recovery chat 09 (template-onboarding.php + form field spec)

Canonical relationship:
- [NEW] commercial-site intake schema; feeds into intake/onboarding receiver
- Pairs with stripe_webhook_receiver.py product catalog
- Reference: memory project_hustleforge_commercial (launch blocked April 2026)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from backend.catalog.registry import sku as _sku


@dataclass
class FormField:
    name: str
    label: str
    kind: str                          # text | email | url | textarea | checkbox | select
    placeholder: Optional[str] = None
    required: bool = False
    options: List[str] = field(default_factory=list)


ONBOARDING_FORM = [
    FormField("full_name", "Full Name", "text", "Jane Smith", required=True),
    FormField("email", "Email Address", "email", "jane@yourcompany.com", required=True),
    FormField("company", "Company / Brand", "text", "Acme Inc.", required=True),
    FormField("website", "Website URL", "url", "https://yoursite.com"),
    FormField(
        "needs", "What do you need help with?", "checkbox",
        options=[
            "SEO Audit & Fix",
            "Monthly SEO Optimization",
            "SEO + Automation System",
            "48-Hour Workflow Rescue",
            "Workflow System Buildout",
            "AI Ops Partner Program",
            "Workflow Playbook",
            "Not sure yet",
        ],
    ),
    FormField(
        "bottleneck", "Describe your biggest bottleneck", "textarea",
        "What's slowing you down the most right now? What's breaking, taking too long, or costing you the most time?",
        required=True,
    ),
    FormField(
        "budget", "Monthly budget range", "select",
        options=["Under $150", "$150 – $500", "$500 – $2,000", "$2,000 – $5,000", "$5,000+"],
    ),
    FormField(
        "timeline", "Timeline", "select",
        options=["As soon as possible", "This month", "Next 30 days", "Next 90 days", "Just exploring"],
    ),
]


SUBMIT_CONFIG = {
    "button_text": "Submit & Start Onboarding",
    "success_message": "You're in the queue. We've received your information and will review it within one business day with a clear action path.",
    "trust_note": "Response within 24 hours. Priority is given to active purchases and urgent workflow bottlenecks.",
    "post_url": "/intake/onboarding",   # routes through samus-webhook-receiver:8081
}


# ----- Stripe buy-button product catalog (chat 09) -----
#
# HISTORY / why this is derived, not hard-coded:
# This dict used to carry a hard-coded ``buy_url`` per product. Four of the
# five silently went stale when the underlying Stripe payment links were
# regenerated (2026-04-11) and later archived — the exact dead-checkout
# failure mode ``backend/catalog/link_audit.py`` exists to catch. Commit
# cce6f0b reconciled those four in ``backend/catalog/registry.py`` but missed
# this mirror copy, which is how it desynced.
#
# FIX: ``buy_url`` and ``label`` are now DERIVED from the canonical SKU
# registry by ``sku_id``. A future link rotation updates ``registry.py`` once
# and this file follows automatically. Never re-hard-code a payment link here.
#
# The legacy per-product ``buy_button_id`` (Stripe Buy Button embeds) was
# REMOVED. Those are separate Stripe objects from the payment links, have no
# canonical home in the registry, are not covered by ``link_audit`` (it only
# queries ``GET /v1/payment_links``), and the five values here were all from
# the archived 05:xx generation (buy_btn_1TKt*/1TKu*) — stale, live-looking
# IDs sitting next to a ``pk_live`` key are a worse hazard than none. If the
# site needs embedded buy buttons, model them in the catalog so they can be
# audited too.

# Local onboarding-form product key -> canonical registry ``sku_id``.
_PRODUCT_SKU_IDS = {
    "48hr_workflow": "service_workflow_rescue",
    "seo_audit": "seo_audit",
    "seo_implementation": "service_seo_implementation",
    "seo_optimization": "retainer_seo_optimization",
}


def _catalog_product(sku_id: str) -> dict:
    entry = _sku(sku_id)
    return {"label": entry.display_name, "buy_url": entry.payment_link_url}


STRIPE_PRODUCTS = {
    key: _catalog_product(sku_id) for key, sku_id in _PRODUCT_SKU_IDS.items()
}

# NOTE — ``seo_automation`` ("SEO + Automation System") is intentionally NOT in
# the map above: it has no catalog SKU, so its link cannot be derived. Its old
# link (…so0f, created 2026-04-11 05:47) was regenerated the same day (14:02)
# to …so0j in the same batch as the four links above, and all four of those
# were later confirmed archived — so …so0f is almost certainly archived too.
# It needs either a catalog SKU (preferred) or live-Stripe confirmation of the
# current link before it can be trusted; do NOT paste a raw URL back here.
# Live candidate to verify + catalog: https://buy.stripe.com/fZufZg9cK06u6y2fe48so0j

PUBLISHABLE_KEY = ""  # redacted; recovery-only file, active code loads from env in backend/finance/
