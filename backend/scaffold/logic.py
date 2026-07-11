"""Asset-generation logic for the scaffold workcell (doc §6).

Pure-Python; no external network or LLM calls. The full LLM-driven generation
lands in a later phase — for now we ship deterministic, audit-friendly assets.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from backend.common import events, persistence
from backend.common.config import get_settings
from backend.common.http_client import signed_post_json_sync
from backend.common.idempotency import GLOBAL_IDEMPOTENCY_STORE

from .models import ScaffoldRequest
from .templates import render_template

_LOG = logging.getLogger("samus.scaffold.logic")

# Container-side default — every workcell anchors its audit ledger under
# /opt/samus/data/<workcell>/ (crm, intake, proposal, seo, … all match). The
# host overrides via SAMUS_SCAFFOLD_AUDIT_PATH. The prior r"E:\…" Windows path
# does not exist in the Cloud Run / Docker image, so the ledger append silently
# OSError'd and the audit trail was lost off-host.
_AUDIT_PATH_DEFAULT = "/opt/samus/data/scaffold/scaffold_audit.jsonl"


def _audit_ledger() -> persistence.JsonlLedger:
    path = os.getenv("SAMUS_SCAFFOLD_AUDIT_PATH", _AUDIT_PATH_DEFAULT)
    return persistence.JsonlLedger(path)


# Inbound deal funnel (Unit S): a rendered asset is registered back into
# samus-crm as an Artifact when the request carried an owner linkage. Maps the
# four scaffold asset types onto the CRM ArtifactKind enum (no `scaffold` /
# `proposal_pack` kind exists — a proposal pack IS the proposal, in
# client-facing form, so it lands as kind="proposal" disambiguated by source).
_ASSET_ARTIFACT_KIND: dict[str, str] = {
    "proposal_pack": "proposal",
    "implementation_plan": "fulfillment_plan",
    "operating_brief": "other",
    "campaign_brief": "other",
}


def _dispatch_artifact_to_crm(req: ScaffoldRequest, payload: dict[str, Any]) -> None:
    """Best-effort: register a rendered scaffold asset as a CRM artifact.

    Fires only when ``ScaffoldRequest.inputs`` carries an owner linkage
    (``opportunity_id`` or ``prospect_id``) — scaffold also runs standalone
    (sandbox / preview), and that path registers nothing. Mirrors
    ``backend.proposal.service._dispatch_artifact_to_crm``: dispatches a
    ``create_artifact`` job through the gateway's ``POST /dispatch/crm`` so
    the operator can pull every deliverable for the deal from one place.
    Config gaps log + skip; transport failures are swallowed — the asset has
    already been rendered.
    """
    inputs = req.inputs or {}
    opportunity_id = str(inputs.get("opportunity_id") or "")
    prospect_id = str(inputs.get("prospect_id") or "")
    if not opportunity_id and not prospect_id:
        return

    settings = get_settings()
    gateway_url = settings.gateway_urls.get("gateway")
    if not gateway_url or not settings.shared_hmac_key:
        _LOG.debug("scaffold crm dispatch skipped: gateway_url / shared_hmac_key unset")
        return

    # opportunity linkage wins over prospect (deal-level beats account-level),
    # mirroring proposal.service._dispatch_artifact_to_crm.
    if opportunity_id:
        owner_kind, owner_id = "opportunity", opportunity_id
    else:
        owner_kind, owner_id = "prospect", prospect_id

    envelope = {
        "task_id": f"scaffold-artifact-{owner_id}",
        "payload": {
            "kind": _ASSET_ARTIFACT_KIND.get(req.asset_type, "other"),
            "owner_entity_kind": owner_kind,
            "owner_entity_id": owner_id,
            "title": req.title,
            "inline_data": {
                "asset_type": req.asset_type,
                "client": req.client,
                "document": payload.get("document", ""),
            },
            "mime_type": "text/markdown",
            "source": "scaffold",
            "created_by": "samus-scaffold",
        },
        "metadata": {"action": "create_artifact"},
    }
    try:
        signed_post_json_sync(gateway_url, "/dispatch/crm", envelope, retries=2)
    except Exception as exc:  # noqa: BLE001 — best-effort, never blocks producer
        _LOG.warning("scaffold crm artifact dispatch failed: %s", exc)


def build_positioning(inputs: dict[str, Any]) -> dict[str, str]:
    """Return ``{problem, mechanism, outcome}`` derived from raw inputs."""
    industry = str(inputs.get("industry") or "operations").strip() or "operations"
    pain = str(inputs.get("pain") or "manual work and coordination drag").strip()
    outcome = str(
        inputs.get("outcome")
        or f"Faster {industry} throughput with audit-ready execution."
    ).strip()
    return {
        "problem": pain,
        "mechanism": "automation-first orchestration with governed execution",
        "outcome": outcome,
    }


def build_offer(positioning: dict[str, str], offer_name: str) -> dict[str, str]:
    """Return ``{headline, mechanism, outcome, price_anchor}``."""
    name = (offer_name or "the offer").strip() or "the offer"
    return {
        "headline": f"{name}: eliminate {positioning.get('problem','')}",
        "mechanism": positioning.get("mechanism", ""),
        "outcome": positioning.get("outcome", ""),
        "price_anchor": "$500-$5,000 depending on scope",
    }


def build_sequence(offer: dict[str, str], brand_voice: str) -> list[dict[str, Any]]:
    """Return a four-step outreach sequence."""
    voice = (brand_voice or "direct").strip() or "direct"
    headline = offer.get("headline", "")
    mechanism = offer.get("mechanism", "")
    outcome = offer.get("outcome", "")
    return [
        {
            "step": 1,
            "message": f"Opening ({voice} voice): introduce the situation and tee up {headline}.",
        },
        {
            "step": 2,
            "message": f"Mechanism: explain {mechanism} and how it removes the bottleneck.",
        },
        {
            "step": 3,
            "message": f"Outcome: walk through {outcome} with one concrete example.",
        },
        {
            "step": 4,
            "message": "Call to action: propose a 20-minute scoping call this week.",
        },
    ]


def generate_scaffold(req: ScaffoldRequest) -> dict[str, Any]:
    """End-to-end asset generation. Returns the full payload dict."""
    key = (
        f"scaffold:{req.asset_type}:"
        f"{req.client.strip().lower()}:{req.title.strip().lower()}"
    )
    cached = GLOBAL_IDEMPOTENCY_STORE.get(key)
    if cached is not None and isinstance(cached, dict):
        _LOG.info("generate_scaffold cache hit key=%s", key)
        return cached

    positioning = build_positioning(req.inputs or {})
    offer = build_offer(positioning, req.offer)
    sequence = build_sequence(offer, req.brand_voice)

    payload: dict[str, Any] = {
        "asset_type": req.asset_type,
        "title": req.title,
        "client": req.client,
        "brand_voice": req.brand_voice,
        "goals": list(req.goals or []),
        "positioning": positioning,
        "offer": offer,
        "sequence": sequence,
    }
    payload["document"] = render_template(req.asset_type, payload)

    # Design-taste Pre-Flight (dormant by default). When armed, run the
    # zero-LLM anti-slop audit over the rendered asset and attach the verdict
    # so the operator/CRM can see the deliverable's quality grade. Best-effort:
    # an audit error must never block a rendered asset.
    _settings = get_settings()
    if getattr(_settings, "taste_audit_enabled", False):
        try:
            from backend.taste.audit import audit_deliverable

            result = audit_deliverable({"document": payload["document"], "kind": req.asset_type})
            audit_dict = result.to_dict()
            # When block_on_fail is armed, surface the verdict on the artifact so
            # the CRM funnel / operator can refuse to ship a hard-failing asset;
            # default (warn-only) records the grade without flagging enforcement.
            _block = bool(getattr(_settings, "taste_audit_block_on_fail", False))
            audit_dict["enforced"] = _block
            audit_dict["ship_blocked"] = bool(_block and not result.passed)
            payload["taste_audit"] = audit_dict
            if not result.passed:
                _LOG.info(
                    "scaffold taste audit flagged %s hard violation(s) on %s (grade %s, blocked=%s)",
                    result.fail_count, key, result.grade, audit_dict["ship_blocked"],
                )
        except Exception as exc:  # noqa: BLE001 — quality signal, never blocks producer
            _LOG.warning("scaffold taste audit failed: %s", exc)

    GLOBAL_IDEMPOTENCY_STORE.set(key, payload)

    audit_event = events.build_audit_event(
        service="scaffold",
        task_id=key,
        action="generate_assets",
        input_payload=req.model_dump(),
        output_payload={"asset_type": req.asset_type, "title": req.title},
        status="completed",
    )
    try:
        _audit_ledger().append(audit_event)
    except OSError as exc:
        _LOG.warning("scaffold audit ledger append failed: %s", exc)

    # Inbound deal funnel (Unit S): when this asset was rendered for a tracked
    # opportunity / prospect, register it back into samus-crm as an Artifact.
    _dispatch_artifact_to_crm(req, payload)

    return payload
