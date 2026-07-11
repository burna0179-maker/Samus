"""Surface prospects with no/broken website and classify them for website creation.

Filters a list of ProspectRecords to those with a website gap, assigns a
WebsiteGap kind and a tier (high_value / quick_win / low_priority), and
auto-populates a partial WebsiteBrief ready for operator review or the
website build pipeline.

Usage:
    from backend.website.no_website_classifier import surface, classify

    ranked = surface(records)           # list[(ProspectRecord, NoWebsiteClassification)]
    for rec, cls in ranked:
        print(rec.company_name, cls.tier, cls.pitch_hook)
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict

from backend.prospecting.models import ProspectRecord
from backend.website.models import WebsiteBrief

# Statuses that indicate a prospect has no usable web presence.
# Grouped by the pitch angle each warrants.
_NO_WEBSITE_STATUSES = frozenset({
    "no_website",       # no URL at all
    "domain_unresolved",# DNS dead
    "gone",             # HTTP 410
    "parked",           # parked/placeholder domain
    "social_only",      # Facebook/IG page only, no real site
})

_BROKEN_STATUSES = frozenset({
    "empty",            # blank page
    "unreachable",      # generic unreachable
    "unreachable_timeout",
    "server_error",     # 5xx
})

# access_blocked = WAF blocked the crawl; site is fine for humans → skip
# live = working website → skip


class WebsiteGap(str, Enum):
    NO_WEBSITE = "no_website"   # truly absent
    PARKED = "parked"           # domain exists but unused
    SOCIAL_ONLY = "social_only" # only social profiles
    GONE = "gone"               # domain gone / 410
    BROKEN = "broken"           # broken / unreachable / empty


class WebsiteProspectTier(str, Enum):
    HIGH_VALUE = "high_value"   # lead_score ≥70 AND contactable
    QUICK_WIN = "quick_win"     # lead_score 45-69, or high-score but no contact
    LOW_PRIORITY = "low_priority"


class NoWebsiteClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gap: WebsiteGap
    tier: WebsiteProspectTier
    # Estimated one-time build revenue (cents), sourced from catalog floor.
    estimated_revenue_usd: int
    # One-liner for the callsheet / outreach hook.
    pitch_hook: str
    # Pre-populated brief — operator fills gaps before building.
    brief_draft: WebsiteBrief
    # Recommended next action.
    action: str


# Catalog floor price for a website build (matches service_website_build SKU).
_WEBSITE_BUILD_PRICE_USD = 800

# Tier thresholds mirror prospecting.scorer classify_priority breakpoints.
_HOT_THRESHOLD = 70
_WARM_THRESHOLD = 45


def _gap_from_status(status: str) -> Optional[WebsiteGap]:
    if status in _NO_WEBSITE_STATUSES:
        mapping = {
            "no_website": WebsiteGap.NO_WEBSITE,
            "domain_unresolved": WebsiteGap.NO_WEBSITE,
            "gone": WebsiteGap.GONE,
            "parked": WebsiteGap.PARKED,
            "social_only": WebsiteGap.SOCIAL_ONLY,
        }
        return mapping.get(status, WebsiteGap.NO_WEBSITE)
    if status in _BROKEN_STATUSES:
        return WebsiteGap.BROKEN
    return None


def _is_contactable(record: ProspectRecord) -> bool:
    return bool(record.phone or record.owner_email or record.contact_emails)


def _tier(record: ProspectRecord) -> WebsiteProspectTier:
    if record.lead_score >= _HOT_THRESHOLD and _is_contactable(record):
        return WebsiteProspectTier.HIGH_VALUE
    if record.lead_score >= _WARM_THRESHOLD or (record.lead_score >= _HOT_THRESHOLD):
        return WebsiteProspectTier.QUICK_WIN
    return WebsiteProspectTier.LOW_PRIORITY


def _pitch_hook(record: ProspectRecord, gap: WebsiteGap) -> str:
    name = record.company_name or "your business"
    city = record.city or ""
    suffix = f" in {city}" if city else ""
    if gap == WebsiteGap.NO_WEBSITE:
        return (
            f"{name}{suffix} has no website — potential customers can't find you "
            f"when they search for {record.industry or 'your services'} locally."
        )
    if gap == WebsiteGap.PARKED:
        return (
            f"{name}{suffix} owns the domain but it's parked. "
            "We can turn it into a converting site this week."
        )
    if gap == WebsiteGap.SOCIAL_ONLY:
        return (
            f"{name}{suffix} only appears on social — a website builds trust "
            "and captures leads you're missing right now."
        )
    if gap == WebsiteGap.GONE:
        return (
            f"{name}'s website is gone. "
            "Customers searching for you are hitting a dead end."
        )
    # BROKEN
    return (
        f"{name}{suffix}'s website is down or broken. "
        "Every day it's broken is lost revenue — we can fix or replace it fast."
    )


def _action(gap: WebsiteGap, tier: WebsiteProspectTier) -> str:
    if tier == WebsiteProspectTier.LOW_PRIORITY:
        return "monitor — revisit if lead score improves"
    if gap == WebsiteGap.BROKEN:
        return "pitch website rebuild"
    return "pitch new website build"


def _build_brief(record: ProspectRecord) -> WebsiteBrief:
    city = record.city or ""
    state = record.state or ""
    address = f"{city}, {state}".strip(", ") if city or state else ""
    return WebsiteBrief(
        business_name=record.company_name or "Unknown",
        business_description=record.business_description or "",
        industry=record.industry or "",
        contact_email=record.owner_email or "",
        contact_phone=record.phone or "",
        address=address,
        registered_country="US",
        registered_state=state,
        service_areas=[city] if city else [],
    )


def classify(record: ProspectRecord) -> Optional[NoWebsiteClassification]:
    """Return a classification if the prospect has a website gap; None otherwise."""
    gap = _gap_from_status(record.website_status)
    # Also treat an empty website_url + empty status as no_website
    if gap is None and not record.website_url and not record.website_status:
        gap = WebsiteGap.NO_WEBSITE
    if gap is None:
        return None

    tier = _tier(record)
    return NoWebsiteClassification(
        gap=gap,
        tier=tier,
        estimated_revenue_usd=_WEBSITE_BUILD_PRICE_USD,
        pitch_hook=_pitch_hook(record, gap),
        brief_draft=_build_brief(record),
        action=_action(gap, tier),
    )


def surface(
    records: list[ProspectRecord],
) -> list[tuple[ProspectRecord, NoWebsiteClassification]]:
    """Filter and rank prospects by website gap.

    Returns (record, classification) pairs sorted by tier priority then
    lead_score descending. high_value > quick_win > low_priority.
    """
    _tier_rank = {
        WebsiteProspectTier.HIGH_VALUE: 0,
        WebsiteProspectTier.QUICK_WIN: 1,
        WebsiteProspectTier.LOW_PRIORITY: 2,
    }

    results: list[tuple[ProspectRecord, NoWebsiteClassification]] = []
    for rec in records:
        cls = classify(rec)
        if cls is not None:
            results.append((rec, cls))

    results.sort(key=lambda pair: (_tier_rank[pair[1].tier], -pair[0].lead_score))
    return results


__all__ = [
    "WebsiteGap",
    "WebsiteProspectTier",
    "NoWebsiteClassification",
    "classify",
    "surface",
]
