"""USD cost-estimate table for Apollo.io API endpoints (Guardrail G11).

Source-of-truth for the per-call cost estimate that
:mod:`backend.common.apollo_budget` charges to today's budget.

Apollo bills in *credits*, not dollars. Each endpoint consumes a fixed
number of credits per unit (per person, per match, ...) and the USD
value of one credit depends on the operator's Apollo plan. We expose
both halves so the operator can tune them via env without redeploying:

  ``SAMUS_APOLLO_USD_PER_CREDIT`` — default ``0.04`` (a conservative
  estimate based on Apollo's public-tier pricing as of 2026-05; lower
  on the basic plan, higher on free credits).

Credits per endpoint (conservative public-tier values as of 2026-05):

  ``people_search``         — 1 credit (per result page returned)
  ``email_unlock``          — 1 credit (per person matched / People Match)
  ``phone_unlock``          — 8 credits (per phone reveal — the expensive one)
  ``organization_search``   — 0 credits (free per Apollo docs; still logged)

Unknown endpoints fall back to ``SAMUS_APOLLO_USD_PER_CREDIT`` (assume
1 credit) and emit a warning so the operator notices a typo or a new
Apollo endpoint that hasn't been classified.

Sparse comments by design — the table itself is the spec.
"""
from __future__ import annotations

import logging
import os


_LOG = logging.getLogger("samus.common.apollo_pricing")


_DEFAULT_USD_PER_CREDIT = 0.04


# Endpoint -> credits per unit. Keys are the short names the budget
# layer uses; the HTTP-path mapping is the wrapper's concern, not ours.
ENDPOINT_CREDITS: dict[str, int] = {
    "people_search": 1,
    "email_unlock": 1,
    "phone_unlock": 8,
    "organization_search": 0,
}


def usd_per_credit() -> float:
    """Read the per-credit USD rate from env, with a safe default.

    Read every call (not cached) so the operator can flip the rate
    without restarting the workcell — same pattern as the LLM budget's
    cap env. Negative / non-numeric values fall back to the default.
    """
    raw = os.getenv("SAMUS_APOLLO_USD_PER_CREDIT", "").strip()
    if not raw:
        return _DEFAULT_USD_PER_CREDIT
    try:
        rate = float(raw)
    except ValueError:
        _LOG.warning(
            "SAMUS_APOLLO_USD_PER_CREDIT not numeric (%r); using default $%.4f",
            raw, _DEFAULT_USD_PER_CREDIT,
        )
        return _DEFAULT_USD_PER_CREDIT
    if rate < 0:
        _LOG.warning(
            "SAMUS_APOLLO_USD_PER_CREDIT negative (%s); using default $%.4f",
            rate, _DEFAULT_USD_PER_CREDIT,
        )
        return _DEFAULT_USD_PER_CREDIT
    return rate


def credits_for(endpoint: str) -> int:
    """Return credits-per-unit for a known endpoint, or 1 for unknown.

    Unknown endpoint logs a warning and assumes 1 credit per unit so we
    over-account rather than under-account (the cap is a safety net —
    favour fail-CLOSED on cost knowledge gaps).
    """
    if endpoint in ENDPOINT_CREDITS:
        return ENDPOINT_CREDITS[endpoint]
    _LOG.warning(
        "apollo_pricing: unknown endpoint %r — assuming 1 credit/unit",
        endpoint,
    )
    return 1


def estimate_call_cost(endpoint: str, units: int = 1) -> float:
    """USD estimate for ``units`` calls to ``endpoint``.

    ``units`` is the number of billable units the call will consume —
    e.g. one People Match unlocks one person (units=1); a People Search
    page returning 25 results is one billable page (units=1, NOT 25 —
    Apollo bills the page, not the row). The caller's wrapper decides.

    Negative ``units`` is clamped to 0 (caller bug — don't charge).
    """
    if units < 0:
        units = 0
    return round(credits_for(endpoint) * usd_per_credit() * units, 4)


__all__ = [
    "ENDPOINT_CREDITS",
    "credits_for",
    "estimate_call_cost",
    "usd_per_credit",
]
