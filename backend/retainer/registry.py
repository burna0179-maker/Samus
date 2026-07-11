"""Retainer SKU registry — 4 recurring-revenue products.

Mirrors the ``ProductConfig`` pattern from
``Samus/recovery/callsheet_product_registry.py`` but specialized for
recurring-revenue plans: each entry carries a cadence + monthly cycle
identifier so the monthly-cycle runner can dispatch on ``sku_id`` alone.

The four retainers:
  * ``retainer_seo_optimization`` — $300/mo flat. Monthly fix cycle on top
    of the SEO Implementation work that customers complete first.
  * ``retainer_ai_ops_partner_entry`` — $2,000/mo flat. Self-serve tier
    matching the hustleforge.tech/ai-ops-partner/ public page; includes
    onboarding (audit + first-month roadmap) — no separate build SKU.
  * ``retainer_ai_ops_partner_premium`` — $5,000/mo flat. Customers reach
    this via the Rescue → Buildout → Build funnel. Upkeep of the operations
    engine built in the ``service_ai_ops_partner_build`` engagement.
  * ``retainer_ai_receptionist`` — $99/mo flat base + $0.35/call-minute
    metered overage. An always-on inbound voice receptionist; unlike the
    other three it has NO monthly deliverable DAG (``has_monthly_cycle``
    is False) — value is delivered continuously, billed by usage.

Both AI Ops tiers run the same ``ai_ops_partner_cycle`` DAG (Week 1 assess
→ Week 2 prioritize → Week 3 build → Week 4 report) — they differ in scope
ceiling per cycle and price.

``price_usd_cents_low`` and ``price_usd_cents_high`` are equal on every
flat-rate SKU; the range fields stay for API compatibility.

Stripe IDs (``stripe_product_id`` / ``stripe_price_id``) are populated
by the catalog workcell — see ``backend/catalog/registry.py``. The
retainer module itself reads ``sku_id`` only; Stripe IDs live here as
the documented single-source-of-truth join column.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


RetainerCadence = Literal["monthly"]


@dataclass(frozen=True)
class RetainerProductConfig:
    """One retainer SKU's configuration.

    Frozen so accidental in-place mutation in a request handler can't
    corrupt the registry for subsequent requests.
    """
    sku_id: str
    display_name: str
    cadence: RetainerCadence
    cycle_id: str                     # which monthly_cycle DAG to run
    description: str
    price_usd_cents_low: int
    price_usd_cents_high: int
    # Stripe handoff — populated by Stream 4 (catalog workcell). The
    # retainer module does NOT consume these; they exist so the registry
    # can be the single source of truth read by both workcells.
    stripe_product_id: str | None = None
    stripe_price_id: str | None = None
    # --- metered overage (AI Digital Receptionist + future usage SKUs) ---
    # A usage-billed SKU keeps its flat base (price_usd_cents_* +
    # stripe_price_id, semantics unchanged) and ADDS a Stripe metered
    # price. ``stripe_overage_price_id`` is that metered price id; the
    # per-unit overage RATE lives only in Stripe — never duplicated here,
    # so the registry/Stripe drift flagged in stripe_sync_report.md can't
    # recur. ``stripe_meter_event_name`` is the Stripe Meter event_name
    # usage events report against. ``included_units_per_cycle`` is the one
    # policy number a flat metered price can't express (0 = bill from the
    # first unit). ``overage_unit`` labels the unit, e.g. "minute". All
    # None/0/"" on every flat-rate SKU — non-metered SKUs are untouched.
    stripe_overage_price_id: str | None = None
    stripe_meter_event_name: str | None = None
    included_units_per_cycle: int = 0
    overage_unit: str = ""
    # False for always-on services (e.g. the receptionist) that have no
    # monthly deliverable DAG. The enroll path skips schedule_next_cycle
    # when this is False so it never queues a cycle with no plan builder.
    has_monthly_cycle: bool = True
    # Inputs the enroll path must collect for this SKU before scheduling
    # the first monthly cycle. Empty list -> email-only enrollment is
    # sufficient (e.g. AI Ops Partner gathers scope on the welcome call,
    # not via the buy page).
    required_intake_fields: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Canonical SKU table
# ---------------------------------------------------------------------------

RETAINER_SKUS: tuple[RetainerProductConfig, ...] = (
    RetainerProductConfig(
        sku_id="retainer_seo_optimization",
        display_name="SEO Optimization",
        cadence="monthly",
        cycle_id="seo_optimization_cycle",
        description=(
            "Ongoing technical SEO + on-page optimization. Each month we "
            "re-run the audit, diff against prior month, apply the "
            "highest-impact fixes (title/meta optimization, broken-link "
            "repair, schema markup, internal-linking, Core Web Vitals), "
            "and send a visibility report showing rank movement, GSC "
            "traffic delta, and what was changed."
        ),
        price_usd_cents_low=30000,    # $300/mo
        price_usd_cents_high=30000,
        required_intake_fields=("audit_url",),
        stripe_product_id="prod_UJX2ZiARBH88n0",
        stripe_price_id="price_1TKu0aBdo4u4gpS7I6ZPKIy6",
    ),
    RetainerProductConfig(
        sku_id="retainer_ai_ops_partner_entry",
        display_name="AI Ops Partner",
        cadence="monthly",
        cycle_id="ai_ops_partner_cycle",
        description=(
            "Entry retainer matching the public AI Ops Partner page. "
            "Includes onboarding (audit + first-month roadmap). Same "
            "four-week cycle as Premium (assess / prioritize / build / "
            "report) but sized for one focal workflow per cycle. "
            "Self-serve from hustleforge.tech/ai-ops-partner/. $2,000/mo."
        ),
        price_usd_cents_low=200000,   # $2,000/mo
        price_usd_cents_high=200000,
        required_intake_fields=("operations_summary",),
        stripe_product_id="prod_U913mXXZXG9REI",
        stripe_price_id="price_1TAj1pBdo4u4gpS7GZGee1zn",
    ),
    RetainerProductConfig(
        sku_id="retainer_ai_ops_partner_premium",
        display_name="AI Ops Partner — Premium",
        cadence="monthly",
        cycle_id="ai_ops_partner_cycle",
        description=(
            "Premium retainer following the AI Ops Partner — Build "
            "engagement. Four-week cycle: Week 1 assess + check-in, "
            "Week 2 prioritize, Week 3 build + deploy improvements, "
            "Week 4 deliver the monthly ops report. Multi-workflow "
            "scope ceiling per cycle. Flat $5,000/mo."
        ),
        price_usd_cents_low=500000,   # $5,000/mo
        price_usd_cents_high=500000,
        required_intake_fields=("operations_summary",),
        stripe_product_id="prod_UWt4i2QB7BZlFx",
        stripe_price_id="price_1TXpJEBdo4u4gpS7p3kkVPlt",
    ),
    RetainerProductConfig(
        sku_id="retainer_ai_receptionist",
        display_name="AI Digital Receptionist",
        cadence="monthly",
        # Never dispatched — has_monthly_cycle=False. Kept as a stable
        # identifier in case a usage-summary cycle is added later.
        cycle_id="ai_receptionist_cycle",
        description=(
            "AI voice receptionist that answers a business's inbound calls "
            "24/7: greets callers, handles FAQs, captures appointment + "
            "callback requests, transfers urgent calls, and takes after-"
            "hours messages. Flat $99/mo base plus $0.35 per call-minute "
            "billed through a Stripe metered price. Operator onboards each "
            "client (greeting, business hours, routing numbers) — hybrid "
            "delivery, no monthly deliverable cycle."
        ),
        price_usd_cents_low=9900,     # $99/mo flat base
        price_usd_cents_high=9900,
        required_intake_fields=(
            "business_name",
            "forwarding_phone",
            "business_hours",
            "greeting_script",
        ),
        # Stripe IDs PENDING Phase 0 — the operator creates the product,
        # flat base price, Meter, metered price + setup-fee price on
        # acct_1SQRZABdo4u4gpS7, then populates the product/price/overage
        # ids here. ``stripe_meter_event_name`` is operator-chosen (not
        # Stripe-generated) so it is pinned now and the Meter created to
        # match. See backend/catalog/stripe_sync_report.md.
        stripe_product_id=None,
        stripe_price_id=None,
        stripe_overage_price_id=None,
        stripe_meter_event_name="receptionist_call_minutes",
        included_units_per_cycle=0,   # pilot: bill from the first minute
        overage_unit="minute",
        has_monthly_cycle=False,      # always-on service — no monthly DAG
    ),
)


# Convenience lookup map. Built at import time so callers can't accidentally
# desync from RETAINER_SKUS via an outdated dict reference.
_SKU_BY_ID: dict[str, RetainerProductConfig] = {s.sku_id: s for s in RETAINER_SKUS}


def get_retainer_sku(sku_id: str) -> RetainerProductConfig | None:
    """Return the registry entry for ``sku_id`` or None if unknown.

    Callers MUST treat ``None`` as a hard reject (unknown SKU). The enroll
    path raises ``UnknownRetainerSkuError`` on ``None``; the monthly-cycle
    runner does the same so a malformed cron entry can't silently no-op.
    """
    return _SKU_BY_ID.get(sku_id)


def list_retainer_sku_ids() -> list[str]:
    """All registered retainer SKU IDs (stable order matching RETAINER_SKUS)."""
    return [s.sku_id for s in RETAINER_SKUS]


__all__ = [
    "RetainerCadence",
    "RetainerProductConfig",
    "RETAINER_SKUS",
    "get_retainer_sku",
    "list_retainer_sku_ids",
]
