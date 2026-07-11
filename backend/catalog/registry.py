"""Canonical SKU registry — single source of truth for site SKU <-> Stripe IDs.

Every product the Samus site sells maps to exactly one CatalogEntry here. The
fulfillment_module field tells the gateway which Python module owns delivery
when a webhook fires for that SKU's price.

Stripe IDs in this file are LIVE (acct_1SQRZABdo4u4gpS7 / "HustleForge"). They
were audited and partially created on 2026-05-16 via the Stripe MCP. See
``stripe_sync_report.md`` for the full audit record.

To add or change a SKU, see ``README.md`` in this directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SkuCategory(str, Enum):
    SERVICE = "service"
    RETAINER = "retainer"
    DIGITAL = "digital"
    ADDON = "addon"


class FulfillmentModule(str, Enum):
    PRODUCTS = "backend.products.fulfill_digital"
    SERVICES = "backend.services.fulfill_service"
    RETAINER = "backend.retainer.enroll"
    LEGACY_SEO_AUDIT = "backend.fulfill"


@dataclass(frozen=True)
class CatalogEntry:
    sku_id: str
    display_name: str
    category: SkuCategory
    fulfillment_module: FulfillmentModule
    price_usd_cents: int          # for retainers, this is the monthly cents
    recurring: bool               # True for retainers
    stripe_product_id: Optional[str]
    stripe_price_id: Optional[str]
    payment_link_url: Optional[str]
    description: str


CATALOG: list[CatalogEntry] = [
    # --- Services (one-time) ---
    CatalogEntry(
        sku_id="service_website_build",
        display_name="Business Website Build",
        category=SkuCategory.SERVICE,
        fulfillment_module=FulfillmentModule.SERVICES,
        # Floor price $800; scoped per build (small local business 3-5 pages).
        # Invoiced manually — no live Stripe product yet (operator to create).
        price_usd_cents=80000,
        recurring=False,
        stripe_product_id=None,
        stripe_price_id=None,
        payment_link_url=None,
        description="A professional, mobile-friendly website for local businesses with no web presence or a broken/outdated site. Includes up to 5 pages, local SEO markup, Google Business integration, and a contact/quote form. Delivered within one week.",
    ),
    CatalogEntry(
        sku_id="service_workflow_rescue",
        display_name="48-Hour Workflow Rescue",
        category=SkuCategory.SERVICE,
        fulfillment_module=FulfillmentModule.SERVICES,
        price_usd_cents=50000,
        recurring=False,
        stripe_product_id="prod_U913rQk3aII2T4",
        stripe_price_id="price_1TAj1nBdo4u4gpS7Z4fGaYT0",
        payment_link_url="https://buy.stripe.com/4gMcN474C4mK4pUea08so0l",
        description="A production-ready workflow that saves hours every week from day one — scoped, built, and handed off in 48 hours.",
    ),
    CatalogEntry(
        sku_id="service_workflow_buildout",
        display_name="Workflow System Buildout",
        category=SkuCategory.SERVICE,
        fulfillment_module=FulfillmentModule.SERVICES,
        # NOTE: canonical table calls for $2,750; live Stripe price is $2,500.
        # Registry mirrors live Stripe to preserve checkout integrity; price
        # mismatch is flagged in stripe_sync_report.md for operator action.
        price_usd_cents=250000,
        recurring=False,
        stripe_product_id="prod_U913HTNX3D0lMH",
        stripe_price_id="price_1TAj1pBdo4u4gpS7JBbZ5SWJ",
        payment_link_url=None,
        description="Complete operating-system design for teams that need tools, handoffs, and execution logic rebuilt cleanly. Connects fragmented systems into a scalable execution layer, resolves broken handoffs, and clarifies ownership. Scoped engagement — submit interest and we'll define scope + timeline.",
    ),
    CatalogEntry(
        sku_id="service_seo_implementation",
        display_name="SEO Implementation",
        category=SkuCategory.SERVICE,
        fulfillment_module=FulfillmentModule.SERVICES,
        # NOTE: canonical table calls for $750; live Stripe price is $200.
        # Flagged in stripe_sync_report.md.
        price_usd_cents=20000,
        recurring=False,
        stripe_product_id="prod_UJXJbkZfIZOM5a",
        stripe_price_id="price_1TKuEeBdo4u4gpS7853xVNwz",
        payment_link_url="https://buy.stripe.com/4gM00i3SqaL84pU3vm8so0k",
        description="Fast execution of the highest-impact fixes from your SEO audit — page-level optimization, structure, and performance work.",
    ),
    CatalogEntry(
        sku_id="seo_audit",
        display_name="SEO Audit",
        category=SkuCategory.SERVICE,
        fulfillment_module=FulfillmentModule.LEGACY_SEO_AUDIT,
        # NOTE: canonical table calls for $500; live Stripe price is $149.
        # Flagged in stripe_sync_report.md.
        price_usd_cents=14900,
        recurring=False,
        stripe_product_id="prod_UJWzOjmJrxZxQq",
        stripe_price_id="price_1TKtvJBdo4u4gpS70PJf01xN",
        payment_link_url="https://buy.stripe.com/9B6fZgcoW7yWbSm6Hy8so0h",
        description="Full technical SEO audit covering page-level optimization, Core Web Vitals, keyword opportunities, and a prioritized action roadmap.",
    ),

    # --- AI Ops Partner build (one-time, quote-based — pairs with retainer below) ---
    CatalogEntry(
        sku_id="service_ai_ops_partner_build",
        display_name="AI Ops Partner — Build",
        category=SkuCategory.SERVICE,
        fulfillment_module=FulfillmentModule.SERVICES,
        # Quote-based: $2,000-$5,000 range, operator scopes per engagement.
        # price_usd_cents reflects the floor; payment_link_url is None because
        # this SKU is invoiced manually via custom Stripe invoices (the operator
        # generates one per customer with the negotiated price + any prior-tier
        # credit applied).
        price_usd_cents=200000,
        recurring=False,
        stripe_product_id=None,
        stripe_price_id=None,
        payment_link_url=None,
        description="One-time engagement to build a standing operations engine: integrated automations, monitoring, and runbooks. Price scoped per build ($2,000-$5,000 typical). Pairs with the AI Ops Partner monthly retainer for ongoing upkeep.",
    ),

    # --- Retainers (monthly recurring) ---
    # AI Ops Partner has two tiers reflecting two customer journeys:
    #   * Entry: customer self-serves from hustleforge.tech/ai-ops-partner/.
    #     $2k/mo, onboarding (audit + first-month roadmap) included. No
    #     separate build engagement.
    #   * Premium: customer arrives via the funnel
    #     Rescue → Buildout → AI Ops Partner — Build (one-time $2-5k) and
    #     then enters the premium retainer at $5k/mo for ongoing upkeep of
    #     the operations engine built during the build engagement.
    CatalogEntry(
        sku_id="retainer_ai_ops_partner_entry",
        display_name="AI Ops Partner",
        category=SkuCategory.RETAINER,
        fulfillment_module=FulfillmentModule.RETAINER,
        price_usd_cents=200000,
        recurring=True,
        # Reuses the existing AI Ops Partner Stripe product (originally at
        # $2k/mo — matches the public page price exactly).
        stripe_product_id="prod_U913mXXZXG9REI",
        stripe_price_id="price_1TAj1pBdo4u4gpS7GZGee1zn",
        payment_link_url=None,
        description="Entry retainer matching the hustleforge.tech/ai-ops-partner/ offer: onboarding (systems audit + first-month roadmap), continuous workflow optimization, monthly new automation builds, dedicated point of contact, monthly performance + ROI reviews, same-week response on requests, ongoing documentation updates.",
    ),
    CatalogEntry(
        sku_id="retainer_ai_ops_partner_premium",
        display_name="AI Ops Partner — Premium",
        category=SkuCategory.RETAINER,
        fulfillment_module=FulfillmentModule.RETAINER,
        price_usd_cents=500000,
        recurring=True,
        stripe_product_id="prod_UWt4i2QB7BZlFx",
        stripe_price_id="price_1TXpJEBdo4u4gpS7p3kkVPlt",
        payment_link_url="https://buy.stripe.com/4gMeVcfB82eC9Ke0ja8so0n",
        description="Premium retainer that follows the AI Ops Partner — Build engagement. Full upkeep + active management of the multi-workflow operations engine: monitoring, monthly ops report, automation tuning, and priority operator execution. Customers reach this tier through the Rescue → Buildout → Build funnel.",
    ),
    CatalogEntry(
        sku_id="retainer_seo_optimization",
        display_name="SEO Optimization",
        category=SkuCategory.RETAINER,
        fulfillment_module=FulfillmentModule.RETAINER,
        price_usd_cents=30000,
        recurring=True,
        stripe_product_id="prod_UJX2ZiARBH88n0",
        stripe_price_id="price_1TKu0aBdo4u4gpS7I6ZPKIy6",
        payment_link_url="https://buy.stripe.com/6oU5kCagO7yW9Keea08so0i",
        description="Ongoing technical SEO improvements, on-page optimization, internal linking, performance work, and monthly visibility reporting.",
    ),
    # AI Digital Receptionist — flat $99/mo base + $0.35/min metered overage.
    # The catalog maps a SKU to exactly ONE checkout/webhook price, so this
    # entry points at the flat BASE price only; the metered overage price
    # (price_<OVERAGE>) is deliberately NOT cataloged — a usage-billed
    # invoice line is not a purchase event and must not resolve to a
    # fulfillment module via lookup_by_stripe_price. The retainer registry
    # (backend/retainer/registry.py) stays the single source of truth for
    # the metered fields. Stripe IDs PENDING Phase 0 — see stripe_sync_report.md.
    # payment_link_url is None: the pilot has no self-serve checkout; the
    # operator creates the two-item subscription manually (hybrid onboarding).
    CatalogEntry(
        sku_id="retainer_ai_receptionist",
        display_name="AI Digital Receptionist",
        category=SkuCategory.RETAINER,
        fulfillment_module=FulfillmentModule.RETAINER,
        price_usd_cents=9900,
        recurring=True,
        stripe_product_id=None,
        stripe_price_id=None,
        payment_link_url=None,
        description="AI voice receptionist answering inbound calls 24/7: greeting, FAQs, appointment + callback capture, call transfer, and after-hours messages. Flat $99/mo base plus $0.35 per call-minute.",
    ),

    # --- Digital playbooks ---
    CatalogEntry(
        sku_id="playbook_lead_qual",
        display_name="Lead Qualification Workflow Playbook",
        category=SkuCategory.DIGITAL,
        fulfillment_module=FulfillmentModule.PRODUCTS,
        price_usd_cents=39000,
        recurring=False,
        stripe_product_id="prod_U913nsMud12tVp",
        stripe_price_id="price_1TAj1sBdo4u4gpS7FZtd5A6q",
        payment_link_url=None,
        description="Higher-quality pipeline conversations and less time wasted on low-intent prospects — a complete lead-qualification workflow you can run today.",
    ),
    CatalogEntry(
        sku_id="playbook_client_onboarding",
        display_name="Client Onboarding Workflow Playbook",
        category=SkuCategory.DIGITAL,
        fulfillment_module=FulfillmentModule.PRODUCTS,
        price_usd_cents=49000,
        recurring=False,
        stripe_product_id="prod_U913b1h7cGXVwH",
        stripe_price_id="price_1TAj1rBdo4u4gpS7zoeRhJEP",
        payment_link_url=None,
        description="A consistent onboarding flow that reduces delays, cuts confusion, and improves early retention from the first client touch.",
    ),
    CatalogEntry(
        sku_id="playbook_sales_followup",
        display_name="Sales Follow-Up Workflow Playbook",
        category=SkuCategory.DIGITAL,
        fulfillment_module=FulfillmentModule.PRODUCTS,
        price_usd_cents=39000,
        recurring=False,
        stripe_product_id="prod_U913ApbXvKqJ7d",
        stripe_price_id="price_1TAj1qBdo4u4gpS7PPBq3VAW",
        payment_link_url=None,
        description="Faster response times, cleaner follow-up, and more closed deals without chasing people manually.",
    ),

    # --- Digital packs ---
    CatalogEntry(
        sku_id="pack_creator_quickstart",
        display_name="Creator QuickStart Pack",
        category=SkuCategory.DIGITAL,
        fulfillment_module=FulfillmentModule.PRODUCTS,
        price_usd_cents=15000,
        recurring=False,
        stripe_product_id="prod_U914lCS5MSDPKN",
        stripe_price_id="price_1TAj1sBdo4u4gpS7JFRQtBuj",
        payment_link_url=None,
        description="A fast-start content system that reduces blank-page time and gives creators a usable publishing runway.",
    ),
    CatalogEntry(
        sku_id="pack_content_funnel",
        display_name="Full Content Funnel Kit",
        category=SkuCategory.DIGITAL,
        fulfillment_module=FulfillmentModule.PRODUCTS,
        price_usd_cents=30000,
        recurring=False,
        stripe_product_id="prod_U914ZfY9YY9ncl",
        stripe_price_id="price_1TAj1tBdo4u4gpS7oL1R8dzv",
        payment_link_url=None,
        description="A complete content funnel pack that helps turn attention into qualified conversations and next-step actions.",
    ),
    CatalogEntry(
        sku_id="pack_authority_accelerator",
        display_name="Authority Accelerator Pack",
        category=SkuCategory.DIGITAL,
        fulfillment_module=FulfillmentModule.PRODUCTS,
        price_usd_cents=50000,
        recurring=False,
        stripe_product_id="prod_U914wnm1xgBDqM",
        stripe_price_id="price_1TAj1uBdo4u4gpS7Ve32ttJR",
        payment_link_url=None,
        description="A premium authority asset pack that sharpens positioning and gives high-intent buyers stronger conversion language.",
    ),

    # --- Add-ons (created 2026-05-16) ---
    CatalogEntry(
        sku_id="addon_stripe_hardening",
        display_name="Stripe Webhook Hardening",
        category=SkuCategory.ADDON,
        fulfillment_module=FulfillmentModule.SERVICES,
        price_usd_cents=4999,
        recurring=False,
        stripe_product_id="prod_UWt5OLlYT4RJtM",
        stripe_price_id="price_1TXpJIBdo4u4gpS7hWXSn0EL",
        payment_link_url="https://buy.stripe.com/dRm9AS4Wu5qO4pU4zq8so0o",
        description="Production-grade webhook signature verification, idempotency keys, and replay protection wired into your existing Stripe integration.",
    ),
    CatalogEntry(
        sku_id="addon_email_deliverability",
        display_name="Email Deliverability Audit",
        category=SkuCategory.ADDON,
        fulfillment_module=FulfillmentModule.SERVICES,
        price_usd_cents=7999,
        recurring=False,
        stripe_product_id="prod_UWt5lw09y8X5Px",
        stripe_price_id="price_1TXpJLBdo4u4gpS7H8PJEYVF",
        payment_link_url="https://buy.stripe.com/bJe3cu74Cf1of4y7LC8so0p",
        description="Inbox-placement diagnostic covering SPF, DKIM, DMARC, sender reputation, and bounce/complaint patterns — with a prioritized fix list.",
    ),
    CatalogEntry(
        sku_id="addon_404_audit",
        display_name="Broken Link & 404 Audit",
        category=SkuCategory.ADDON,
        fulfillment_module=FulfillmentModule.SERVICES,
        price_usd_cents=2999,
        recurring=False,
        stripe_product_id="prod_UWt5uHnQTEr3RK",
        stripe_price_id="price_1TXpJUBdo4u4gpS7DPd7RKeg",
        payment_link_url="https://buy.stripe.com/5kQ3cu74C4mK9Ke4zq8so0q",
        description="Full-site crawl that identifies broken internal links, 404 pages, and redirect chains, delivered with a ranked fix list.",
    ),
    CatalogEntry(
        sku_id="addon_dns_health",
        display_name="DNS & Email Auth (SPF/DKIM/DMARC) Setup",
        category=SkuCategory.ADDON,
        fulfillment_module=FulfillmentModule.SERVICES,
        price_usd_cents=9999,
        recurring=False,
        stripe_product_id="prod_UWt5xWZqXGMGsY",
        stripe_price_id="price_1TXpJYBdo4u4gpS7IxXDPTKw",
        payment_link_url="https://buy.stripe.com/5kQcN4agOg5s5tY3vm8so0r",
        description="End-to-end DNS hardening for your sending domains: SPF, DKIM, DMARC, and MX records configured for maximum inbox placement and anti-spoofing protection.",
    ),

    # --- Add-ons (created 2026-05-16, second batch — fulfillment in backend.products) ---
    CatalogEntry(
        sku_id="addon_automation_health_check",
        display_name="Automation Health-Check",
        category=SkuCategory.ADDON,
        fulfillment_module=FulfillmentModule.PRODUCTS,
        price_usd_cents=4999,
        recurring=False,
        stripe_product_id="prod_UWtIaePPPccapg",
        stripe_price_id="price_1TXpW3Bdo4u4gpS77xKIfuUq",
        payment_link_url="https://buy.stripe.com/cNidR8coWdXkf4y9TK8so0s",
        description="5-axis inventory of your Zapier / Make / n8n scenarios with redundancy, failure-mode, and cost-per-run analysis, plus a 30-day triage plan.",
    ),
    CatalogEntry(
        sku_id="addon_crm_hygiene_sweep",
        display_name="CRM Hygiene Sweep",
        category=SkuCategory.ADDON,
        fulfillment_module=FulfillmentModule.PRODUCTS,
        price_usd_cents=4999,
        recurring=False,
        stripe_product_id="prod_UWtIp0wQgBIwsE",
        stripe_price_id="price_1TXpW7Bdo4u4gpS7q7RTrEXD",
        payment_link_url="https://buy.stripe.com/14A14m1Ki1ay6y28PG8so0t",
        description="Dedupe, stale-record, field-completeness, and ownership integrity scan of your CRM, delivered as per-axis CSVs with prioritized actions.",
    ),
    CatalogEntry(
        sku_id="addon_dashboard_setup",
        display_name="Operator Dashboard Setup",
        category=SkuCategory.ADDON,
        fulfillment_module=FulfillmentModule.PRODUCTS,
        price_usd_cents=9999,
        recurring=False,
        stripe_product_id="prod_UWtIK7z7Bbe9VR",
        stripe_price_id="price_1TXpWABdo4u4gpS71bxVuezC",
        payment_link_url="https://buy.stripe.com/6oU7sK60yf1o09Efe48so0u",
        description="Single-page operator KPI dashboard skeleton — 15 tiles in a 5x3 grid, source-to-spreadsheet wiring blueprints, weekly review ritual.",
    ),
]


# ---- Lookup helpers --------------------------------------------------------

_BY_SKU: dict[str, CatalogEntry] = {e.sku_id: e for e in CATALOG}
_BY_STRIPE_PRODUCT: dict[str, CatalogEntry] = {
    e.stripe_product_id: e for e in CATALOG if e.stripe_product_id
}
_BY_STRIPE_PRICE: dict[str, CatalogEntry] = {
    e.stripe_price_id: e for e in CATALOG if e.stripe_price_id
}


def sku(sku_id: str) -> CatalogEntry:
    """Look up a catalog entry by its canonical sku_id. Raises KeyError if missing."""
    try:
        return _BY_SKU[sku_id]
    except KeyError as exc:
        raise KeyError(f"Unknown sku_id: {sku_id!r}") from exc


def lookup_by_stripe_product(product_id: str) -> Optional[CatalogEntry]:
    """Return the catalog entry for a Stripe product_id, or None if not mapped."""
    return _BY_STRIPE_PRODUCT.get(product_id)


def lookup_by_stripe_price(price_id: str) -> Optional[CatalogEntry]:
    """Return the catalog entry for a Stripe price_id, or None if not mapped."""
    return _BY_STRIPE_PRICE.get(price_id)


def by_category(cat: SkuCategory) -> list[CatalogEntry]:
    """Return all catalog entries in the given category, in declaration order."""
    return [e for e in CATALOG if e.category == cat]


def all_sku_ids() -> list[str]:
    """Return every sku_id in declaration order."""
    return [e.sku_id for e in CATALOG]
