"""process_lead — orchestrates the leadgen pipeline.

LeadRequest -> normalize_domain -> enrich -> classify_segment -> score -> tier
-> recommendations -> LeadScore. Idempotency keyed on
``f"{normalized_domain}:{company.lower()}"``. Audit event appended to the
service ledger on first computation.
"""

from __future__ import annotations

import logging
import os

from backend.common import events, persistence
from backend.common.idempotency import GLOBAL_IDEMPOTENCY_STORE

from .enrichment import enrich_lead
from .models import LeadRequest, LeadScore
from .normalizer import normalize_domain
from .scoring import (
    build_recommendations,
    classify_segment,
    score_lead,
    tier_for_score,
)

_LOG = logging.getLogger("samus.leadgen.service")

# Container-side default — every workcell anchors its audit ledger under
# /opt/samus/data/<workcell>/ (crm, intake, proposal, seo, … all match). The
# host overrides via SAMUS_LEADGEN_AUDIT_PATH. The prior r"E:\…" Windows path
# does not exist in the Cloud Run / Docker image, so the ledger append silently
# OSError'd and the audit trail was lost off-host.
_AUDIT_PATH_DEFAULT = "/opt/samus/data/leadgen/leadgen_audit.jsonl"


def _audit_ledger() -> persistence.JsonlLedger:
    path = os.getenv("SAMUS_LEADGEN_AUDIT_PATH", _AUDIT_PATH_DEFAULT)
    return persistence.JsonlLedger(path)


def _build_reasons(
    req: LeadRequest,
    enrichment: dict,
    segment: str,
    breakdown: dict,
    matched: list[str],
) -> list[str]:
    reasons: list[str] = []
    reasons.append(
        f"segment={segment} (employees={req.employee_count}, revenue={req.annual_revenue_usd})"
    )
    reasons.append(f"industry={req.industry.lower()} points={breakdown['industry']}")
    reasons.append(f"geo={req.geo.upper()} points={breakdown['geo']}")
    if matched:
        reasons.append(f"matched_signals={','.join(matched)} (points={breakdown['signals']})")
    else:
        reasons.append("no ICP signals matched")
    if breakdown["bonus"]:
        reasons.append(f"size_bonus={breakdown['bonus']}")
    if not enrichment.get("has_corporate_domain", True):
        reasons.append("domain lacks a TLD — verify corporate identity")
    return reasons


def process_lead(req: LeadRequest, *, task_id: str | None = None) -> LeadScore:
    """End-to-end lead qualification."""
    normalized = normalize_domain(req.domain)
    idempotency_key = f"{normalized}:{req.company.strip().lower()}"

    cached = GLOBAL_IDEMPOTENCY_STORE.get(idempotency_key)
    if cached is not None and isinstance(cached, dict):
        _LOG.info("process_lead cache hit key=%s", idempotency_key)
        return LeadScore.model_validate(cached)

    enrichment = enrich_lead(req)
    segment = classify_segment(req.employee_count, req.annual_revenue_usd)
    total, breakdown, matched = score_lead(req, enrichment)
    tier = tier_for_score(total)
    recommendations = build_recommendations(segment, tier, matched)
    reasons = _build_reasons(req, enrichment, segment, breakdown, matched)

    result = LeadScore(
        company=enrichment.get("normalized_company") or req.company.strip(),
        normalized_domain=normalized,
        segment=segment,  # type: ignore[arg-type]
        score=total,
        tier=tier,  # type: ignore[arg-type]
        matched_signals=matched,
        reasons=reasons,
        recommendations=recommendations,
    )

    GLOBAL_IDEMPOTENCY_STORE.set(idempotency_key, result.model_dump())

    audit_event = events.build_audit_event(
        service="leadgen",
        task_id=task_id or idempotency_key,
        action="score_lead",
        input_payload=req.model_dump(),
        output_payload=result.model_dump(),
        status="completed",
        metadata={"breakdown": breakdown},
    )
    try:
        _audit_ledger().append(audit_event)
    except OSError as exc:
        _LOG.warning("leadgen audit ledger append failed: %s", exc)

    _LOG.info(
        "process_lead complete",
        extra={
            "company": result.company,
            "domain": normalized,
            "score": total,
            "tier": tier,
        },
    )
    return result
