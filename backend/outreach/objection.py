"""6-category objection detector for outbound sales conversations.

Pure-functional dispatch table. Inputs are conversation transcript snippets
(raw text); outputs are the detected objection category, a canned response
string the agent can deliver, and a secondary-product pivot suggestion.

Category priority order (highest to lowest):
    price -> not_interested -> already_have -> timing -> trust -> no_need

When intel containing a ``recommended_secondary_product`` key is supplied to
``handle_objection``, that value overrides the pivot table lookup, allowing
upstream deal-scoring or intelligence modules to influence the recommended
pivot without re-categorising the objection.

Public API
----------
detect_objection(transcript_text) -> str | None
handle_objection(transcript_text, intel) -> dict
"""
from __future__ import annotations

import re
import logging
from typing import Final

__all__: list[str] = ["detect_objection", "handle_objection",
                       "OBJECTION_KEYWORDS", "RESPONSE_TABLE", "PIVOT_TABLE"]

_LOG = logging.getLogger("samus.outreach.objection")

# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

OBJECTION_KEYWORDS: Final[dict[str, list[str]]] = {
    "price": [
        r"too expensive",
        r"can'?t afford",
        r"\bcheaper\b",
        r"out of (my )?budget",
        r"costs? too much",
        r"too much money",
    ],
    "not_interested": [
        r"not interested",
        r"don'?t need",
        r"\bno thanks?\b",
        r"\bpass\b",
        r"not for (us|me)",
    ],
    "already_have": [
        r"already (have|using|got)",
        r"we have (someone|a (guy|vendor))",
        r"got (someone|a vendor)",
    ],
    "timing": [
        r"call (me )?(back|later)",
        r"bad time",
        r"next (week|month|quarter)",
        r"busy right now",
        r"try (me )?next",
    ],
    "trust": [
        r"\bscam\b",
        r"\bspam\b",
        r"prove (it|that)",
        r"never heard (of you|about you)",
        r"how do (I|we) know",
    ],
    "no_need": [
        r"don'?t (see (the |a )?need|need that)",
        r"works fine",
        r"no (problem|issues)",
        r"satisfied with",
    ],
}

# Compile once at module load; stored as category -> list[Pattern]
_COMPILED: Final[dict[str, list[re.Pattern[str]]]] = {
    category: [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
    for category, patterns in OBJECTION_KEYWORDS.items()
}

# Priority order defines which category wins when multiple match.
_PRIORITY_ORDER: Final[tuple[str, ...]] = (
    "price",
    "not_interested",
    "already_have",
    "timing",
    "trust",
    "no_need",
)

# ---------------------------------------------------------------------------
# Canned response table
# ---------------------------------------------------------------------------

RESPONSE_TABLE: Final[dict[str, str]] = {
    "price": (
        "I hear you on budget — we have a lighter starter package that delivers "
        "most of the value at less than half the cost. Worth a quick look?"
    ),
    "not_interested": (
        "Totally fair — before you go, would a free audit that shows exactly where "
        "you're leaving revenue on the table be useful? No strings attached."
    ),
    "already_have": (
        "Great, having something in place is a solid start. We often find gaps even "
        "in well-run setups — a complementary assessment takes ten minutes and you "
        "keep every insight, regardless of what you decide."
    ),
    "timing": (
        "No problem at all — I'll reach back at a better moment. Is there a specific "
        "day and time next week that works better for you?"
    ),
    "trust": (
        "That's a fair concern, and I want to earn your confidence before asking for "
        "anything. I can send over case studies and references from clients in your "
        "industry right now — would that help?"
    ),
    "no_need": (
        "Glad things are running smoothly. The clients who told us the same thing "
        "were often surprised by a hidden 15-20% efficiency gap — a quick look costs "
        "nothing and you'll know for certain."
    ),
}

# ---------------------------------------------------------------------------
# Pivot table
# ---------------------------------------------------------------------------

PIVOT_TABLE: Final[dict[str, str]] = {
    "price": "starter_package",
    "not_interested": "free_seo_audit",
    "already_have": "complementary_assessment",
    "timing": "scheduled_callback",
    "trust": "reference_pack",
    "no_need": "efficiency_gap_analysis",
}

# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def detect_objection(transcript_text: str) -> str | None:
    """Return the first matching objection category or None.

    Iterates categories in priority order (price first, no_need last). The
    input is lowercased before matching so patterns do not need case flags,
    though the compiled patterns carry ``re.IGNORECASE`` as a belt-and-
    suspenders safeguard.

    Parameters
    ----------
    transcript_text:
        Raw conversation snippet. May be multi-sentence.

    Returns
    -------
    str | None
        One of the six category keys, or ``None`` when no pattern matches.
    """
    lowered = transcript_text.lower()
    for category in _PRIORITY_ORDER:
        for pattern in _COMPILED[category]:
            if pattern.search(lowered):
                _LOG.debug("objection detected category=%s pattern=%s",
                           category, pattern.pattern)
                return category
    return None


def handle_objection(
    transcript_text: str,
    intel: dict | None = None,
) -> dict:
    """Detect an objection and return a structured response dict.

    Parameters
    ----------
    transcript_text:
        Raw conversation snippet fed to ``detect_objection``.
    intel:
        Optional intelligence dict from deal-scoring or upstream modules.
        When it contains a ``"recommended_secondary_product"`` key, its value
        overrides the default pivot from ``PIVOT_TABLE``.

    Returns
    -------
    dict with keys:
        ``detected``  — category string or ``None``
        ``response``  — canned response string or ``None``
        ``pivot``     — secondary-product pivot string or ``None``
    """
    category = detect_objection(transcript_text)
    if category is None:
        _LOG.debug("handle_objection: no objection detected")
        return {"detected": None, "response": None, "pivot": None}

    response = RESPONSE_TABLE[category]
    pivot = PIVOT_TABLE[category]

    # Intel override: allow deal_scoring / intelligence layer to suggest a
    # more contextually appropriate secondary product.
    if intel is not None:
        override = intel.get("recommended_secondary_product")
        if override:
            _LOG.debug(
                "handle_objection: pivot overridden by intel "
                "category=%s original=%s override=%s",
                category, pivot, override,
            )
            pivot = override

    return {"detected": category, "response": response, "pivot": pivot}
