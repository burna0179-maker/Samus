"""Thin async CRM client for the strategy workcell.

Reads Prospect records from the CRM service via signed_post_json and
maps them to StrategyContext instances for the engine.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from backend.common.http_client import signed_post_json

from .engine import StrategyContext

_LOG = logging.getLogger("samus.strategy.crm_client")

SAMUS_CRM_URL: str = os.getenv("SAMUS_CRM_URL", "http://samus-crm:8080")


async def fetch_prospect(prospect_id: str) -> dict[str, Any] | None:
    """Return a Prospect dict from the CRM, or None on 404.

    Uses signed_post_json with an empty body to the prospect resource
    path (CRM service exposes POST /crm/prospects/{id} for signed reads).
    """
    path = f"/crm/prospects/{prospect_id}"
    try:
        response = await signed_post_json(SAMUS_CRM_URL, path, payload={})
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("crm fetch_prospect failed prospect_id=%s error=%s", prospect_id, exc)
        return None

    if response.status_code == 404:
        return None
    if response.status_code == 200:
        return response.json()

    _LOG.warning(
        "crm fetch_prospect unexpected status prospect_id=%s status=%s",
        prospect_id,
        response.status_code,
    )
    return None


async def build_context(prospect_id: str) -> StrategyContext:
    """Build a StrategyContext from CRM data.

    Falls back to default values (lead_score=0, seo_score=100, stage="new")
    when the prospect is not found or the CRM is unreachable.
    """
    prospect = await fetch_prospect(prospect_id)

    if prospect is None:
        return StrategyContext(prospect_id=prospect_id)

    return StrategyContext(
        prospect_id=prospect_id,
        lead_score=float(prospect.get("lead_score", 0.0)),
        seo_score=float(prospect.get("seo_score", 100.0)),
        stage=prospect.get("status", "new"),
        engagement=prospect.get("engagement_level", "low"),
        last_activity=prospect.get("last_activity_at"),
        conversion_signals=list(prospect.get("signals", [])),
    )
