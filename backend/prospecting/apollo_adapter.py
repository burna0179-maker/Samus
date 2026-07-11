"""Apollo.io enrichment — last-resort owner-contact lookup by domain.

The existing enrichment cascade (``enrich_from_page_with_fallback``) covers
homepage scrape + /contact + /about + Facebook About. For prospects that
still have no ``owner_email`` after all those stages — typically small
businesses whose websites omit owner info entirely — Apollo's people-search
gives us a third-party data source keyed on the company's web domain.

Gated by **two** dials so it never spends silently:

  * ``apollo_api_key`` (env ``APOLLO_API_KEY``) — empty key = adapter is a
    no-op that returns ``{}``. Paid Apollo plans return unmasked emails;
    free tier returns masked emails (we skip those).
  * **G11 daily $-cap** (``SAMUS_APOLLO_DAILY_BUDGET_USD``, default ``$5``)
    via :mod:`backend.common.apollo_budget`. Pre-flight ``assert_allows``
    fail-CLOSED before the HTTP request; post-flight ``record_spend`` on
    success. SAME store as :mod:`backend.outreach.apollo_source` so both
    Apollo paths (this enrichment fallback + the standalone run_campaign
    cold-mail flow) share ONE budget ledger.

The adapter is INTENTIONALLY narrow: people-search by org domain + title
(owner/ceo/president/founder), grab the top result, return its email if
unmasked. Anything more (org enrichment, sequences, lists) lives in the
standalone outreach.apollo_source module.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

_LOG = logging.getLogger("samus.prospecting.apollo_adapter")

# Endpoint short-name as the apollo_pricing / apollo_budget layer keys on
# (same key the standalone outreach.apollo_source uses for "people_search").
# The full HTTP path is the wrapper's concern, not the budget's.
_APOLLO_SEARCH_ENDPOINT = "people_search"
_APOLLO_PEOPLE_SEARCH = "https://api.apollo.io/v1/mixed_people/search"
_DEFAULT_TITLES = ("owner", "ceo", "president", "founder", "managing partner")

# Circuit breaker: after N consecutive auth failures, suppress all calls
# for COOLDOWN seconds.  Prevents credit drain when the key is invalid,
# the account is exhausted, or Apollo is having an outage.
_AUTH_FAIL_THRESHOLD = 3
_AUTH_FAIL_COOLDOWN_SEC = 3600.0
_auth_fail_count = 0
_auth_fail_suppressed_until = 0.0

# Apollo masks emails on the free tier with these tokens. Any email matching
# the marker (or whose local part starts with "email_not_unlocked") is treated
# as no-signal so we don't propagate a useless string downstream.
_MASKED_TOKENS = ("email_not_unlocked", "not_unlocked@", "domain.com")


def _domain_from_url(url: str) -> str:
    """Strip scheme / www / path. Returns the bare host."""
    import re  # noqa: PLC0415
    u = (url or "").strip().lower()
    if not u:
        return ""
    u = re.sub(r"^[a-z][a-z0-9+.-]*://", "", u)
    u = u.split("/", 1)[0].split("?", 1)[0]
    if u.startswith("www."):
        u = u[4:]
    return u.strip(".")


def _is_masked(email: str) -> bool:
    e = (email or "").lower()
    if not e:
        return True
    for tok in _MASKED_TOKENS:
        if tok in e:
            return True
    return False


def enrich_via_apollo(
    *,
    company_name: str = "",
    website_url: str = "",
    city: str = "",
    state: str = "",
) -> dict[str, str]:
    """Return owner_email / owner_name / owner_title from Apollo, or {} on no-signal.

    Fail-soft on every error: missing key, network fault, no results, masked
    email, daily-cap hit. The cascade caller treats ``{}`` as "Apollo had
    nothing useful, move on".
    """
    global _auth_fail_count, _auth_fail_suppressed_until  # noqa: PLW0603

    if os.getenv("SAMUS_APOLLO_DISABLED", "").strip() not in ("", "0", "false"):
        return {}

    if time.monotonic() < _auth_fail_suppressed_until:
        return {}

    from backend.common.config import get_settings  # local import to keep
    settings = get_settings()                       # cold-import cheap

    key = (getattr(settings, "apollo_api_key", "") or "").strip()
    if not key:
        return {}

    domain = _domain_from_url(website_url)
    if not domain and not company_name.strip():
        return {}

    # G11 pre-flight: shared daily $-cap with backend.outreach.apollo_source.
    # ApolloBudgetExceeded means today's spend would breach the cap, so we
    # skip cleanly (no fallback, no signal); next day's bucket resets.
    try:
        from backend.common.apollo_budget import (  # noqa: PLC0415
            ApolloBudgetExceeded,
            get_store as _budget_store,
        )
        from backend.common.apollo_pricing import (  # noqa: PLC0415
            estimate_call_cost,
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("apollo budget module unavailable: %s; skipping", exc)
        return {}
    try:
        estimated_usd = estimate_call_cost(_APOLLO_SEARCH_ENDPOINT, units=1)
        _budget_store().assert_allows(estimated_usd)
    except ApolloBudgetExceeded as exc:
        _LOG.info("apollo G11 cap reached (%s); skipping enrichment for %r",
                  exc, company_name or domain)
        return {}
    except Exception as exc:  # noqa: BLE001 — store fault is fail-OPEN
        _LOG.warning("apollo budget pre-flight degraded (%s); proceeding", exc)
        estimated_usd = 0.0

    try:
        import httpx  # noqa: PLC0415 — boundary import (codebase-standard
        #              client; the prospecting image ships httpx, NOT requests
        #              — matches backend.outreach.apollo_source).
    except Exception:  # noqa: BLE001
        _LOG.warning("httpx missing; apollo enrichment skipped")
        return {}

    payload: dict[str, Any] = {
        "page": 1,
        "per_page": 5,
        "person_titles": list(_DEFAULT_TITLES),
    }
    if domain:
        payload["q_organization_domains"] = domain
    elif company_name.strip():
        payload["q_organization_name"] = company_name.strip()
    # Locality is a soft filter — pass when known so Apollo prefers local
    # matches over an out-of-state same-named org.
    if city:
        payload["person_locations"] = [
            ", ".join([c for c in (city, state) if c]).strip(", ")
        ]

    try:
        # 8s timeout: longer than the homepage fetch (network call), shorter
        # than the per-prospect ceiling that the discovery loop budgets.
        resp = httpx.post(
            _APOLLO_PEOPLE_SEARCH,
            json=payload,
            headers={
                "Cache-Control": "no-cache",
                "Content-Type": "application/json",
                "X-Api-Key": key,
            },
            timeout=8.0,
        )
    except Exception as exc:  # noqa: BLE001 — network boundary
        _LOG.warning("apollo request failed company=%r domain=%r err=%s",
                     company_name, domain, exc)
        return {}

    # G11 post-flight: record the actual spend on the SHARED budget ledger
    # so both Apollo paths reconcile against one daily cap. Fail-OPEN per
    # the apollo_budget contract: a persistence fault never blocks the
    # caller (the credit is already consumed).
    try:
        _budget_store().record_spend(
            estimated_usd,
            endpoint=_APOLLO_SEARCH_ENDPOINT,
            prospect_id=None,
        )
    except ApolloBudgetExceeded:
        # A concurrent caller raced us past the cap between pre- and
        # post-flight. Surface to logs; the operator needs to know the
        # cap got breached.
        _LOG.warning("apollo G11 raced past cap on post-flight")
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("apollo budget post-flight degraded: %s", exc)

    if resp.status_code in (401, 403):
        _auth_fail_count += 1
        if _auth_fail_count >= _AUTH_FAIL_THRESHOLD:
            _auth_fail_suppressed_until = (
                time.monotonic() + _AUTH_FAIL_COOLDOWN_SEC
            )
            _LOG.error(
                "apollo circuit-breaker OPEN after %d consecutive auth "
                "failures — suppressing calls for %ds. Rotate the key or "
                "check account credits before the cooldown expires.",
                _auth_fail_count, int(_AUTH_FAIL_COOLDOWN_SEC),
            )
        else:
            _LOG.warning(
                "apollo auth/forbidden (%d) — body=%s [fail %d/%d]",
                resp.status_code, resp.text[:200],
                _auth_fail_count, _AUTH_FAIL_THRESHOLD,
            )
        return {}
    if resp.status_code >= 400:
        _LOG.warning("apollo http %d for company=%r domain=%r body=%s",
                     resp.status_code, company_name, domain, resp.text[:200])
        return {}

    _auth_fail_count = 0

    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return {}

    people = body.get("people") or body.get("contacts") or []
    if not isinstance(people, list) or not people:
        return {}

    # Pick the first person with an unmasked email + a plausibly-owner title.
    for person in people:
        if not isinstance(person, dict):
            continue
        email = str(person.get("email") or "").strip().lower()
        if _is_masked(email):
            continue
        name_parts = [
            str(person.get("first_name") or "").strip(),
            str(person.get("last_name") or "").strip(),
        ]
        name = " ".join(p for p in name_parts if p).strip()
        title = str(person.get("title") or "").strip()
        return {
            "owner_email": email,
            "owner_name": name,
            "owner_title": title,
        }

    # No unmasked email in the top results.
    return {}


__all__ = ["enrich_via_apollo"]
