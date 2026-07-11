"""Tests for ``backend.campaigns.bootstrap_from_lead``.

The load-bearing invariant: a StoredLead carrying the v3 Social presence
fieldset values ends up as a ``clients/<slug>/campaign.yaml`` the existing
``contract_wire`` + ``client_directory`` modules can load without changes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from backend.campaigns.bootstrap_from_lead import (
    BootstrapError,
    build_instance_dict,
    target_yaml_path,
    write_campaign_yaml,
)
from backend.intake.models import StoredLead


def _lead(**overrides) -> StoredLead:
    """A StoredLead exercising every social-hint field, overrideable."""
    base = dict(
        lead_id="lead_test",
        created_at="2026-07-10T00:00:00Z",
        name="Jane Smith",
        email="jane@acme.com",
        company="Acme Widgets",
        website_url="https://acme.com",
        service_interest=["seo_audit"],
        pain_points="Manual follow-up is broken.",
        monthly_budget="$500-$2000",
        timeline="this_month",
        social_facebook="facebook.com/acme",
        social_instagram="@acme",
        social_linkedin="linkedin.com/company/acme",
        brand_voice_notes="Friendly, direct, no jargon.",
        social_cadence_pref="moderate",
        source_ip="1.2.3.4",
        user_agent="ua",
        dedup_key="d",
    )
    base.update(overrides)
    return StoredLead(**base)


# ---------------------------------------------------------------------------
# build_instance_dict — pure conversion
# ---------------------------------------------------------------------------


def test_build_instance_populates_socials_and_cadence():
    payload = build_instance_dict(
        _lead(),
        template_id="school_enrollment_campaign",
        client_id="acme_widgets",
        campaign_id="acme_widgets_enrollment_2026",
        docuseal_slug="acme-widgets",
    )

    inst = payload["campaign_instance"]
    assert inst["campaign_id"] == "acme_widgets_enrollment_2026"
    assert inst["client_id"] == "acme_widgets"
    assert inst["template_id"] == "school_enrollment_campaign"
    assert inst["docuseal_slug"] == "acme-widgets"

    inputs = inst["inputs"]
    assert inputs["approval_contact"] == "jane@acme.com"
    assert inputs["authorized_signatory"] == "Jane Smith"
    assert inputs["client_display_name"] == "Acme Widgets"
    assert inputs["website_url"] == "https://acme.com"
    # 3 present handles → 3 channels.
    assert inputs["social_channels"] == ["facebook", "instagram", "linkedin"]
    assert inputs["social_handles"] == {
        "facebook": "facebook.com/acme",
        "instagram": "@acme",
        "linkedin": "linkedin.com/company/acme",
    }
    # moderate → 3/week per the mapping documented on the bootstrap.
    assert inputs["social_posting_cadence"] == "3/week"
    assert inputs["brand_voice_notes"] == "Friendly, direct, no jargon."


def test_build_instance_omits_absent_channels():
    """Only the handles the operator supplied should surface as channels."""
    lead = _lead(social_facebook="", social_linkedin="  ")
    payload = build_instance_dict(
        lead,
        template_id="school_enrollment_campaign",
        client_id="acme",
        campaign_id="acme_2026",
    )
    inputs = payload["campaign_instance"]["inputs"]
    assert inputs["social_channels"] == ["instagram"]
    assert inputs["social_handles"] == {"instagram": "@acme"}


def test_build_instance_omits_cadence_when_operator_skipped():
    """Empty social_cadence_pref must NOT stomp the template default."""
    lead = _lead(social_cadence_pref="")
    payload = build_instance_dict(
        lead,
        template_id="school_enrollment_campaign",
        client_id="acme",
        campaign_id="acme_2026",
    )
    inputs = payload["campaign_instance"]["inputs"]
    assert "social_posting_cadence" not in inputs


def test_build_instance_omits_social_block_entirely_when_no_hints():
    """No handles + no cadence + no brand voice = no social keys at all."""
    lead = _lead(
        social_facebook="",
        social_instagram="",
        social_linkedin="",
        social_cadence_pref="",
        brand_voice_notes="",
    )
    payload = build_instance_dict(
        lead,
        template_id="school_enrollment_campaign",
        client_id="acme",
        campaign_id="acme_2026",
    )
    inputs = payload["campaign_instance"]["inputs"]
    for k in (
        "social_channels",
        "social_handles",
        "social_posting_cadence",
        "brand_voice_notes",
    ):
        assert k not in inputs, f"unexpected {k!r} = {inputs.get(k)!r}"
    # Contact fields still land — they are not part of the social fieldset.
    assert inputs["approval_contact"] == "jane@acme.com"


def test_build_instance_merges_extra_inputs_last():
    """Caller-supplied inputs win over derived defaults."""
    payload = build_instance_dict(
        _lead(),
        template_id="school_enrollment_campaign",
        client_id="acme",
        campaign_id="acme_2026",
        extra_inputs={
            "school_name": "Acme Academy",
            # Deliberately clobber the derived cadence to prove precedence.
            "social_posting_cadence": "5/week",
        },
    )
    inputs = payload["campaign_instance"]["inputs"]
    assert inputs["school_name"] == "Acme Academy"
    assert inputs["social_posting_cadence"] == "5/week"


def test_build_instance_rejects_empty_identity():
    for kwargs in (
        {"template_id": "", "client_id": "c", "campaign_id": "c_2026"},
        {"template_id": "t", "client_id": "  ", "campaign_id": "c_2026"},
        {"template_id": "t", "client_id": "c", "campaign_id": ""},
    ):
        with pytest.raises(BootstrapError):
            build_instance_dict(_lead(), **kwargs)


# ---------------------------------------------------------------------------
# write_campaign_yaml — disk side + refuse-to-clobber
# ---------------------------------------------------------------------------


def test_write_campaign_yaml_writes_expected_path(tmp_path: Path):
    target = write_campaign_yaml(
        _lead(),
        template_id="school_enrollment_campaign",
        client_id="Acme Widgets",  # normalized → acme_widgets
        campaign_id="acme_widgets_2026",
        docuseal_slug="acme-widgets",
        clients_root=tmp_path,
    )
    assert target == tmp_path / "acme_widgets" / "campaign.yaml"
    assert target.exists()

    # YAML must be loadable by the same modules that read the hand-written
    # client YAMLs today.
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    inst = data["campaign_instance"]
    assert inst["client_id"] == "Acme Widgets"  # value preserved verbatim
    assert inst["docuseal_slug"] == "acme-widgets"
    assert inst["inputs"]["social_channels"] == [
        "facebook",
        "instagram",
        "linkedin",
    ]
    assert inst["inputs"]["social_posting_cadence"] == "3/week"


def test_write_campaign_yaml_refuses_to_clobber(tmp_path: Path):
    """Existing files (potentially hand-tuned) must not be silently replaced."""
    kwargs = dict(
        template_id="school_enrollment_campaign",
        client_id="acme",
        campaign_id="acme_2026",
        clients_root=tmp_path,
    )
    write_campaign_yaml(_lead(), **kwargs)
    with pytest.raises(BootstrapError, match="already exists"):
        write_campaign_yaml(_lead(), **kwargs)


def test_write_campaign_yaml_overwrite_is_explicit(tmp_path: Path):
    kwargs = dict(
        template_id="school_enrollment_campaign",
        client_id="acme",
        campaign_id="acme_2026",
        clients_root=tmp_path,
    )
    first = write_campaign_yaml(_lead(), **kwargs)
    first_text = first.read_text(encoding="utf-8")
    second = write_campaign_yaml(
        _lead(social_facebook=""),
        overwrite=True,
        **kwargs,
    )
    assert second == first
    second_text = second.read_text(encoding="utf-8")
    assert first_text != second_text
    # Facebook handle gone → facebook not in channels.
    data = yaml.safe_load(second_text)
    assert "facebook" not in data["campaign_instance"]["inputs"]["social_channels"]


def test_target_yaml_path_normalizes_slug():
    p = target_yaml_path("Acme Widgets")
    assert p.name == "campaign.yaml"
    assert p.parent.name == "acme_widgets"


def test_target_yaml_path_rejects_blank_slug():
    with pytest.raises(BootstrapError):
        target_yaml_path("   ")


# ---------------------------------------------------------------------------
# Cross-check: the bootstrap output is loadable by client_directory
# ---------------------------------------------------------------------------


def test_bootstrap_output_loadable_by_client_directory(tmp_path: Path, monkeypatch):
    """A generated YAML must be indexed as a known client on next read."""
    import backend.crm.client_directory as cd

    write_campaign_yaml(
        _lead(),
        template_id="school_enrollment_campaign",
        client_id="acme_widgets",
        campaign_id="acme_widgets_2026",
        docuseal_slug="acme-widgets",
        clients_root=tmp_path,
    )
    monkeypatch.setattr(cd, "_CLIENTS_ROOT", tmp_path)
    # Force a rebuild — the module caches mtimes.
    monkeypatch.setattr(cd, "_cache", {})
    monkeypatch.setattr(cd, "_cache_stamps", {})

    known = cd.lookup_client("jane@acme.com")
    assert known is not None
    assert known.client_id == "acme_widgets"
    assert known.campaign_id == "acme_widgets_2026"
    assert known.docuseal_slug == "acme-widgets"
    assert known.role == "approval_contact"
