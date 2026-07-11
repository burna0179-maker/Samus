"""Lead scoring for prospecting.

Continuous 0-100 score across four equally-weighted 25-point components:
industry fit, review rating, review volume, and SEO opportunity (the worse a
prospect's SEO, the more there is for HustleForge to fix + sell). The earlier
scorer summed coarse step-functions that topped out at 52 — `hot` was
unreachable and warm leads bunched in a 3-point band; this one spreads the
population across the full range so the strongest leads sort to the top.

A learning-based scorer would replace this in a later phase.
"""

from __future__ import annotations

import math

from .models import ProspectRecord


# Industry fit — points out of _INDUSTRY_MAX. The prospecting pipeline forwards
# Google Places searchText keywords verbatim through Prospect.industry, so the
# 7 target verticals must match as exact lowercase strings. Tiers derived from
# the worked examples in services/workflow_rescue/example_workflows.md +
# retainer/ai_ops_partner/example_engagements.md.
INDUSTRY_WEIGHTS: dict[str, int] = {
    # Google Places searchText keywords — the live prospecting vocabulary.
    "real estate agency": 25,
    "dentist": 25,
    "accounting firm": 20,
    "hvac contractor": 20,
    "plumber": 20,
    "roofing contractor": 20,
    "car dealer": 18,
    # Legacy industry-category vocabulary — vestigial (prospecting feeds the
    # Places keywords above), kept so non-prospecting callers still resolve.
    "healthcare": 25,
    "finance": 25,
    "manufacturing": 22,
    "logistics": 22,
    "insurance": 22,
    "professional_services": 20,
    "construction": 17,
    "legal": 17,
    "retail": 15,
    "technology": 12,
}
DEFAULT_INDUSTRY_WEIGHT = 8

_INDUSTRY_MAX = 25
_RATING_MAX = 25
_REVIEWS_MAX = 25
_SEO_MAX = 25

# Review count at which the (log-scaled) reviews component saturates at full
# marks — a 500-review business is already a maximally-established local.
_REVIEW_SATURATION = 500

# SEO-opportunity credit awarded when the SEO score is genuinely *unknown* —
# an seo_score of 0 the crawl could not actually measure (a WAF block, a
# transient timeout, an unreachable host, an audit that never ran). Scoring an
# un-measured site as "worst SEO = max opportunity" wrongly floated such
# prospects to the top of the call list (the 2026-05-21 false-positive sweep);
# neutral half-credit is the honest placeholder.
_SEO_UNKNOWN_FRACTION = 0.5

# Website statuses that are POSITIVE proof of an absent / dead web presence —
# the one case where an seo_score of 0 genuinely IS the pitch ("you have no
# real website"), so it keeps full SEO-opportunity credit. Every other reason
# for a 0 (access_blocked, unreachable_timeout, unreachable, server_error,
# http_error, empty, or a live site whose audit never produced a score) is
# treated as unknown — see _seo_points_for.
_GENUINE_NO_WEBSITE: frozenset[str] = frozenset(
    {
        "no_website",
        "parked",
        "social_only",
        "domain_unresolved",
        "gone",
    }
)


def _rating_points(raw: str) -> float:
    """Continuous 0-25 from a 0-5 star rating. 3.0 stars or below scores 0;
    each star above 3.0 is worth 12.5 points, so 5.0 -> 25, 4.5 -> 18.75."""
    try:
        rating = float(raw) if raw else 0.0
    except ValueError:
        return 0.0
    fraction = max(0.0, min(1.0, (rating - 3.0) / 2.0))
    return fraction * _RATING_MAX


def _review_points(raw: str) -> float:
    """Log-scaled 0-25 from review count. Review volume is heavily skewed, so
    a linear scale would let a 400-review business barely outscore a 12-review
    one; log scaling gives the early reviews real weight and saturates at
    _REVIEW_SATURATION (~10 reviews -> 9.6, 50 -> 15.7, 400 -> 24.1)."""
    try:
        count = int(raw) if raw else 0
    except ValueError:
        return 0.0
    if count <= 0:
        return 0.0
    fraction = math.log10(count + 1) / math.log10(_REVIEW_SATURATION + 1)
    return min(1.0, fraction) * _REVIEWS_MAX


def _seo_opportunity_points(seo_score: int) -> float:
    """0-25 from the INVERSE of seo_score — a prospect with poor SEO is the
    better lead (more for HustleForge to fix + sell). seo_score 0 -> 25,
    seo_score 100 -> 0. A 0 from a genuinely unreachable / absent / broken site
    still reads as max opportunity, which is intentional: a broken web presence
    is a strong pitch.

    The one case this is *wrong* for — a healthy site whose SEO we simply could
    not measure because a WAF blocked the crawl — is handled by the caller
    (:func:`score_prospect`), which substitutes neutral credit instead of
    calling this with a meaningless 0.
    """
    score = max(0, min(100, int(seo_score or 0)))
    return (100 - score) / 100 * _SEO_MAX


def _seo_points_for(p: ProspectRecord) -> float:
    """SEO-opportunity points for a prospect, honouring the unknown-vs-broken
    distinction.

    An ``seo_score`` of 0 only earns the full max-opportunity credit when the
    website status is positive proof of an absent web presence (see
    ``_GENUINE_NO_WEBSITE``) — that genuinely is the pitch. A 0 from a crawl
    that was blocked / timed out / never completed is *unknown*, not worst, and
    gets neutral credit instead so a transient crawl failure cannot inflate the
    lead score. Any measured score (> 0) is scored normally.
    """
    score = max(0, min(100, int(p.seo_score or 0)))
    if score == 0:
        status = (p.website_status or "").strip().lower()
        if status not in _GENUINE_NO_WEBSITE:
            return _SEO_UNKNOWN_FRACTION * _SEO_MAX
    return _seo_opportunity_points(score)


def score_prospect(p: ProspectRecord) -> int:
    """Continuous 0-100 lead score — four equally-weighted 25-point components.

    Reads ``p.seo_score`` + ``p.website_status``, so
    :func:`backend.prospecting.service.process_discovery` must score a prospect
    *after* the Step 2 SEO pass has populated both. An ``seo_score`` of 0 earns
    full SEO-opportunity credit only when ``website_status`` proves an absent
    web presence; a 0 the crawl could not measure (blocked / timed out /
    unreachable) gets neutral credit instead — see :func:`_seo_points_for`.
    """
    industry = min(
        INDUSTRY_WEIGHTS.get(p.industry.lower(), DEFAULT_INDUSTRY_WEIGHT),
        _INDUSTRY_MAX,
    )
    total = (
        industry
        + _rating_points(p.review_rating)
        + _review_points(p.review_count)
        + _seo_points_for(p)
    )
    return int(round(min(100.0, total)))


def classify_priority(score: int) -> str:
    """Tier from the 0-100 lead score. Thresholds recalibrated for the
    continuous scorer — the previous 75/50 cuts were written for a 0-100 scale
    the old step-function formula (max 52) could never reach."""
    if score >= 70:
        return "hot"
    if score >= 45:
        return "warm"
    return "low"
