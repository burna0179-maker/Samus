"""Tests for backend.crm.client_directory — email->client lookup."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml


def _write_campaign_yaml(
    root: Path,
    slug: str,
    *,
    client_id: str = "sample_school",
    campaign_id: str = "sample_school_enrollment_2026",
    template_id: str = "school_enrollment_campaign",
    vertical: str = "education",
    approval_contact: str = "<client-email>@example.com",
    authorized_signatory: str = "Kerry Brown",
    docuseal_slug: str = "bJ1CqmfM2vbjv9",
    additional_contacts: list | None = None,
) -> Path:
    d = root / "clients" / slug
    d.mkdir(parents=True)
    inputs = {"approval_contact": approval_contact}
    if additional_contacts is not None:
        inputs["additional_contacts"] = additional_contacts
    doc = {
        "campaign_instance": {
            "client_id": client_id,
            "campaign_id": campaign_id,
            "template_id": template_id,
            "vertical": vertical,
            "docuseal_slug": docuseal_slug,
            "authorized_signatory": authorized_signatory,
            "inputs": inputs,
        }
    }
    p = d / "campaign.yaml"
    p.write_text(yaml.dump(doc), encoding="utf-8")
    return p


def _import_module_with_root(root: Path):
    """Import client_directory with _CLIENTS_ROOT patched to the given root."""
    import importlib

    import backend.crm.client_directory as cd
    importlib.reload(cd)
    cd._CLIENTS_ROOT = root / "clients"
    cd._cache = {}
    cd._cache_stamps = {}
    return cd


def test_lookup_client_returns_none_when_dir_missing(tmp_path):
    cd = _import_module_with_root(tmp_path)  # no clients/ written
    assert cd.lookup_client("someone@example.com") is None
    assert cd.is_known_client("someone@example.com") is False
    assert cd.all_known_clients() == []


def test_lookup_client_returns_none_for_unknown_email(tmp_path):
    _write_campaign_yaml(tmp_path, "sample_school")
    cd = _import_module_with_root(tmp_path)
    assert cd.lookup_client("stranger@example.com") is None


def test_lookup_client_finds_approval_contact(tmp_path):
    _write_campaign_yaml(tmp_path, "sample_school")
    cd = _import_module_with_root(tmp_path)
    kc = cd.lookup_client("<client-email>@example.com")
    assert kc is not None
    assert kc.email == "<client-email>@example.com"
    assert kc.client_id == "sample_school"
    assert kc.campaign_id == "sample_school_enrollment_2026"
    assert kc.role == "approval_contact"
    assert kc.display_name == "Kerry Brown"
    assert kc.docuseal_slug == "bJ1CqmfM2vbjv9"


def test_lookup_client_is_case_insensitive(tmp_path):
    _write_campaign_yaml(tmp_path, "sample_school")
    cd = _import_module_with_root(tmp_path)
    assert cd.lookup_client("PASTOR@BBC4ME.ORG") is not None
    assert cd.lookup_client("  Pastor@BBC4me.org  ") is not None


def test_lookup_client_additional_contacts(tmp_path):
    _write_campaign_yaml(
        tmp_path,
        "sample_school",
        additional_contacts=[
            {"email": "admin@conquerors.example", "role": "admin", "name": "Admin Assistant"},
        ],
    )
    cd = _import_module_with_root(tmp_path)
    kc = cd.lookup_client("admin@conquerors.example")
    assert kc is not None
    assert kc.role == "admin"
    assert kc.display_name == "Admin Assistant"


def test_lookup_client_skips_malformed_yaml(tmp_path):
    d = tmp_path / "clients" / "broken"
    d.mkdir(parents=True)
    (d / "campaign.yaml").write_text("::: not valid yaml {{{", encoding="utf-8")
    # good sibling
    _write_campaign_yaml(tmp_path, "sample_school")
    cd = _import_module_with_root(tmp_path)
    assert cd.lookup_client("<client-email>@example.com") is not None


def test_all_known_clients_returns_snapshot(tmp_path):
    _write_campaign_yaml(tmp_path, "sample_school")
    _write_campaign_yaml(
        tmp_path,
        "another_school",
        client_id="another_school",
        campaign_id="another_school_enrollment",
        approval_contact="ceo@another.example",
        authorized_signatory="Alice Alpha",
    )
    cd = _import_module_with_root(tmp_path)
    everyone = cd.all_known_clients()
    emails = sorted(k.email for k in everyone)
    assert emails == ["ceo@another.example", "<client-email>@example.com"]


def test_cache_invalidates_on_mtime_change(tmp_path):
    p = _write_campaign_yaml(tmp_path, "sample_school")
    cd = _import_module_with_root(tmp_path)
    assert cd.lookup_client("<client-email>@example.com") is not None
    assert cd.lookup_client("newcontact@<client-domain>.example") is None

    # Rewrite with a new approval_contact and bump the mtime.
    import os
    import time

    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    doc["campaign_instance"]["inputs"]["approval_contact"] = "newcontact@<client-domain>.example"
    p.write_text(yaml.dump(doc), encoding="utf-8")
    os.utime(p, (time.time() + 5, time.time() + 5))

    assert cd.lookup_client("newcontact@<client-domain>.example") is not None


def test_lookup_client_ignores_missing_email_field(tmp_path):
    d = tmp_path / "clients" / "no_contact"
    d.mkdir(parents=True)
    doc = {
        "campaign_instance": {
            "client_id": "no_contact",
            "campaign_id": "no_contact_2026",
            "template_id": "school_enrollment_campaign",
            "inputs": {},
        }
    }
    (d / "campaign.yaml").write_text(yaml.dump(doc), encoding="utf-8")
    cd = _import_module_with_root(tmp_path)
    assert cd.all_known_clients() == []


# --- operator_addresses -----------------------------------------------------

def test_operator_addresses_reads_sendgrid_from(monkeypatch):
    from backend.crm import client_directory as cd
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "ahartman@hustleforge.tech")
    monkeypatch.delenv("SAMUS_OPERATOR_EMAILS", raising=False)
    assert cd.operator_addresses() == {"ahartman@hustleforge.tech"}
    assert cd.is_operator_address("Ahartman@Hustleforge.tech") is True
    assert cd.is_operator_address("stranger@example.com") is False


def test_operator_addresses_merges_extras(monkeypatch):
    from backend.crm import client_directory as cd
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "ahartman@hustleforge.tech")
    monkeypatch.setenv(
        "SAMUS_OPERATOR_EMAILS", "alex@hustleforge.tech, ops@hustleforge.tech ,"
    )
    got = cd.operator_addresses()
    assert got == {
        "ahartman@hustleforge.tech",
        "alex@hustleforge.tech",
        "ops@hustleforge.tech",
    }


def test_operator_addresses_empty_when_env_unset(monkeypatch):
    from backend.crm import client_directory as cd
    monkeypatch.delenv("SENDGRID_FROM_EMAIL", raising=False)
    monkeypatch.delenv("SAMUS_OPERATOR_EMAILS", raising=False)
    assert cd.operator_addresses() == set()
    assert cd.is_operator_address("anyone@example.com") is False


# --- find_client_in_text --------------------------------------------------

def test_find_client_in_text_matches_display_name(tmp_path):
    _write_campaign_yaml(tmp_path, "sample_school")
    cd = _import_module_with_root(tmp_path)
    kc = cd.find_client_in_text(
        "Meeting note: Kerry Brown asked about the phased approach."
    )
    assert kc is not None
    assert kc.client_id == "sample_school"


def test_find_client_in_text_matches_email_domain(tmp_path):
    _write_campaign_yaml(tmp_path, "sample_school")
    cd = _import_module_with_root(tmp_path)
    kc = cd.find_client_in_text(
        "Reply-to header pointed at admin@<client-domain>.example for confirmation."
    )
    assert kc is not None
    assert kc.client_id == "sample_school"


def test_find_client_in_text_matches_client_id_humanized(tmp_path):
    _write_campaign_yaml(tmp_path, "sample_school")
    cd = _import_module_with_root(tmp_path)
    kc = cd.find_client_in_text(
        "Subject: Sample School – Back to School Brigade"
    )
    assert kc is not None
    assert kc.client_id == "sample_school"


def test_find_client_in_text_returns_none_on_no_match(tmp_path):
    _write_campaign_yaml(tmp_path, "sample_school")
    cd = _import_module_with_root(tmp_path)
    assert cd.find_client_in_text(
        "Weekly team meeting notes from Global Widget Co."
    ) is None


def test_find_client_in_text_none_for_empty(tmp_path):
    _write_campaign_yaml(tmp_path, "sample_school")
    cd = _import_module_with_root(tmp_path)
    assert cd.find_client_in_text("") is None
    assert cd.find_client_in_text(None) is None  # type: ignore[arg-type]


def test_find_client_in_text_prefers_longest_match(tmp_path):
    """Multiple clients — the more specific (longer) name should win."""
    _write_campaign_yaml(tmp_path, "sample_school")
    _write_campaign_yaml(
        tmp_path,
        "conquerors_ministries",
        client_id="conquerors_ministries",
        campaign_id="conquerors_ministries_2026",
        approval_contact="admin@ministries.example",
        authorized_signatory="Someone Else",
    )
    cd = _import_module_with_root(tmp_path)
    kc = cd.find_client_in_text(
        "Update on Sample School enrollment progress."
    )
    assert kc is not None
    assert kc.client_id == "sample_school"


def test_find_client_in_text_matches_additional_contact(tmp_path):
    _write_campaign_yaml(
        tmp_path,
        "sample_school",
        additional_contacts=[
            {"email": "contact@sample-school.example",
             "role": "liaison", "name": "Frank South"},
        ],
    )
    cd = _import_module_with_root(tmp_path)
    kc = cd.find_client_in_text(
        "Cc'd: contact@sample-school.example per Kerry's request."
    )
    assert kc is not None
    assert kc.client_id == "sample_school"
