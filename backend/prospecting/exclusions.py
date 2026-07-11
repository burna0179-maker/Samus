"""Prospect exclusions — government offices + an operator denylist.

Two filters, both applied at discovery time in
:func:`backend.prospecting.place_search.discover_for_zipcode`:

1. **Government / public-sector offices** — identified by Google Places
   ``types``. A city hall or police department is never a cold-call target.
2. **An operator denylist** — specific orgs that look like fair targets by
   type but are too institutional to pursue (too much sales resistance).
   Google classifies them as ordinary businesses, so the type filter misses
   them — e.g. Ampla Health is tagged ``medical_clinic``, not government.
   Grow the denylist by editing ``EXCLUDED_DOMAINS`` /
   ``EXCLUDED_NAME_SUBSTRINGS`` below.

``exclusion_reason()`` is the single check; it returns a short reason string
(for logging) or ``""`` to keep the prospect.
"""

from __future__ import annotations

from urllib.parse import urlparse

# Google Places (New) `types` that mark a place as a government / public-sector
# office. Any overlap with a place's types => exclude.
_GOVERNMENT_TYPES: frozenset[str] = frozenset(
    {
        "local_government_office",
        "city_hall",
        "courthouse",
        "embassy",
        "fire_station",
        "police",
        "post_office",
        "government_office",  # legacy Places type name — kept for safety
    }
)

# --- operator denylist — edit these two sets to grow it --------------------
# Website domains to exclude. Use the bare registrable domain, lowercase, no
# scheme / no www — subdomains are matched automatically.
EXCLUDED_DOMAINS: set[str] = {
    # Ampla Health — a community health network (FQHC). Too institutional to
    # sell; Google tags it medical_clinic, so the government-type filter
    # above does not catch it. Added 2026-05-20.
    "amplahealth.org",
}

# Company-name substrings to exclude (lowercase). Use sparingly: a substring
# match also drops any legitimate prospect whose name happens to contain it.
EXCLUDED_NAME_SUBSTRINGS: set[str] = set()
# ---------------------------------------------------------------------------


def _host_of(url: str) -> str:
    """Lowercase hostname of a URL; '' when there's no URL / it won't parse."""
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else f"http://{url}")
    return (parsed.hostname or "").lower()


def exclusion_reason(
    *,
    place_types: str,
    website_url: str,
    company_name: str,
) -> str:
    """Return a short reason this prospect should be excluded, or '' to keep it.

    ``place_types`` is the comma-joined Google Places types string — i.e.
    ``ProspectRecord.business_categories``.
    """
    types = {t.strip().lower() for t in (place_types or "").split(",") if t.strip()}
    if types & _GOVERNMENT_TYPES:
        return "government_office"

    host = _host_of(website_url)
    if host:
        for domain in EXCLUDED_DOMAINS:
            if host == domain or host.endswith("." + domain):
                return f"denylist_domain:{domain}"

    name = (company_name or "").lower()
    for substring in EXCLUDED_NAME_SUBSTRINGS:
        if substring and substring in name:
            return f"denylist_name:{substring}"

    return ""
