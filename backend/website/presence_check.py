"""Pre-build web-presence verification — cost + credibility insurance.

Before building (or pitching) a "no website" site, RE-CHECK Google Places (the
authoritative website field) for the business. A hit means it HAS a site: the
prospecting crawler's broken/no_website flag was a FALSE POSITIVE (a bot-blocked
working site, or a listing found under a slightly different query). Building — or
pitching "you have no website" — to such a business wastes money AND torches
credibility.

  Verified live 2026-07-02: the "USA Auto Sale" prospect = United Auto Sales,
  unitedautosalesca.com (full for-sale inventory). It had been tagged
  "no working website". This gate catches exactly that.

The verdict also carries the FRESH Places business data (name, address, phone,
hours, services/types, rating, reviews, editorial description) so a demo can be
tailored with the prospect's REAL info — they recognise it as theirs, which is
what turns a demo into a sale.

Read-only Google Places (``place_search.search_text``). Fail-soft: any check
failure returns ``buildable=True`` (best-effort insurance must never hard-block
the pipeline) with the reason recorded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

_LOG = logging.getLogger("samus.website.presence_check")


@dataclass
class PresenceVerdict:
    buildable: bool
    reason: str
    website: str = ""
    matched_name: str = ""
    business: dict[str, Any] = field(default_factory=dict)


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _name_matches(place_name: str, company_name: str) -> bool:
    """Loose match: normalized equality or containment either way. Guards against
    building for the wrong 'top result' when the names don't actually line up."""
    a, b = _norm(place_name), _norm(company_name)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _business_from_place(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": (p.get("displayName") or {}).get("text", ""),
        "address": p.get("formattedAddress", ""),
        "phone": p.get("nationalPhoneNumber", ""),
        "rating": p.get("rating"),
        "review_count": p.get("userRatingCount"),
        "hours": (p.get("regularOpeningHours") or {}).get("weekdayDescriptions", []),
        "types": p.get("types", []),
        "primary_type": (p.get("primaryTypeDisplayName") or {}).get("text", ""),
        "description": (p.get("editorialSummary") or {}).get("text", ""),
        "website": p.get("websiteUri", "") or "",
    }


# Aggregators / directories / socials — a result on one of these is NOT the
# business's own website (Places already covers reviews). Only a result OUTSIDE
# this set counts as "they have a site".
_DIRECTORY_HOSTS = frozenset(
    {
        "yelp.com",
        "facebook.com",
        "instagram.com",
        "linkedin.com",
        "yellowpages.com",
        "nextdoor.com",
        "manta.com",
        "bbb.org",
        "dnb.com",
        "dandb.com",
        "bizapedia.com",
        "cylex.us.com",
        "thumbtack.com",
        "birdeye.com",
        "justia.com",
        "mapquest.com",
        "google.com",
        "bing.com",
        "twitter.com",
        "x.com",
        "youtube.com",
        "tiktok.com",
        "zoominfo.com",
        "rocketreach.co",
        "buzzfile.com",
        "chamberofcommerce.com",
        "angi.com",
        "houzz.com",
        "tripadvisor.com",
        "foursquare.com",
        "opencorporates.com",
        "bizprofile.net",
        "yahoo.com",
        "wikipedia.org",
        "reddit.com",
        "indeed.com",
        "glassdoor.com",
        "crunchbase.com",
        "apple.com",
        "amazon.com",
        "pinterest.com",
        "superpages.com",
        "citysearch.com",
        "taxbuzz.com",
        "precisionplanting.com",
        "virtualvalley.io",
        "beautihost.com",
        "dexknows.com",
        "chamberofcommerce.com",
    }
)


def _host(url: str) -> str:
    from urllib.parse import urlparse

    try:
        h = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""
    return h[4:] if h.startswith("www.") else h


def _is_directory(url: str) -> bool:
    h = _host(url)
    if not h:
        return True
    return any(h == d or h.endswith("." + d) for d in _DIRECTORY_HOSTS)


def web_search_finds_site(
    company_name: str,
    city: str = "",
    *,
    api_key: str | None = None,
    cse_id: str | None = None,
    http_client: Any = None,
    timeout: float = 30.0,
) -> str:
    """Find the business's OWN website via web search — closing the gap where a
    real site isn't linked in the Google Business Profile (Places misses it).
    Returns the site URL or ''. Two autonomous backends, tried in order:

      1. Google Programmable Search (``google_cse_*``) — raw results, first
         non-directory hit. NOTE: the Custom Search JSON API is CLOSED TO NEW
         CUSTOMERS (Google, grandfathered existing users to 2027-01-01), so it
         403s on any project created after the cutoff — leave ``google_cse_*``
         UNSET on such projects. Kept for pre-existing/old projects that still
         have access; the failure path below falls through to Gemini regardless.
      2. Gemini grounded search (``gemini_api_key``, already provisioned) — the
         EFFECTIVE PRIMARY here and Google's own recommended CSE replacement
         (Grounding with Google Search). Gemini runs the Google search and names
         the site. Catches unique-name sites (dynamicagservices.com) but is
         conservative on name-collisions. No setup — cross-references TODAY.

    With neither credential, returns '' (Places-only). Fail-soft."""
    from backend.common.config import get_settings

    s = get_settings()
    key = (api_key if api_key is not None else getattr(s, "google_cse_api_key", "")) or ""
    cx = (cse_id if cse_id is not None else getattr(s, "google_cse_id", "")) or ""

    if key and cx:
        site = _cse_finds_site(
            company_name, city, api_key=key, cse_id=cx, http_client=http_client, timeout=timeout
        )
        if site:
            return site
        if site == "":
            # CSE ran successfully and found nothing owned — confident "no site".
            return ""
        # site is None -> the CSE call itself FAILED (403 not-enabled / quota /
        # network). A failed lookup is NOT evidence of "no site"; fall through to
        # the Gemini backend so a real site isn't missed while CSE is unavailable.

    gkey = (getattr(s, "gemini_api_key", "") or "").strip()
    if gkey:
        return _gemini_finds_site(
            company_name, city, api_key=gkey, http_client=http_client, timeout=timeout
        )
    return ""


def _cse_finds_site(
    company_name, city, *, api_key, cse_id, http_client=None, timeout=30.0
) -> str | None:
    """Return the business's own site URL (found), ``""`` (search RAN, nothing
    owned in results), or ``None`` (the CSE call itself FAILED — 403/quota/
    network — so the caller must fall back rather than conclude "no site")."""
    try:
        import httpx

        q = " ".join(x for x in (company_name, city) if x).strip()
        params = {"key": api_key, "cx": cse_id, "q": q, "num": 10}
        own = http_client is None
        client = http_client or httpx.Client(timeout=timeout)
        try:
            r = client.get("https://www.googleapis.com/customsearch/v1", params=params)
            r.raise_for_status()
            items = (r.json() or {}).get("items") or []
        finally:
            if own:
                client.close()
        for item in items:
            link = (item.get("link") or "").strip()
            if link and not _is_directory(link):
                return link
        return ""
    except Exception as exc:  # noqa: BLE001 — best-effort, never blocks
        _LOG.warning("CSE presence search failed for %r: %s", company_name, exc)
        return None


def _gemini_finds_site(
    company_name, city, *, api_key, http_client=None, timeout=30.0, model="gemini-2.5-flash"
) -> str:
    """Gemini grounded Google search: reply is the bare site URL or NONE. Uses
    the already-provisioned Gemini key (no setup). Best-effort/fail-soft."""
    try:
        import re
        import httpx

        prompt = (
            f"Search Google for: {company_name}"
            f"{f' {city} California' if city else ''} website. "
            f"If the search results include the business's OWN homepage (a real "
            f"company website, NOT a Yelp/Facebook/Yellowpages/directory/social/"
            f"map listing), reply with ONLY that bare URL. Reply NONE only if the "
            f"search returns no company website for them. Prefer returning a URL "
            f"you found over NONE."
        )
        body = {"contents": [{"parts": [{"text": prompt}]}], "tools": [{"google_search": {}}]}
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        own = http_client is None
        client = http_client or httpx.Client(timeout=timeout)
        try:
            r = client.post(url, headers={"x-goog-api-key": api_key}, json=body)
            r.raise_for_status()
            data = r.json() or {}
        finally:
            if own:
                client.close()
        cand = (data.get("candidates") or [{}])[0]
        text = "".join(p.get("text", "") for p in (cand.get("content", {}) or {}).get("parts", []))
        m = re.search(r"https?://[^\s)>\]\"']+", text)
        if not m:
            return ""
        site = m.group(0).rstrip(".,);]")
        return "" if _is_directory(site) else site
    except Exception as exc:  # noqa: BLE001 — best-effort, never blocks
        _LOG.warning("gemini presence search failed for %r: %s", company_name, exc)
        return ""


def verify_presence(
    company_name: str,
    *,
    city: str = "",
    state: str = "",
    existing_website: str = "",
    known_phone: str = "",
    known_address: str = "",
    api_key: str | None = None,
) -> PresenceVerdict:
    """Return whether a demo should be BUILT for this business (True == no site
    found; safe to build) plus the fresh Places data for tailoring.

    ``known_phone`` / ``known_address`` (the prospect's own CRM contact data) arm
    the phone/address DEEP-VERIFICATION layer, which disambiguates generic names
    a plain search can't. Absent, the Places listing's phone/address is used."""
    if (existing_website or "").strip():
        return PresenceVerdict(
            False,
            f"record already carries a website ({existing_website.strip()})",
            website=existing_website.strip(),
        )

    try:
        from backend.prospecting import place_search

        query = " ".join(x for x in (company_name, city, state) if x).strip()
        if not query:
            return PresenceVerdict(True, "no query terms — cannot verify; proceeding")
        resp = place_search.search_text(query, max_results=10, api_key=api_key)
        places = resp.get("places") or []

        matched = next(
            (
                p
                for p in places
                if _name_matches((p.get("displayName") or {}).get("text", ""), company_name)
            ),
            None,
        )
        # Places website field is authoritative WHEN present (name-matched, so an
        # unrelated top result never wrongly blocks).
        biz: dict[str, Any] = {}
        if matched is not None:
            biz = _business_from_place(matched)
            if biz["website"]:
                return PresenceVerdict(
                    False,
                    f"Places lists a live website ({biz['website']})",
                    website=biz["website"],
                    matched_name=biz["name"],
                    business=biz,
                )
        elif places:
            biz = _business_from_place(places[0])

        # Places has NO linked site — but many real sites aren't in the Google
        # Business Profile (dynamicagservices.com was missed by Places, found by a
        # plain search). Two-layer resolution:
        #   (1) web_search_finds_site — Gemini's CONSERVATIVE verdict: names a site
        #       only when confident, ABSTAINS on ambiguous generic names. Safe but
        #       low-confidence (unverified).
        #   (2) deep_verify_site — HIGH-confidence: fetches candidate domains (the
        #       web_search hit + grounded-search sources over the name AND the
        #       phone) and confirms ownership by matching the prospect's OWN phone/
        #       address on the page. This disambiguates same-name companies in
        #       BOTH directions (only a contact match attributes a site).
        site = web_search_finds_site(company_name, city)

        from backend.common.config import get_settings

        s = get_settings()
        anchor_phone = (known_phone or biz.get("phone", "")).strip()
        anchor_address = (known_address or biz.get("address", "")).strip()
        if getattr(s, "presence_deep_verify_enabled", True) and (anchor_phone or anchor_address):
            from backend.website.deep_verify import deep_verify_site

            dv = deep_verify_site(
                company_name,
                city=city,
                state=state,
                known_phone=anchor_phone,
                known_address=anchor_address,
                api_key=api_key,
                seed_url=site,
            )
            if dv.found:  # contact-verified — the site is provably theirs
                return PresenceVerdict(
                    False,
                    f"deep-verify [{dv.matched_on}] {dv.reason}",
                    website=dv.url,
                    matched_name=biz.get("name", ""),
                    business=biz,
                )

        # No contact match — fall back to the conservative web-search verdict
        # (unverified, so it abstains on ambiguous names rather than attribute an
        # impostor). A named site still suppresses a false "you have no website".
        if site:
            return PresenceVerdict(
                False,
                f"web search found a site ({site})",
                website=site,
                matched_name=biz.get("name", ""),
                business=biz,
            )
        return PresenceVerdict(
            True,
            "no site in Places or web search — buildable",
            matched_name=biz.get("name", ""),
            business=biz,
        )

    except Exception as exc:  # noqa: BLE001 — insurance is best-effort, never blocks
        _LOG.warning("presence check failed for %r: %s", company_name, exc)
        return PresenceVerdict(True, f"presence check failed ({type(exc).__name__}); proceeding")
