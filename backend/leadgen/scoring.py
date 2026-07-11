"""Lead-scoring tables + scorer (doc §5.scoring).

Deterministic, in-process scoring. All weights and thresholds live here so they
can be tuned without touching the service orchestrator.
"""

from __future__ import annotations

from typing import Any

from .models import LeadRequest


# --- weight tables --------------------------------------------------------

ICP_INDUSTRIES: dict[str, int] = {
    "healthcare": 20,
    "finance": 20,
    "manufacturing": 18,
    "logistics": 18,
    "insurance": 18,
    "professional_services": 16,
    "construction": 14,
    "legal": 14,
    "retail": 12,
    "technology": 10,
}
_ICP_DEFAULT = 6

GEO_WEIGHTS: dict[str, int] = {
    "US": 8,
    "CA": 7,
    "UK": 7,
    "AU": 6,
    "EU": 6,
}
_GEO_DEFAULT = 4

SIGNAL_WEIGHTS: dict[str, int] = {
    "manual_ops": 14,
    "fragmented_tooling": 12,
    "high_ticket_volume": 12,
    "funding": 10,
    "compliance_pressure": 10,
    "slow_reporting": 10,
    "hiring": 8,
    "expansion": 8,
}

_SEGMENT_POINTS: dict[str, int] = {
    "micro": 4,
    "smb": 15,
    "midmarket": 22,
    "enterprise": 12,
}


def classify_segment(employee_count: int, revenue: int) -> str:
    """Return one of ``micro|smb|midmarket|enterprise``."""
    if employee_count < 10 and revenue < 1_000_000:
        return "micro"
    if employee_count < 100 and revenue < 10_000_000:
        return "smb"
    if employee_count < 1_000 and revenue < 100_000_000:
        return "midmarket"
    return "enterprise"


def _industry_points(industry: str) -> int:
    key = (industry or "").strip().lower().replace(" ", "_").replace("-", "_")
    return ICP_INDUSTRIES.get(key, _ICP_DEFAULT)


def _geo_points(geo: str) -> int:
    key = (geo or "").strip().upper()
    return GEO_WEIGHTS.get(key, _GEO_DEFAULT)


def score_lead(
    req: LeadRequest,
    enrichment: dict[str, Any],
) -> tuple[int, dict[str, int], list[str]]:
    """Score the request. Returns (total, breakdown, matched_signals)."""
    segment = classify_segment(req.employee_count, req.annual_revenue_usd)

    industry_pts = _industry_points(req.industry)
    segment_pts = _SEGMENT_POINTS.get(segment, 0)
    geo_pts = _geo_points(req.geo)

    matched: list[str] = []
    signal_pts = 0
    for raw in req.signals or []:
        key = (raw or "").strip().lower()
        if key in SIGNAL_WEIGHTS:
            matched.append(key)
            signal_pts += SIGNAL_WEIGHTS[key]

    bonus_pts = 0
    if req.employee_count >= 25:
        bonus_pts += 5
    if req.annual_revenue_usd >= 2_000_000:
        bonus_pts += 5

    total = industry_pts + segment_pts + geo_pts + signal_pts + bonus_pts
    total = max(0, min(100, total))

    breakdown = {
        "industry": industry_pts,
        "segment": segment_pts,
        "geo": geo_pts,
        "signals": signal_pts,
        "bonus": bonus_pts,
        "enrichment_signal_count": int(enrichment.get("signal_count", 0)),
    }
    return total, breakdown, matched


def tier_for_score(score: int) -> str:
    if score >= 85:
        return "priority"
    if score >= 70:
        return "high"
    if score >= 45:
        return "medium"
    return "low"


_SIGNAL_RECOMMENDATIONS: dict[str, str] = {
    "manual_ops": "Workflow replacement opportunity — pitch automation-first ops.",
    "fragmented_tooling": "Consolidation play — replace point tools with a unified stack.",
    "high_ticket_volume": "Throughput pitch — bulk-resolution playbook + ticket triage automation.",
    "funding": "Time the outreach to capital deployment window.",
    "compliance_pressure": "Lead with governed-execution + audit-ledger story.",
    "slow_reporting": "Show same-day reporting + executive dashboards.",
    "hiring": "Position as headcount-leverage: tooling instead of FTEs.",
    "expansion": "Map the offer to the new market or product line.",
}

_TIER_RECOMMENDATIONS: dict[str, str] = {
    "priority": "Assign senior closer and book an executive briefing within 5 business days.",
    "high": "Multi-thread outreach: ops lead + finance lead + executive sponsor.",
    "medium": "Run a discovery sprint before investing senior cycles.",
    "low": "Drop into nurture; revisit on signal change.",
}

_SEGMENT_RECOMMENDATIONS: dict[str, str] = {
    "micro": "Self-serve bundle; minimize sales cycle.",
    "smb": "Standard SMB motion: 2-call cycle, packaged offer.",
    "midmarket": "POC scoped to one workflow before expansion.",
    "enterprise": "Procurement + security review track in parallel.",
}


def build_recommendations(
    segment: str,
    tier: str,
    matched_signals: list[str],
) -> list[str]:
    """Return 3-5 context-aware recommendation strings."""
    recs: list[str] = []
    if tier in _TIER_RECOMMENDATIONS:
        recs.append(_TIER_RECOMMENDATIONS[tier])
    if segment in _SEGMENT_RECOMMENDATIONS:
        recs.append(_SEGMENT_RECOMMENDATIONS[segment])
    for sig in matched_signals:
        rec = _SIGNAL_RECOMMENDATIONS.get(sig)
        if rec and rec not in recs:
            recs.append(rec)
        if len(recs) >= 5:
            break
    if len(recs) < 3:
        recs.append("Confirm budget authority and decision timeline on the first call.")
    if len(recs) < 3:
        recs.append("Document the current process before proposing a replacement.")
    return recs[:5]
