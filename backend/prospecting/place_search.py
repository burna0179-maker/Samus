"""Google Places API (New) textSearch client for prospecting.

Reads ``settings.google_places_api_key`` (bound from GCP Secret Manager on
Cloud Run; from ``.env.prod`` locally — see project memory:
[[project-samus-cloud-run-multi-target]]).

Endpoint: ``POST https://places.googleapis.com/v1/places:searchText``
Docs:     https://developers.google.com/maps/documentation/places/web-service/text-search
"""
from __future__ import annotations

import logging
from typing import Any, Iterable
from uuid import uuid4

import httpx

from backend.common.settings import get_settings

from .exclusions import exclusion_reason
from .models import ProspectRecord

_LOG = logging.getLogger("samus.prospecting.place_search")

_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

# NOTE on billing: rating/userRatingCount/regularOpeningHours already put each
# request in the Text Search (New) Enterprise SKU. `editorialSummary` is an
# Atmosphere-tier field, so requesting it bumps the per-request SKU one tier.
# Accepted trade-off: it is the fallback "what this business does" line behind
# the free homepage scrape — see backend.prospecting.enrichment._extract_description.
_FIELD_MASK = ",".join((
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.addressComponents",
    "places.nationalPhoneNumber",
    "places.websiteUri",
    "places.types",
    "places.primaryTypeDisplayName",
    "places.rating",
    "places.userRatingCount",
    "places.regularOpeningHours.weekdayDescriptions",
    "places.editorialSummary",
    "nextPageToken",
))

_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)


class PlacesError(RuntimeError):
    """Places API call failed (missing key, auth, quota, transport)."""


def _require_api_key() -> str:
    key = (get_settings().google_places_api_key or "").strip()
    if not key:
        raise PlacesError(
            "GOOGLE_PLACES_API_KEY is not set. On Cloud Run, bind it from GCP "
            "Secret Manager via --set-secrets; locally, set it in .env.prod.",
        )
    return key


def search_text(
    text_query: str,
    *,
    max_results: int = 20,
    page_token: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Raw Places API textSearch call. Returns the parsed JSON response.

    ``max_results`` is clamped to [1, 20] per Google's per-request cap. For more,
    follow ``nextPageToken`` — see ``discover_for_zipcode`` which paginates.
    """
    api_key = api_key or _require_api_key()
    body: dict[str, Any] = {
        "textQuery": text_query,
        "maxResultCount": max(1, min(20, max_results)),
        "languageCode": "en",
        "regionCode": "us",
    }
    if page_token:
        body["pageToken"] = page_token
    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": _FIELD_MASK,
    }
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.post(_SEARCH_URL, json=body, headers=headers)
    if resp.status_code != 200:
        raise PlacesError(
            f"Places searchText returned {resp.status_code}: {resp.text[:512]}",
        )
    return resp.json()


def _extract_address_part(
    components: list[dict[str, Any]],
    wanted_type: str,
    *,
    short: bool = False,
) -> str:
    for component in components or []:
        if wanted_type in (component.get("types") or []):
            return component.get("shortText" if short else "longText", "") or ""
    return ""


def _hours_joined(opening: dict[str, Any] | None) -> str:
    if not opening:
        return ""
    return " | ".join(opening.get("weekdayDescriptions") or [])


def _prospect_id(place_id: str) -> str:
    return f"pr_{place_id[:20]}" if place_id else f"pr_{uuid4().hex[:20]}"


def _account_id(place_id: str) -> str:
    return f"acct_{place_id[:20]}" if place_id else f"acct_{uuid4().hex[:20]}"


def place_to_prospect(
    place: dict[str, Any],
    *,
    zipcode: str,
    industry: str,
) -> ProspectRecord:
    """Map one Places result to a ProspectRecord (discovery-stage fields only)."""
    place_id = place.get("id", "") or ""
    display = (place.get("displayName") or {}).get("text", "") or ""
    components = place.get("addressComponents") or []

    city = _extract_address_part(components, "locality")
    state = _extract_address_part(components, "administrative_area_level_1", short=True)
    found_zip = _extract_address_part(components, "postal_code") or zipcode

    rating = place.get("rating")
    rating_str = str(rating) if rating is not None else ""
    review_count = place.get("userRatingCount")
    review_count_str = str(review_count) if review_count is not None else ""

    # Google's curated one-line summary. Fallback only — the homepage scrape
    # in enrichment._extract_description takes precedence when it yields one.
    editorial = place.get("editorialSummary") or {}
    business_description = (editorial.get("text") or "").strip()

    return ProspectRecord(
        prospect_id=_prospect_id(place_id),
        account_id=_account_id(place_id),
        company_name=display,
        phone=place.get("nationalPhoneNumber", "") or "",
        website_url=place.get("websiteUri", "") or "",
        city=city,
        state=state,
        zipcode=found_zip,
        industry=industry,
        review_rating=rating_str,
        review_count=review_count_str,
        business_description=business_description,
        business_hours=_hours_joined(place.get("regularOpeningHours")),
        business_categories=", ".join(place.get("types") or []),
    )


def discover_for_zipcode(
    *,
    zipcode: str,
    industries: Iterable[str],
    max_results_per_zip: int = 25,
    must_have_website: bool = True,
) -> list[ProspectRecord]:
    """Search Places for one zipcode across each industry term. Dedupe by place_id.

    Iterates industries in order; for each, paginates until either the per-zip
    cap is hit or the response yields no ``nextPageToken``.
    """
    seen: set[str] = set()
    out: list[ProspectRecord] = []
    industries_list = list(industries) or [""]
    for industry in industries_list:
        if len(out) >= max_results_per_zip:
            break
        query = f"{industry} in {zipcode}".strip() if industry else zipcode
        page_token: str | None = None
        while len(out) < max_results_per_zip:
            resp = search_text(query, max_results=20, page_token=page_token)
            for place in resp.get("places") or []:
                if len(out) >= max_results_per_zip:
                    break
                place_id = place.get("id", "")
                if not place_id or place_id in seen:
                    continue
                prospect = place_to_prospect(place, zipcode=zipcode, industry=industry)
                if must_have_website and not prospect.website_url:
                    continue
                # Drop government offices + operator-denylisted orgs — never
                # good cold-call targets (see backend.prospecting.exclusions).
                excluded = exclusion_reason(
                    place_types=prospect.business_categories,
                    website_url=prospect.website_url,
                    company_name=prospect.company_name,
                )
                if excluded:
                    _LOG.info(
                        "discover_for_zipcode excluded prospect",
                        extra={"company": prospect.company_name,
                               "reason": excluded, "zipcode": zipcode},
                    )
                    continue
                seen.add(place_id)
                out.append(prospect)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    _LOG.info(
        "discover_for_zipcode complete",
        extra={"zipcode": zipcode, "industries": industries_list, "count": len(out)},
    )
    return out
