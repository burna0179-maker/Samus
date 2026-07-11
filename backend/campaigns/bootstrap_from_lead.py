"""Bootstrap ``clients/<slug>/campaign.yaml`` from an intake lead.

When a lead promotes to a paying client, the campaign engine needs a
``clients/<slug>/campaign.yaml`` binding a template to that client's real
inputs (handles, cadence, brand voice). Historically an operator hand-wrote
this file per client — see ``clients/conquerors_christian_school/campaign.yaml``
for the shape.

The Social presence fieldset on the onboarding form (see
:mod:`backend.intake.form_schema` v3+) captures the same information at
intake time, so this module closes the loop: given a StoredLead + a
template_id + a client identity, it emits the client-instance YAML with
the operator's real handles pre-populated.

Design notes:

* Purely deterministic + fail-loud. If the caller passes a malformed lead
  or template_id, the write does NOT happen — an exception surfaces to
  the promoter (typically the operator dashboard or a Stripe-charge hook)
  where it can be surfaced to the operator instead of silently emitting
  a broken YAML.
* Does NOT overwrite an existing ``campaign.yaml`` unless ``overwrite=True``
  is passed explicitly. Operators may have hand-tuned a file post-signing;
  the bootstrap is a first-run helper, not an authority.
* Emits a canonical field ordering so a git diff on the file reflects real
  operator changes, not YAML-dump reordering.
* Only writes to the on-disk ``clients/`` root that ``contract_wire.py`` +
  ``client_directory.py`` already read from — no divergence.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from backend.intake.models import StoredLead

_LOG = logging.getLogger("samus.campaigns.bootstrap_from_lead")

# Same anchor other modules use — see contract_wire._CLIENTS_ROOT and
# client_directory._CLIENTS_ROOT. Keep them in lockstep.
_CLIENTS_ROOT = Path(__file__).resolve().parents[2] / "clients"


# Map the intake form's social_cadence_pref enum onto the string form the
# ``school_enrollment_campaign`` (and its siblings) expect for
# ``$inputs.social_posting_cadence``. Empty string ("" = operator did not
# pick) intentionally maps to nothing — the caller omits the input entirely
# so the template's own default (e.g. "3/week") wins.
_CADENCE_PREF_TO_YAML: dict[str, str] = {
    "light": "1/week",
    "moderate": "3/week",
    "aggressive": "1/day",
}


class BootstrapError(ValueError):
    """Raised when the lead → campaign.yaml conversion cannot proceed."""


def _norm_handle(raw: str) -> str:
    """Trim + strip trailing slash. Preserve case for URL handles."""
    return (raw or "").strip().rstrip("/")


def _collect_social_channels(lead: StoredLead) -> tuple[list[str], dict[str, str]]:
    """Return (channel_names, {channel: handle}) for present-only fields.

    The template's ``$inputs.social_channels`` is a list of channel *names*
    the publisher can iterate over; ``$inputs.social_handles`` is the
    per-channel handle. We deliberately keep them as two structures so a
    template can consume just the name list without needing to know the
    handle format.

    A field with only whitespace is treated as absent — the operator left
    it blank on the form, so the campaign shouldn't try to publish there.
    """
    handles: dict[str, str] = {}
    channels: list[str] = []
    for channel, raw in (
        ("facebook", lead.social_facebook),
        ("instagram", lead.social_instagram),
        ("linkedin", lead.social_linkedin),
    ):
        h = _norm_handle(raw)
        if h:
            channels.append(channel)
            handles[channel] = h
    return channels, handles


def build_instance_dict(
    lead: StoredLead,
    *,
    template_id: str,
    client_id: str,
    campaign_id: str,
    docuseal_slug: str = "",
    extra_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ``campaign_instance:`` payload for a client YAML.

    Not a Pydantic instance — the return value is the raw dict that gets
    yaml-dumped. This keeps the shape aligned with the hand-written YAMLs
    other operators have already produced (see
    ``clients/conquerors_christian_school/campaign.yaml``) and lets us dump
    with a stable ordering.

    ``extra_inputs`` merges LAST so a caller (typically the operator
    dashboard capturing template-specific fields at promotion time) can
    layer required fields like ``school_name`` on top of the intake-lead-
    derived defaults.
    """
    if not template_id or not template_id.strip():
        raise BootstrapError("template_id is required")
    if not client_id or not client_id.strip():
        raise BootstrapError("client_id is required")
    if not campaign_id or not campaign_id.strip():
        raise BootstrapError("campaign_id is required")

    channels, handles = _collect_social_channels(lead)

    inputs: dict[str, Any] = {}
    # Contact + identity fields the template will typically reference.
    inputs["approval_contact"] = lead.email
    inputs["authorized_signatory"] = lead.name
    if lead.company.strip():
        inputs["client_display_name"] = lead.company.strip()
    if lead.website_url.strip():
        inputs["website_url"] = lead.website_url.strip()

    if channels:
        inputs["social_channels"] = channels
        inputs["social_handles"] = handles
    if lead.social_cadence_pref:
        mapped = _CADENCE_PREF_TO_YAML.get(lead.social_cadence_pref)
        if mapped:
            inputs["social_posting_cadence"] = mapped
    if lead.brand_voice_notes.strip():
        # Kept as ``brand_voice_notes`` (not ``brand_voice``) so it does
        # not stomp a vertical's canonical tone string — templates that
        # want the operator's raw notes read this field explicitly.
        inputs["brand_voice_notes"] = lead.brand_voice_notes.strip()

    if extra_inputs:
        inputs.update(extra_inputs)

    instance: dict[str, Any] = {
        "campaign_id": campaign_id.strip(),
        "client_id": client_id.strip(),
        "template_id": template_id.strip(),
    }
    if docuseal_slug.strip():
        instance["docuseal_slug"] = docuseal_slug.strip()
    instance["inputs"] = inputs
    return {"campaign_instance": instance}


def target_yaml_path(client_id: str) -> Path:
    """Return the on-disk path a bootstrap would write for ``client_id``.

    Kept public so callers can check-before-write without duplicating the
    path-computation logic. ``client_id`` is normalized (stripped, folded
    to lowercase, spaces → underscores) so an operator entering
    ``"Acme Widgets"`` at intake and the persisted client_id land at the
    same folder.
    """
    slug = client_id.strip().lower().replace(" ", "_")
    if not slug:
        raise BootstrapError("client_id normalizes to empty slug")
    return _CLIENTS_ROOT / slug / "campaign.yaml"


def write_campaign_yaml(
    lead: StoredLead,
    *,
    template_id: str,
    client_id: str,
    campaign_id: str,
    docuseal_slug: str = "",
    extra_inputs: dict[str, Any] | None = None,
    overwrite: bool = False,
    clients_root: Path | None = None,
) -> Path:
    """Bootstrap ``clients/<slug>/campaign.yaml`` from a persisted lead.

    Returns the path written. Raises :class:`BootstrapError` if the target
    already exists and ``overwrite`` is False, or if the caller supplied
    an invalid identity trio.

    ``clients_root`` overrides the default on-disk root — kept for tests
    so they can point at a ``tmp_path`` instead of the real repo layout.
    """
    payload = build_instance_dict(
        lead,
        template_id=template_id,
        client_id=client_id,
        campaign_id=campaign_id,
        docuseal_slug=docuseal_slug,
        extra_inputs=extra_inputs,
    )

    root = clients_root if clients_root is not None else _CLIENTS_ROOT
    slug = client_id.strip().lower().replace(" ", "_")
    if not slug:
        raise BootstrapError("client_id normalizes to empty slug")
    target = root / slug / "campaign.yaml"

    if target.exists() and not overwrite:
        raise BootstrapError(
            f"campaign.yaml already exists at {target}; pass overwrite=True "
            f"to replace (existing operator-tuned files should NOT be silently "
            f"clobbered)"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    # sort_keys=False so our deliberate ordering (campaign_id / client_id /
    # template_id first, then inputs) survives the dump. default_flow_style
    # False forces block style so diffs are line-oriented.
    text = yaml.safe_dump(
        payload,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    target.write_text(text, encoding="utf-8")
    _LOG.info(
        "bootstrap_from_lead: wrote %s (channels=%s cadence=%s)",
        target,
        payload["campaign_instance"]["inputs"].get("social_channels") or [],
        payload["campaign_instance"]["inputs"].get("social_posting_cadence") or "template-default",
    )
    return target


__all__ = [
    "BootstrapError",
    "build_instance_dict",
    "target_yaml_path",
    "write_campaign_yaml",
]
