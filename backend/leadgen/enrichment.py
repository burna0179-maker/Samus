"""Lightweight enrichment shims (doc §5.enrichment).

This module produces a small structured-enrichment dict that downstream scoring
can consume. Real enrichment (firmographics, technographics, intent signals)
lands in a later phase; for now we surface the deterministic, in-process facts.
"""

from __future__ import annotations

from typing import Any

from .models import LeadRequest


def enrich_lead(req: LeadRequest) -> dict[str, Any]:
    """Return derived facts about ``req`` for the scoring stage."""
    company = (req.company or "").strip()
    domain = (req.domain or "").strip()
    return {
        "normalized_company": company.title(),
        "signal_count": len(req.signals or []),
        "has_corporate_domain": "." in domain,
    }
