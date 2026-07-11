"""I/O contracts for the website-build capability.

``WebsiteBrief`` is the operator-authored (or CRM-derived) description of what
to build: the business identity, the pages, the content. It is the website
analogue of the cash engine's Stake Sentence — the human-supplied truth the
sequence executes against. In a *supervised* walk-through the operator fills
this in; in *autonomous* mode it is assembled from the won deal's CRM record +
a structured intake questionnaire (the attachment point is documented on
``from_intake``).

``WebsiteOrder`` binds a brief to its commercial context — who it is for, how
it settles (a paid invoice, or a barter against a logged liability) — so the
final ``settle`` stage can record the right financial event.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# How the order settles once the site is delivered.
#   invoice  — a normal paid build (record/charge via finance/Stripe later)
#   barter   — work-for-debt: delivery records a repayment-in-kind against a
#              lender_id in the liabilities ledger (e.g. the Harmony deal)
#   gratis   — no financial event (portfolio piece, favour)
SettlementKind = Literal["invoice", "barter", "gratis"]


class WebsitePage(BaseModel):
    """One page the site should contain."""

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1)        # url-ish key: "home", "about", "services"
    title: str = Field(min_length=1)
    # Free-form content the stage maps into the template (copy, section text).
    # Kept as a dict so it can carry headings/body/cta without a rigid schema
    # the chosen template would only override.
    content: dict[str, str] = Field(default_factory=dict)


class WebsiteBrief(BaseModel):
    """What to build — the human-supplied (or intake-derived) build spec."""

    model_config = ConfigDict(extra="forbid")

    business_name: str = Field(min_length=1)
    # What the business does — feeds copy + SEO description.
    business_description: str = ""
    industry: str = ""
    # Contact + business-info (set via Site Properties at the business_info stage)
    contact_email: str = ""
    contact_phone: str = ""
    address: str = ""
    # Branding hints the operator/template applies (not auto-designed).
    brand_colors: list[str] = Field(default_factory=list)
    logo_url: str = ""
    # The pages to populate. Empty -> the brief stage parks for the content it
    # needs rather than publishing an empty shell.
    pages: list[WebsitePage] = Field(default_factory=list)
    # The Wix template to base the site on (operator-chosen during provision).
    # Empty is allowed: provision can adopt an operator-created site instead.
    template_id: str = ""
    # An already-provisioned Wix site to populate instead of creating one. When
    # set, the provision stage adopts it rather than calling create_site.
    existing_site_id: str = ""
    # The public, customer-facing URL of the published site (e.g.
    # https://acme.wixstudio.com/site). Used by the deliver-stage live SEO +
    # security gate; absent, the live gate parks (fail-closed) under enforce.
    public_url: str = ""
    # Where the client's company is registered — drives which website-law
    # requirements apply (jurisdiction-aware legal pages). Country is ISO-ish
    # ("US", "GB", "DE", ...); state is the US 2-letter code when country=US.
    # Empty -> derived from ``address`` (country US, state from the address).
    registered_country: str = ""
    registered_state: str = ""
    # Where the "Get a Free Quote" form POSTs (Samus intake workcell onboarding
    # endpoint). Empty -> the form degrades to a mailto: to contact_email so it
    # always works. CORS on the intake side must allow the deployed origin.
    intake_endpoint: str = ""
    # Owner's public Google Calendar appointment-booking URL. When set, a
    # "Schedule Now" button links to it. Empty -> no scheduling button.
    scheduling_url: str = ""
    # Local service-area cities for per-city local-SEO pages. Empty -> one page
    # for the business's own city (derived from address). NEVER invent coverage.
    service_areas: list[str] = Field(default_factory=list)
    # Public "leave a review" URL (e.g. Google review link). Drives the Reviews
    # page CTA; empty -> a generic "work with us" CTA (never fabricated reviews).
    review_url: str = ""
    # Owner's Google Form (job-application) URL. When set, the Careers page
    # embeds it so candidates apply digitally (responses land in the owner's
    # Google Sheet + email). Accepts the form's share link or the embed src
    # (docs.google.com/forms/.../viewform); ?embedded=true is added if missing.
    # Empty -> Careers degrades to a mailto: "Apply Now" so it always works.
    careers_form_url: str = ""


class WebsiteOrder(BaseModel):
    """A website build request + its commercial settlement context."""

    model_config = ConfigDict(extra="forbid")

    # Stable id; server-minted (``wb-...``) when not supplied.
    order_id: str = ""
    customer_name: str = Field(min_length=1)
    brief: WebsiteBrief

    settlement_kind: SettlementKind = "invoice"
    # For settlement_kind == "barter": the lender_id in liabilities.yaml the
    # delivered site repays in kind, and the agreed dollar value applied.
    settlement_lender_id: str = ""
    settlement_amount_usd: float = 0.0

    # Optional linkage back to a won Cash Engine deal, so a future autonomous
    # run can trace the order to its opportunity (and the brief can be derived
    # from that deal's CRM record).
    opportunity_id: str = ""
    prospect_id: str = ""

    # Audit colour: where the order came from (operator | cash_engine | api).
    source: str = "operator"


__all__ = [
    "SettlementKind",
    "WebsitePage",
    "WebsiteBrief",
    "WebsiteOrder",
]
