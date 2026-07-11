"""Canonical form-schema metadata for the hustleforge.tech onboarding page.

Background
----------
Until this module landed, the public onboarding form's labels, placeholders,
checkbox options, budget tiers, and timeline values were defined in TWO
places that had to stay manually in sync:

* The static HTML on ``https://hustleforge.tech/onboarding/`` — what the
  user sees.
* The Pydantic models in :mod:`backend.intake.models` — what
  ``POST /intake/onboarding`` accepts. The module's docstring is explicit:
  "Field names + value enums mirror the HTML form ... Any drift will cause
  the form's POST to 422 — keep this file in sync with the page when the
  form template changes."

That "keep in sync" coupling is enforced by code review only. This module
makes the backend the canonical source: the static site can fetch
``GET /intake/form-schema`` at build/deploy time (or runtime) and render
from a single definition. A field rename in :mod:`models` lands here in
the same commit, so the site picks it up on its next deploy instead of
silently 422-ing.

Scope
-----
Form metadata only. Out of scope here:

* The Stripe publishable key — operational config, not source code.
* The product catalog (``backend.catalog.registry``) — already canonical
  with 19 SKUs; the legacy ``recovery/onboarding_form_schema.STRIPE_PRODUCTS``
  is superseded.
* HTML rendering — the static site owns that; this module returns data.

The field ``name`` / ``kind`` / ``options`` tuples mirror the active
:class:`backend.intake.models.OnboardingLeadRequest` field names + Literal
enum values exactly. Adding a field there REQUIRES updating this module —
the test suite asserts the schema covers every model field.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

# Bump when the field set, names, or enum value strings change in a way
# that would break a site rendering against an older copy of the schema.
# Adding optional metadata (e.g. a new placeholder, a new docstring) does
# NOT require a bump; renaming / removing / changing accepted values does.
# v2: added the ``ai_receptionist`` service-interest option (AI Digital
# Receptionist) — a new accepted enum value, so the version bumps.
# v3: added the optional Social presence fieldset (social_facebook,
# social_instagram, social_linkedin, brand_voice_notes,
# social_cadence_pref) so paying-client promotion can auto-populate
# ``clients/<slug>/campaign.yaml`` with the operator's real handles
# instead of manual data entry. Introduces the ``fieldset`` field
# grouping concept and a top-level ``fieldsets`` list.
SCHEMA_VERSION = "v3"


class FormFieldSchema(BaseModel):
    """One form-field definition.

    ``name`` MUST match the corresponding attribute on
    :class:`backend.intake.models.OnboardingLeadRequest`. ``options`` is
    the list of accepted submit values (Literal members for the
    select/checkbox fields), NOT human-readable labels; ``option_labels``
    parallels it for rendering. The site is expected to render each
    ``options[i]`` as ``option_labels[i]``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    label: str
    kind: str  # text | email | url | textarea | checkbox | select
    placeholder: Optional[str] = None
    required: bool = False
    max_length: Optional[int] = None
    options: List[str] = Field(default_factory=list)
    option_labels: List[str] = Field(default_factory=list)
    help_text: Optional[str] = None
    # Optional grouping. ``None`` renders inline with the primary flow;
    # a non-empty value MUST match an entry in FormSchemaResponse.fieldsets
    # so the site can render a labelled ``<fieldset>`` around the group.
    fieldset: Optional[str] = None


class FieldsetSchema(BaseModel):
    """One field-grouping definition.

    Fields carrying a matching ``fieldset`` value are rendered inside a
    ``<fieldset>`` with this ``label`` as the ``<legend>``. Ordering of
    fieldsets follows the order they appear in FormSchemaResponse.fieldsets
    so the site is not forced to sort.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    label: str
    help_text: Optional[str] = None


class SubmitConfig(BaseModel):
    """Static copy + POST target used by the form's submit handler."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    button_text: str
    success_message: str
    trust_note: str
    post_url: str


class FormSchemaResponse(BaseModel):
    """Response shape for ``GET /intake/form-schema``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    fields: List[FormFieldSchema]
    submit: SubmitConfig
    # Optional field groupings. Every field.fieldset MUST reference an id
    # here. Old sites that ignore this list still work — they'll render
    # every field flat, which is degraded but not broken.
    fieldsets: List[FieldsetSchema] = Field(default_factory=list)


# ---- The canonical schema --------------------------------------------------

# Service-interest checkbox: submit values (column 1) and labels (column 2)
# are kept paired so a rename on either side requires touching both, which
# the test suite verifies.
_SERVICE_INTEREST: tuple[tuple[str, str], ...] = (
    ("seo_audit", "SEO Audit & Fix"),
    ("seo_implementation", "SEO Implementation"),
    ("seo_optimization", "Monthly SEO Optimization"),
    ("workflow_rescue", "48-Hour Workflow Rescue"),
    ("workflow_buildout", "Workflow System Buildout"),
    ("ai_ops_partner", "AI Ops Partner Program"),
    ("ai_receptionist", "AI Digital Receptionist"),
    ("playbook", "Workflow Playbook"),
    ("not_sure", "Not sure yet"),
)

_BUDGET: tuple[tuple[str, str], ...] = (
    ("", "Select a range"),
    ("under_$150", "Under $150"),
    ("$150-$500", "$150 – $500"),
    ("$500-$2000", "$500 – $2,000"),
    ("$2000-$5000", "$2,000 – $5,000"),
    ("$5000+", "$5,000+"),
)

_TIMELINE: tuple[tuple[str, str], ...] = (
    ("", "Select a timeline"),
    ("asap", "As soon as possible"),
    ("this_month", "This month"),
    ("next_30_days", "Next 30 days"),
    ("next_90_days", "Next 90 days"),
    ("exploring", "Just exploring"),
)

# Social cadence hint — informs the campaign engine's default
# ``social_posting_cadence`` when a lead promotes to a paying client.
# ``""`` is the "operator did not pick" placeholder; the campaign template's
# own default is used when this is empty.
_SOCIAL_CADENCE: tuple[tuple[str, str], ...] = (
    ("", "Use campaign default"),
    ("light", "Light — 1×/week"),
    ("moderate", "Moderate — 3×/week"),
    ("aggressive", "Aggressive — daily"),
)

# Fieldset ids. Kept as module-level constants so the model-side wiring
# and the render-side wiring reference the same string.
FS_SOCIAL_PRESENCE = "social_presence"


def _split_pairs(
    pairs: tuple[tuple[str, str], ...],
) -> tuple[List[str], List[str]]:
    values = [v for v, _ in pairs]
    labels = [l for _, l in pairs]
    return values, labels


def _build_fields() -> List[FormFieldSchema]:
    service_values, service_labels = _split_pairs(_SERVICE_INTEREST)
    budget_values, budget_labels = _split_pairs(_BUDGET)
    timeline_values, timeline_labels = _split_pairs(_TIMELINE)
    cadence_values, cadence_labels = _split_pairs(_SOCIAL_CADENCE)
    return [
        FormFieldSchema(
            name="name",
            label="Full Name",
            kind="text",
            placeholder="Jane Smith",
            required=True,
            max_length=256,
        ),
        FormFieldSchema(
            name="email",
            label="Email Address",
            kind="email",
            placeholder="jane@yourcompany.com",
            required=True,
            max_length=256,
        ),
        FormFieldSchema(
            name="company",
            label="Company / Brand",
            kind="text",
            placeholder="Acme Inc.",
            required=False,
            max_length=256,
        ),
        FormFieldSchema(
            name="website_url",
            label="Website URL",
            kind="url",
            placeholder="https://yoursite.com",
            required=False,
            max_length=2048,
        ),
        FormFieldSchema(
            name="service_interest",
            label="What do you need help with?",
            kind="checkbox",
            required=False,
            options=service_values,
            option_labels=service_labels,
        ),
        FormFieldSchema(
            name="pain_points",
            label="Describe your biggest bottleneck",
            kind="textarea",
            placeholder=(
                "What's slowing you down the most right now? What's "
                "breaking, taking too long, or costing you the most time?"
            ),
            required=True,
            max_length=2048,
        ),
        FormFieldSchema(
            name="monthly_budget",
            label="Monthly budget range",
            kind="select",
            required=False,
            options=budget_values,
            option_labels=budget_labels,
        ),
        FormFieldSchema(
            name="timeline",
            label="Timeline",
            kind="select",
            required=False,
            options=timeline_values,
            option_labels=timeline_labels,
        ),
        # --- Social presence (optional) -----------------------------------
        # Consumed by the campaign engine's client-yaml bootstrap when a
        # lead promotes to a paying client — pre-populates their real
        # handles so ``clients/<slug>/campaign.yaml`` doesn't need manual
        # data entry. Every field is optional so this fieldset never blocks
        # a lead who doesn't want to share.
        FormFieldSchema(
            name="social_facebook",
            label="Facebook page URL or handle",
            kind="text",
            placeholder="facebook.com/yourbrand or @yourbrand",
            required=False,
            max_length=256,
            fieldset=FS_SOCIAL_PRESENCE,
        ),
        FormFieldSchema(
            name="social_instagram",
            label="Instagram handle",
            kind="text",
            placeholder="@yourbrand",
            required=False,
            max_length=256,
            fieldset=FS_SOCIAL_PRESENCE,
        ),
        FormFieldSchema(
            name="social_linkedin",
            label="LinkedIn company URL",
            kind="text",
            placeholder="linkedin.com/company/yourbrand",
            required=False,
            max_length=256,
            fieldset=FS_SOCIAL_PRESENCE,
        ),
        FormFieldSchema(
            name="brand_voice_notes",
            label="Brand voice / do-not-say notes",
            kind="textarea",
            placeholder=(
                "Anything the campaign should reflect or avoid — tone, "
                "words we don't use, off-limits topics, favorite phrases."
            ),
            required=False,
            max_length=1024,
            fieldset=FS_SOCIAL_PRESENCE,
        ),
        FormFieldSchema(
            name="social_cadence_pref",
            label="Preferred social posting cadence",
            kind="select",
            required=False,
            options=cadence_values,
            option_labels=cadence_labels,
            fieldset=FS_SOCIAL_PRESENCE,
        ),
    ]


def _build_fieldsets() -> List[FieldsetSchema]:
    return [
        FieldsetSchema(
            id=FS_SOCIAL_PRESENCE,
            label="Social presence (optional)",
            help_text=(
                "Share what you already run and we'll pre-populate your "
                "campaign plan. Nothing here is required."
            ),
        ),
    ]


# Frozen at import time — the schema is immutable per process. Hot-reload
# requires a worker restart, which matches how every other Samus config
# constant behaves.
_FIELDS: tuple[FormFieldSchema, ...] = tuple(_build_fields())
_FIELDSETS: tuple[FieldsetSchema, ...] = tuple(_build_fieldsets())

_SUBMIT = SubmitConfig(
    button_text="Submit & Start Onboarding",
    success_message=(
        "You're in the queue. We've received your information and will "
        "review it within one business day with a clear action path."
    ),
    trust_note=(
        "Response within 24 hours. Priority is given to active purchases "
        "and urgent workflow bottlenecks."
    ),
    post_url="/intake/onboarding",
)


def get_form_schema() -> FormSchemaResponse:
    """Return the canonical form schema. Cheap; safe to call per request."""
    return FormSchemaResponse(
        schema_version=SCHEMA_VERSION,
        fields=list(_FIELDS),
        submit=_SUBMIT,
        fieldsets=list(_FIELDSETS),
    )


__all__ = [
    "FS_SOCIAL_PRESENCE",
    "FieldsetSchema",
    "FormFieldSchema",
    "FormSchemaResponse",
    "SCHEMA_VERSION",
    "SubmitConfig",
    "get_form_schema",
]
