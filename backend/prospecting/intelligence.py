"""Business signal analysis for the prospecting workcell.

Responsibilities:
  - analyze_business: extract normalised signals from raw business data
  - score_opportunity: convert signals into 5-axis opportunity scores (0-100)
  - map_products: map scores to primary + secondary product recommendations
  - determine_pitch_angle: select the strongest opening narrative

Feeds deal_scoring.score_deal (via the ``intel`` dict) and downstream
callsheet generation.  All functions are pure; no I/O, no external deps.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "analyze_business",
    "score_opportunity",
    "map_products",
    "determine_pitch_angle",
]

# Platform detection patterns keyed on platform slug.
_PLATFORM_PATTERNS: dict[str, re.Pattern[str]] = {
    "wix": re.compile(r"wix", re.IGNORECASE),
    "squarespace": re.compile(r"squarespace", re.IGNORECASE),
    "wordpress": re.compile(r"wordpress|wp-content|wp-json", re.IGNORECASE),
    "shopify": re.compile(r"shopify|myshopify", re.IGNORECASE),
}

# Axis declaration order — used for tie-breaking in map_products.
_AXIS_ORDER = ("website", "seo", "ads", "automation", "reputation")

_AXIS_TO_PRODUCT: dict[str, str] = {
    "website": "website_build",
    "seo": "seo_package",
    "ads": "ads_management",
    "automation": "workflow_automation",
    "reputation": "reputation_management",
}


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def analyze_business(business_data: dict[str, Any]) -> dict[str, Any]:
    """Detect signals from raw business data.

    Tolerant of missing keys; all fields fall back to safe defaults.

    Args:
        business_data: Loose dict from discovery / enrichment pipeline.

    Returns:
        9-key signals dict ready for score_opportunity.
    """
    website_url: str = business_data.get("website_url", "") or ""
    has_website: bool = bool(website_url.strip())

    features: list[str] = business_data.get("website_features", []) or []
    features_lower = [str(f).lower() for f in features]
    has_cta: bool = any(f in features_lower for f in ("cta", "call_to_action"))
    has_booking: bool = any(f in features_lower for f in ("booking", "calendar"))

    # Platform: prefer explicit field, then scan html signals.
    raw_platform: str = (business_data.get("platform") or "").strip().lower()
    if raw_platform in _PLATFORM_PATTERNS:
        platform = raw_platform
    else:
        html_signals: list[str] = business_data.get("website_html_signals", []) or []
        combined_html = " ".join(str(s) for s in html_signals)
        platform = "unknown"
        for slug, pattern in _PLATFORM_PATTERNS.items():
            if pattern.search(combined_html):
                platform = slug
                break

    tech_stack: list[str] = list(business_data.get("tech_stack", []) or [])

    review_count: int = int(business_data.get("review_count", 0) or 0)

    raw_rating = business_data.get("rating", 0.0)
    try:
        rating: float = float(raw_rating)
    except (TypeError, ValueError):
        rating = 0.0
    rating = max(0.0, min(5.0, rating))

    ads_detected: bool = bool(business_data.get("ads_detected", False))

    competitor_count: int = int(business_data.get("competitor_count", 0) or 0)

    return {
        "has_website": has_website,
        "has_cta": has_cta,
        "has_booking": has_booking,
        "platform": platform,
        "tech_stack": tech_stack,
        "review_count": review_count,
        "rating": rating,
        "ads_detected": ads_detected,
        "competitor_count": competitor_count,
    }


def score_opportunity(signals: dict[str, Any]) -> dict[str, int]:
    """Convert signals into 5-axis opportunity scores.

    Higher score = bigger gap = stronger opportunity.

    Args:
        signals: Output of analyze_business.

    Returns:
        Dict with keys: website, seo, ads, automation, reputation (each 0-100).
    """
    has_website: bool = bool(signals.get("has_website"))
    has_cta: bool = bool(signals.get("has_cta"))
    has_booking: bool = bool(signals.get("has_booking"))
    platform: str = str(signals.get("platform", "unknown"))
    review_count: int = int(signals.get("review_count", 0))
    rating: float = float(signals.get("rating", 0.0))
    ads_detected: bool = bool(signals.get("ads_detected"))
    competitor_count: int = int(signals.get("competitor_count", 0))

    # --- website axis ---
    if not has_website:
        website = 100
    elif has_website and not has_cta:
        website = 60
    elif has_website and has_cta and not has_booking:
        website = 30
    else:  # has_website + has_cta + has_booking
        website = 10

    # --- seo axis ---
    seo_base = 40 if platform == "wordpress" else 60
    seo = seo_base
    if competitor_count > 5:
        seo += 20
    if not ads_detected:
        seo += 20

    # --- ads axis ---
    ads = 80 if (not ads_detected and review_count > 10) else 40

    # --- automation axis ---
    automation = 30 if has_booking else 90
    if not has_cta:
        automation += 10

    # --- reputation axis ---
    if review_count < 5:
        reputation = 90
    elif rating < 4.0:
        reputation = 60
    elif rating < 4.5:
        reputation = 30
    else:
        reputation = 10

    def _clamp(v: int) -> int:
        return max(0, min(100, v))

    return {
        "website": _clamp(website),
        "seo": _clamp(seo),
        "ads": _clamp(ads),
        "automation": _clamp(automation),
        "reputation": _clamp(reputation),
    }


def map_products(scores: dict[str, int]) -> dict[str, str | None]:
    """Map opportunity scores to primary + secondary product recommendations.

    Args:
        scores: Output of score_opportunity.

    Returns:
        Dict with keys ``primary`` (str) and ``secondary`` (str | None).
        secondary is None if the second-ranked axis scores below 50.
    """
    # Sort axes by score descending, tie-break by _AXIS_ORDER index.
    ranked = sorted(
        _AXIS_ORDER,
        key=lambda axis: (-scores.get(axis, 0), _AXIS_ORDER.index(axis)),
    )

    primary = _AXIS_TO_PRODUCT[ranked[0]]
    secondary_axis = ranked[1] if len(ranked) > 1 else None
    secondary: str | None = None
    if secondary_axis is not None and scores.get(secondary_axis, 0) >= 50:
        secondary = _AXIS_TO_PRODUCT[secondary_axis]

    return {"primary": primary, "secondary": secondary}


def determine_pitch_angle(
    signals: dict[str, Any],
    scores: dict[str, int],
    learned_performance: dict[str, float] | None = None,
) -> str:
    """Select the strongest opening narrative for this prospect.

    Priority order (first applicable wins):
      trust_gap       — low reputation or no website
      conversion_leak — website exists but no conversion paths
      time_leak       — high automation opportunity
      visibility_gap  — high seo or ads opportunity
      general_growth  — fallback (always applicable)

    Feedback loop: when ``learned_performance`` (angle -> win-rate, from
    :func:`backend.common.feedback_store.get_angle_performance`) is supplied,
    the choice is re-ranked **among the applicable angles only** — if a
    different applicable angle has a strictly higher learned win-rate than the
    deterministic pick, it wins. Unsampled angles take a neutral 0.5 prior, so a
    proven angle must actually beat that prior (or the current pick) to deviate,
    and a single early loss can never drag selection onto a worse-fitting
    narrative. Crucially, re-ranking is bounded to angles that already *apply*
    to this prospect, so learning can prefer a better-closing narrative but
    never pitch an irrelevant one. With ``learned_performance`` None or empty,
    this is byte-for-byte the prior deterministic behaviour.

    Args:
        signals: Output of analyze_business.
        scores:  Output of score_opportunity.
        learned_performance: Optional angle -> win-rate map.

    Returns:
        One of: "trust_gap" | "conversion_leak" | "time_leak" |
                "visibility_gap" | "general_growth"
    """
    has_website = bool(signals.get("has_website"))
    has_cta = bool(signals.get("has_cta"))
    has_booking = bool(signals.get("has_booking"))

    # Applicability in priority order — mirrors the prior first-match conditions.
    # general_growth is always applicable, so applicable[0] reproduces the old
    # deterministic result exactly.
    applicable: list[str] = []
    if scores.get("reputation", 0) >= 70 or not has_website:
        applicable.append("trust_gap")
    if has_website and not has_cta and not has_booking:
        applicable.append("conversion_leak")
    if scores.get("automation", 0) >= 70:
        applicable.append("time_leak")
    if scores.get("seo", 0) >= 70 or scores.get("ads", 0) >= 70:
        applicable.append("visibility_gap")
    applicable.append("general_growth")

    deterministic = applicable[0]
    if not learned_performance:
        return deterministic

    # Neutral prior for unsampled angles; deviate only on a strict win-rate
    # edge over the deterministic pick. max() breaks ties on iteration order
    # (= priority order), so an all-prior tie keeps the deterministic pick.
    _PRIOR = 0.5

    def _rate(angle: str) -> float:
        return learned_performance.get(angle, _PRIOR)

    best = max(applicable, key=_rate)
    if _rate(best) > _rate(deterministic):
        return best
    return deterministic
