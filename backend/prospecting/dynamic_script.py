"""Adaptive script generator — replaces the static-template callsheet with
intel-driven personalization.  Consumes prospecting.intelligence output
(pitch_angle + products + signals) to produce {opener, pitch, close,
voicemail} blocks.

Co-exists with backend.prospecting.callsheet (the static-template path);
not a replacement.  Caller chooses based on whether prospecting.intelligence
has been run on the prospect.
"""
from __future__ import annotations

from typing import Any, Final

__all__: list[str] = [
    "generate_script",
    "generate_script_with_pivot",
    "ANGLE_HOOKS",
    "PRODUCT_PITCH",
    "PRODUCT_CLOSE",
    "VOICEMAIL_TEMPLATES",
]

# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

ANGLE_HOOKS: Final[dict[str, str]] = {
    "trust_gap": (
        "I noticed [signal] — most companies your size handle this with a "
        "hardened presence layer..."
    ),
    "conversion_leak": (
        "I saw your site loads but the booking flow drops users at step 2 — "
        "that's leaving money on the table..."
    ),
    "time_leak": (
        "How much of your week goes to scheduling + follow-ups you could "
        "automate away?"
    ),
    "visibility_gap": (
        "When I searched for [keyword] in [region], you weren't on page 1 — "
        "that's traffic competitors are eating..."
    ),
    "general_growth": (
        "Quick question — what's the bottleneck slowing your next 12 months "
        "of growth?"
    ),
}

PRODUCT_PITCH: Final[dict[str, str]] = {
    "website_build": (
        "We'd rebuild your site as a conversion-optimized presence with the "
        "booking flow and trust signals already wired in — typically 2-3 week "
        "deployment."
    ),
    "seo_package": (
        "We'd run a 90-day local SEO push targeting [region] + your top 5 "
        "service keywords — measurable rank movement in 6 weeks."
    ),
    "ads_management": (
        "We'd take over your Google/Meta ads, run a 30-day audit + rebuild, "
        "then ongoing optimization — typical CPL down 40%."
    ),
    "workflow_automation": (
        "We'd map your top 3 daily workflows and deploy automations — "
        "schedules, follow-ups, internal handoffs."
    ),
    "reputation_management": (
        "We'd build a review-generation flow tied to job completion + handle "
        "response on the new reviews — typical rating lift in 60 days."
    ),
}

PRODUCT_CLOSE: Final[dict[str, str]] = {
    "website_build":        "Worth a 20-minute deep-dive on your current funnel?",
    "seo_package":          "Want the full audit before we talk further?",
    "ads_management":       "Open to a quick look at your current ad spend?",
    "workflow_automation":  "Open to mapping just one of those workflows so you can see the savings?",
    "reputation_management": "Want a quick look at your current review velocity vs. local competitors?",
}

VOICEMAIL_TEMPLATES: Final[dict[str, str]] = {
    "trust_gap": (
        "Hi, it's [NAME] from HustleForge. I pulled up {company}'s online "
        "presence and spotted a few trust signals worth fixing. Call me at "
        "[PHONE] — quick chat."
    ),
    "conversion_leak": (
        "Hey, [NAME] from HustleForge. I noticed {company}'s site has a drop "
        "in the booking flow — worth a five-minute look. Reach me at [PHONE]."
    ),
    "time_leak": (
        "Hi, [NAME] from HustleForge. I had a question for {company} about "
        "automating your scheduling — could save you hours each week. "
        "Call [PHONE]."
    ),
    "visibility_gap": (
        "Hey, [NAME] from HustleForge. {company} isn't showing on page one "
        "for your main keywords right now. I can show you exactly why — "
        "call [PHONE]."
    ),
    "general_growth": (
        "Hi, [NAME] from HustleForge. Calling for {company} about a growth "
        "opportunity I spotted — quick question when you get a minute. "
        "Reach me at [PHONE]."
    ),
}

# Default substitution values when intel signals are absent.
_DEFAULT_SIGNAL  = "your industry"
_DEFAULT_REGION  = "your area"
_DEFAULT_KEYWORD = "your services"

_ALL_ANGLES: Final[tuple[str, ...]] = (
    "trust_gap",
    "conversion_leak",
    "time_leak",
    "visibility_gap",
    "general_growth",
)

_DEFAULT_ANGLE   = "general_growth"
_DEFAULT_PRODUCT = "workflow_automation"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_str(mapping: dict[str, Any], key: str, default: str = "") -> str:
    """Safe string fetch — never raises, always returns str."""
    return str(mapping.get(key) or default)


def _extract_angle(intel: dict[str, Any]) -> str:
    """Return the pitch_angle from intel, defaulting to general_growth."""
    angle = _get_str(intel, "pitch_angle", _DEFAULT_ANGLE)
    return angle if angle in ANGLE_HOOKS else _DEFAULT_ANGLE


def _extract_products(intel: dict[str, Any]) -> tuple[str, str | None]:
    """Return (primary_product, secondary_product | None) from intel.

    intel may have a ``products`` sub-dict (as returned by
    ``intelligence.map_products``) or top-level ``primary_product`` /
    ``secondary_product`` keys.  Falls back gracefully to defaults.
    """
    products_block = intel.get("products")
    if isinstance(products_block, dict):
        primary   = _get_str(products_block, "primary",   _DEFAULT_PRODUCT)
        secondary = products_block.get("secondary") or None
    else:
        primary   = _get_str(intel, "primary_product",   _DEFAULT_PRODUCT)
        secondary = intel.get("secondary_product") or None

    if primary not in PRODUCT_PITCH:
        primary = _DEFAULT_PRODUCT

    if secondary is not None:
        secondary_str = str(secondary)
        if secondary_str not in PRODUCT_PITCH:
            secondary = None
        else:
            secondary = secondary_str

    return primary, secondary  # type: ignore[return-value]


def _extract_substitutions(intel: dict[str, Any]) -> tuple[str, str, str]:
    """Extract [signal], [region], [keyword] values from intel.signals."""
    signals_block = intel.get("signals")
    if not isinstance(signals_block, dict):
        signals_block = {}

    signal  = _get_str(signals_block, "signal",  _DEFAULT_SIGNAL)
    region  = _get_str(signals_block, "region",  _DEFAULT_REGION)
    keyword = _get_str(signals_block, "keyword", _DEFAULT_KEYWORD)

    # Extra fallback: use competitor_count context as signal text when present.
    if signal == _DEFAULT_SIGNAL:
        competitor_count = signals_block.get("competitor_count")
        if competitor_count and int(competitor_count) > 0:
            signal = f"{competitor_count} competitors in your space"

    return signal, region, keyword


def _apply_substitutions(
    text: str,
    signal: str,
    region: str,
    keyword: str,
) -> str:
    """Replace [signal], [region], [keyword] placeholders in text."""
    return (
        text
        .replace("[signal]",  signal)
        .replace("[region]",  region)
        .replace("[keyword]", keyword)
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def generate_script(company_name: str, intel: dict[str, Any]) -> dict[str, Any]:
    """Generate an adaptive call script from prospecting intelligence.

    Parameters
    ----------
    company_name:
        Display name of the prospect company.
    intel:
        Dict produced by ``intelligence.analyze_business`` / ``determine_pitch_angle``
        / ``map_products`` pipeline.  Tolerant of missing or partial keys.

    Returns
    -------
    dict with keys:
        opener, pitch, close, voicemail (str)
        pitch_angle, primary_product, secondary_product (str | None)
    """
    if not isinstance(intel, dict):
        intel = {}

    angle             = _extract_angle(intel)
    primary, secondary = _extract_products(intel)
    signal, region, keyword = _extract_substitutions(intel)

    hook       = _apply_substitutions(ANGLE_HOOKS[angle], signal, region, keyword)
    pitch_body = _apply_substitutions(PRODUCT_PITCH[primary], signal, region, keyword)
    close_line = PRODUCT_CLOSE[primary]
    voicemail  = VOICEMAIL_TEMPLATES[angle].replace("{company}", company_name)

    opener = f"Hi, this is [NAME] calling for {company_name}. {hook}"

    return {
        "opener":           opener,
        "pitch":            pitch_body,
        "close":            close_line,
        "voicemail":        voicemail,
        "pitch_angle":      angle,
        "primary_product":  primary,
        "secondary_product": secondary,
    }


def generate_script_with_pivot(
    company_name: str,
    intel: dict[str, Any],
) -> dict[str, Any]:
    """Generate an adaptive call script and include a secondary-product pivot.

    Identical to ``generate_script`` but also adds a ``pivot`` key containing
    the pitch + close for the secondary product when one is present in intel.
    When no secondary product is available, ``pivot`` is ``None``.

    Parameters
    ----------
    company_name:
        Display name of the prospect company.
    intel:
        Same shape as ``generate_script``.

    Returns
    -------
    dict — same as ``generate_script`` plus key:
        pivot (str | None) — "<secondary_pitch> — <secondary_close>"
    """
    result = generate_script(company_name, intel)

    secondary = result["secondary_product"]
    if secondary and secondary in PRODUCT_PITCH:
        _, _, _, keyword = (
            _extract_substitutions(intel) + ("",)
        )[:4]
        _signal, _region, _keyword = _extract_substitutions(intel)
        pivot_pitch = _apply_substitutions(
            PRODUCT_PITCH[secondary], _signal, _region, _keyword
        )
        pivot_close = PRODUCT_CLOSE[secondary]
        result["pivot"] = f"{pivot_pitch} — {pivot_close}"
    else:
        result["pivot"] = None

    return result
