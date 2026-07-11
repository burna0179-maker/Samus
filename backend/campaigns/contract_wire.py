"""CONTRACT_SIGNED → CampaignOrchestrator auto-wire.

When DocuSeal fires submission.completed the outreach webhook calls
``trigger_campaign_on_signing``. This module scans ``clients/*/campaign.yaml``
for a ``campaign_instance.docuseal_slug`` matching the submitter slug on the
event, then creates and starts a campaign run.

Idempotent: if a run for the campaign_id already exists (duplicate webhook
delivery, manual re-run) the existing run is returned and nothing is re-created
or re-driven.

Trial posture: approval gates are entirely controlled by the campaign.yaml —
for Conquerors the YAML sets approval_contact + authorized_signatory, so every
high-risk node lands in the operator queue instead of auto-running.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

_LOG = logging.getLogger("samus.campaigns.contract_wire")

_CLIENTS_ROOT = Path(__file__).resolve().parents[2] / "clients"


def _find_instance_path_by_slug(slug: str) -> Path | None:
    """Return the first clients/*/campaign.yaml whose docuseal_slug == slug."""
    for yaml_path in sorted(_CLIENTS_ROOT.glob("*/campaign.yaml")):
        try:
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        instance = data.get("campaign_instance") or {}
        if str(instance.get("docuseal_slug") or "").strip() == slug:
            return yaml_path
    return None


def trigger_campaign_on_signing(
    slug: str,
    submission_id: int | None = None,
    email: str = "",
) -> dict[str, Any]:
    """Find the campaign bound to this DocuSeal submitter slug and start it.

    Returns::

        {"ok": True,  "campaign_id": ..., "client_id": ..., "existed": bool}
        {"ok": False, "reason": ...}

    Fail-soft: any exception is caught and logged so the calling webhook always
    returns 200 — a campaign-start failure must never reject a DocuSeal delivery.
    """
    if not slug:
        _LOG.debug("contract_wire: empty slug; skipping")
        return {"ok": False, "reason": "no_slug"}

    yaml_path = _find_instance_path_by_slug(slug)
    if yaml_path is None:
        _LOG.info(
            "contract_wire: no campaign.yaml matches slug=%s sub=%s — "
            "no campaign started (configure docuseal_slug in clients/<id>/campaign.yaml "
            "to arm auto-start)",
            slug,
            submission_id,
        )
        return {"ok": False, "reason": "no_campaign_for_slug"}

    try:
        from .orchestrator import CampaignOrchestrator
        from .store import CampaignRunStore
        from .templates import load_instance

        instance = load_instance(yaml_path)
        store = CampaignRunStore()
        existing = store.get(instance.campaign_id)
        if existing is not None:
            _LOG.info(
                "contract_wire: campaign %s already exists (state=%s); skipping re-create",
                instance.campaign_id,
                existing.state,
            )
            return {
                "ok": True,
                "campaign_id": existing.campaign_id,
                "client_id": existing.client_id,
                "existed": True,
                "state": existing.state.value,
            }

        orchestrator = CampaignOrchestrator(store=store)
        run = orchestrator.create_campaign(instance)
        run = orchestrator.start(run.campaign_id)
        _LOG.info(
            "contract_wire: campaign %s STARTED (state=%s slug=%s sub=%s email=%s)",
            run.campaign_id,
            run.state,
            slug,
            submission_id,
            email,
        )
        return {
            "ok": True,
            "campaign_id": run.campaign_id,
            "client_id": run.client_id,
            "existed": False,
            "state": run.state.value,
            "submission_id": submission_id,
        }
    except Exception as exc:  # noqa: BLE001 — fail-soft; webhook must 200
        _LOG.exception("contract_wire: ERROR starting campaign for slug=%s: %s", slug, exc)
        return {"ok": False, "reason": f"orchestrator_error: {exc}"}
