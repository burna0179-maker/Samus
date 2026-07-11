"""Pydantic models for the intake workcell.

Field names + value enums mirror the HTML form on
``https://hustleforge.tech/onboarding/`` exactly. Any drift will cause the
form's POST to 422 — keep this file in sync with the page when the form
template changes.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# Mirrors the 9 checkbox values rendered on the form. ``not_sure`` is the
# operator-friendly "didn't pick" — kept as a real category, not filtered out.
# Must stay 1:1 with ``form_schema._SERVICE_INTEREST`` (the test suite checks).
ServiceInterest = Literal[
    "seo_audit",
    "seo_implementation",
    "seo_optimization",
    "workflow_rescue",
    "workflow_buildout",
    "ai_ops_partner",
    "ai_receptionist",
    "playbook",
    "not_sure",
]

# Mirrors the budget <select> options. Empty-string allowed because the form
# does not require the operator to pick (the placeholder option has value="").
MonthlyBudget = Literal[
    "",
    "under_$150",
    "$150-$500",
    "$500-$2000",
    "$2000-$5000",
    "$5000+",
]

# Mirrors the timeline <select> options. Empty-string allowed for same reason.
Timeline = Literal[
    "",
    "asap",
    "this_month",
    "next_30_days",
    "next_90_days",
    "exploring",
]

# Mirrors the social-cadence <select> options in the optional "Social presence"
# fieldset. Empty-string is the "operator did not pick" placeholder — the
# campaign template's own default is used when this is blank.
# Must stay 1:1 with ``form_schema._SOCIAL_CADENCE``.
SocialCadencePref = Literal[
    "",
    "light",
    "moderate",
    "aggressive",
]


class OnboardingLeadRequest(BaseModel):
    """Body of ``POST /intake/onboarding``. Names match the HTML form 1:1."""
    # extra='forbid' so the form can't smuggle in unexpected fields and have
    # them silently persisted.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=256)
    # Email kept as plain str + minimal '@' check so we don't drag the
    # `email-validator` package into the runtime image just for a public
    # form. The HTML form's type="email" already filters the obvious typos
    # client-side; this server-side check is the second layer.
    email: str = Field(min_length=3, max_length=256)
    company: str = Field(default="", max_length=256)
    # The form sets ``maxlength="2048"`` on the URL input; pydantic-validated
    # but kept as str (not HttpUrl) so we don't reject leading-space / missing-
    # scheme inputs from operators who type "yoursite.com" instead of full URL.
    website_url: str = Field(default="", max_length=2048)
    service_interest: list[ServiceInterest] = Field(default_factory=list)
    pain_points: str = Field(min_length=1, max_length=2048)
    monthly_budget: MonthlyBudget = ""
    timeline: Timeline = ""
    # --- Social presence (optional) ---------------------------------------
    # Consumed by the campaign engine's client-yaml bootstrap when a lead
    # promotes to a paying client (see backend.campaigns.bootstrap_from_lead).
    # All optional; empty string is the "operator skipped" default. Old rows
    # written before v3 keep parsing because every field has a default.
    social_facebook: str = Field(default="", max_length=256)
    social_instagram: str = Field(default="", max_length=256)
    social_linkedin: str = Field(default="", max_length=256)
    brand_voice_notes: str = Field(default="", max_length=1024)
    social_cadence_pref: SocialCadencePref = ""

    @field_validator("email")
    @classmethod
    def _email_has_at(cls, v: str) -> str:
        v = (v or "").strip()
        if "@" not in v or v.startswith("@") or v.endswith("@"):
            raise ValueError("email must contain '@'")
        local, _, domain = v.rpartition("@")
        if not local or "." not in domain:
            raise ValueError("email must have a local part and a dotted domain")
        return v


class OnboardingLeadResult(BaseModel):
    """Response from ``POST /intake/onboarding``.

    ``status`` distinguishes the four useful outcomes:
      - ``queued``      — new lead persisted to DDB
      - ``duplicate``   — same lead inside dedup window; no second write
      - ``degraded``    — DDB write failed (network / IAM); audit ledger
                          still has the record so the lead isn't lost
      - ``rejected``    — validation failed (should be a 422 instead but
                          kept here for parity with the worker contract)
    """
    model_config = ConfigDict(extra="forbid")

    status: Literal["queued", "duplicate", "degraded", "rejected"]
    lead_id: str
    ts: str
    persisted: bool = False
    audit_appended: bool = False
    error: str | None = None


class StoredLead(BaseModel):
    """Shape of the row written to ``samus_onboarding_leads``.

    Kept separate from ``OnboardingLeadRequest`` so server-side fields
    (``lead_id``, ``created_at``, ``source_ip``, ``user_agent``, dedup key)
    have a single canonical declaration.
    """
    model_config = ConfigDict(extra="ignore")

    lead_id: str
    created_at: str  # ISO-8601 Z
    name: str
    email: str
    company: str = ""
    website_url: str = ""
    service_interest: list[str] = Field(default_factory=list)
    pain_points: str
    monthly_budget: str = ""
    timeline: str = ""
    source_ip: str = ""
    user_agent: str = ""
    dedup_key: str  # sha256(email | website_url | pain[:200])


class LeadListResult(BaseModel):
    """Response from ``GET /intake/leads``. Operator-only view."""
    model_config = ConfigDict(extra="forbid")

    leads: list[StoredLead] = Field(default_factory=list)
    count: int = 0
    scan_truncated: bool = False
    ddb_error: str | None = None


# ---------------------------------------------------------------------------
# Site telemetry (page views, form views, CTA clicks) — the input side of
# closing the "Samus is blind to website interactions" gap flagged during
# the funnel audit. Kept narrow on purpose: this is not a general analytics
# pipe, only the funnel-relevant events the operator needs to see abandoned
# checkouts, dead form pages, and cross-page bounce.
# ---------------------------------------------------------------------------

# Whitelist. Bounded so a compromised site can't pollute the ledger with
# arbitrary strings — every value here is one we've decided is worth counting.
SiteEventType = Literal[
    "page_view",       # generic — path is what matters
    "form_view",       # the onboarding form specifically rendered
    "form_submit_view",  # user reached the success/thanks page
    "pricing_view",    # pricing tier section rendered
    "buy_click",       # a Stripe buy-button (or pricing CTA) was clicked
]


class TelemetryEventRequest(BaseModel):
    """Public POST body for ``/intake/telemetry``.

    ``session_id`` is an opaque token minted by the site (a random hex string
    kept in sessionStorage). We do NOT correlate it back to a lead here — this
    is anonymous funnel telemetry, and coupling it to email would need explicit
    consent handling. Its only job here is dedup across a single visit's
    duplicate beacons (page_view fires on navigate + on back button).
    """
    model_config = ConfigDict(extra="forbid")

    event: SiteEventType
    path: str = Field(default="", max_length=512)
    referrer: str = Field(default="", max_length=1024)
    session_id: str = Field(default="", max_length=128)
    # Site-side ts is advisory (client clock skew is real); the server also
    # stamps received_at at ingest so the ledger has an authoritative order.
    ts: str = Field(default="", max_length=64)
    # Optional. When present + event=="buy_click" this attributes the click
    # to a specific SKU without requiring us to parse the buy URL server-side.
    sku_id: str = Field(default="", max_length=64)


class TelemetryEventResult(BaseModel):
    """Response from ``POST /intake/telemetry``. Kept minimal: telemetry is
    fire-and-forget from the site's point of view, and a chatty response
    body wastes bandwidth on every page view."""
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted", "dropped_disabled"]
    ts: str


class StoredTelemetryEvent(BaseModel):
    """Row shape appended to the telemetry ledger."""
    model_config = ConfigDict(extra="ignore")

    event: str
    path: str = ""
    referrer: str = ""
    session_id: str = ""
    client_ts: str = ""
    received_at: str  # server-authoritative
    source_ip: str = ""
    user_agent: str = ""
    sku_id: str = ""