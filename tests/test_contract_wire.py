"""Tests for backend.campaigns.contract_wire — CONTRACT_SIGNED auto-start."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from backend.campaigns.contract_wire import (
    _find_instance_path_by_slug,
    trigger_campaign_on_signing,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_campaign_yaml(tmp_path: Path, slug: str, campaign_id: str) -> Path:
    d = tmp_path / "clients" / "test_client"
    d.mkdir(parents=True)
    p = d / "campaign.yaml"
    p.write_text(
        yaml.dump(
            {
                "campaign_instance": {
                    "campaign_id": campaign_id,
                    "client_id": "test_client",
                    "template_id": "school_enrollment_campaign",
                    "vertical": "education",
                    "docuseal_slug": slug,
                    "inputs": {
                        "school_name": "Test School",
                        "website_url": "https://example.com",
                        "enrollment_deadline": "2026-08-12",
                        "target_geography": "Test City, TS",
                        "approval_contact": "admin@example.com",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return p


# ---------------------------------------------------------------------------
# _find_instance_path_by_slug
# ---------------------------------------------------------------------------


def test_find_instance_path_returns_match(tmp_path):
    _write_campaign_yaml(tmp_path, slug="abc123", campaign_id="test_campaign")
    with patch("backend.campaigns.contract_wire._CLIENTS_ROOT", tmp_path / "clients"):
        result = _find_instance_path_by_slug("abc123")
    assert result is not None
    assert result.name == "campaign.yaml"


def test_find_instance_path_returns_none_for_unknown_slug(tmp_path):
    _write_campaign_yaml(tmp_path, slug="abc123", campaign_id="test_campaign")
    with patch("backend.campaigns.contract_wire._CLIENTS_ROOT", tmp_path / "clients"):
        result = _find_instance_path_by_slug("zzz999")
    assert result is None


def test_find_instance_path_skips_invalid_yaml(tmp_path):
    d = tmp_path / "clients" / "bad_client"
    d.mkdir(parents=True)
    (d / "campaign.yaml").write_text(": : not valid yaml {{{", encoding="utf-8")
    _write_campaign_yaml(tmp_path, slug="good_slug", campaign_id="good_campaign")
    with patch("backend.campaigns.contract_wire._CLIENTS_ROOT", tmp_path / "clients"):
        result = _find_instance_path_by_slug("good_slug")
    assert result is not None


# ---------------------------------------------------------------------------
# trigger_campaign_on_signing
# ---------------------------------------------------------------------------


def test_trigger_no_slug_returns_no_slug():
    result = trigger_campaign_on_signing(slug="")
    assert result == {"ok": False, "reason": "no_slug"}


def test_trigger_unknown_slug_returns_no_campaign(tmp_path):
    with patch("backend.campaigns.contract_wire._CLIENTS_ROOT", tmp_path / "clients"):
        result = trigger_campaign_on_signing(slug="unknown_slug")
    assert result["ok"] is False
    assert result["reason"] == "no_campaign_for_slug"


def test_trigger_creates_and_starts_new_campaign(tmp_path):
    _write_campaign_yaml(tmp_path, slug="test_slug", campaign_id="test_enrollment_2026")

    mock_run = MagicMock()
    mock_run.campaign_id = "test_enrollment_2026"
    mock_run.client_id = "test_client"
    mock_run.state.value = "running"

    mock_store = MagicMock()
    mock_store.get.return_value = None  # no existing run

    mock_orch = MagicMock()
    mock_orch.create_campaign.return_value = mock_run
    mock_orch.start.return_value = mock_run

    with (
        patch("backend.campaigns.contract_wire._CLIENTS_ROOT", tmp_path / "clients"),
        patch("backend.campaigns.store.CampaignRunStore", return_value=mock_store),
        patch("backend.campaigns.orchestrator.CampaignOrchestrator", return_value=mock_orch),
    ):
        result = trigger_campaign_on_signing(
            slug="test_slug", submission_id=42, email="test@example.com"
        )

    assert result["ok"] is True
    assert result["campaign_id"] == "test_enrollment_2026"
    assert result["existed"] is False
    mock_orch.create_campaign.assert_called_once()
    mock_orch.start.assert_called_once_with("test_enrollment_2026")


def test_trigger_idempotent_on_existing_run(tmp_path):
    _write_campaign_yaml(tmp_path, slug="dupe_slug", campaign_id="dupe_campaign")

    mock_run = MagicMock()
    mock_run.campaign_id = "dupe_campaign"
    mock_run.client_id = "test_client"
    mock_run.state.value = "running"

    mock_store = MagicMock()
    mock_store.get.return_value = mock_run  # run already exists

    mock_orch_cls = MagicMock()

    with (
        patch("backend.campaigns.contract_wire._CLIENTS_ROOT", tmp_path / "clients"),
        patch("backend.campaigns.store.CampaignRunStore", return_value=mock_store),
        patch("backend.campaigns.orchestrator.CampaignOrchestrator", mock_orch_cls),
    ):
        result = trigger_campaign_on_signing(slug="dupe_slug", submission_id=7)

    assert result.get("ok") is True
    assert result.get("existed") is True
    mock_orch_cls.return_value.create_campaign.assert_not_called()


def test_trigger_fail_soft_on_orchestrator_exception(tmp_path):
    _write_campaign_yaml(tmp_path, slug="boom_slug", campaign_id="boom_campaign")

    mock_store = MagicMock()
    mock_store.get.return_value = None

    mock_orch = MagicMock()
    mock_orch.create_campaign.side_effect = RuntimeError("boom")

    with (
        patch("backend.campaigns.contract_wire._CLIENTS_ROOT", tmp_path / "clients"),
        patch("backend.campaigns.store.CampaignRunStore", return_value=mock_store),
        patch("backend.campaigns.orchestrator.CampaignOrchestrator", return_value=mock_orch),
    ):
        result = trigger_campaign_on_signing(slug="boom_slug")

    assert result["ok"] is False
    assert "orchestrator_error" in result["reason"]
