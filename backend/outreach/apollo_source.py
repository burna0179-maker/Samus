"""Apollo.io people-source adapter for the outreach workcell.

Pulls outbound-email prospects from Apollo (whose dataset is LinkedIn-derived)
rather than from the Google-Places call list. The call list never carried a
named person or a deliverable email; Apollo's People Search returns the person,
their LinkedIn URL, and a business email with a deliverability status.

Two Apollo endpoints are used:

  POST /v1/mixed_people/search   discover people by title / industry / location.
                                 In search results the email is frequently
                                 masked ("email_not_unlocked@domain.com") until
                                 the contact is unlocked.
  POST /v1/people/match          reveal (unlock) one person's email. Consumes an
                                 Apollo credit, so the caller unlocks only the
                                 people it actually intends to email, capped by
                                 the campaign's max-send.

Auth: APOLLO_API_KEY env var (seeded from DPAPI Scope=Samus by the host
wrapper Run-OutreachDaily.ps1). Read from the environment directly — NOT via
backend.common.settings — so this module stays decoupled from the Settings
object and testable with a plain monkeypatch.

LIVE-WIRING NOTE: the endpoint paths + response field names below follow
Apollo's public v1 API as of build time. Verify against current Apollo docs
when the live key is seeded — the parser is deliberately defensive (every
field defaults to "") so a minor schema drift degrades a single field rather
than dropping the whole contact.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from backend.common.apollo_budget import (
    ApolloBudgetExceeded,
    get_store as _apollo_budget_store,
)
from backend.common.apollo_pricing import estimate_call_cost
from backend.common.codex import (
    CodexUnavailable,
    ProposedAction,
    check_action,
)

_LOG = logging.getLogger("samus.outreach.apollo_source")

_API_BASE = "https://api.apollo.io"
_SEARCH_PATH = "/v1/mixed_people/search"
_MATCH_PATH = "/v1/people/match"
_DEFAULT_TIMEOUT = 30.0

# Apollo email_status values that we treat as safe to send to. "verified" is
# Apollo's confirmed-deliverable status. "guessed"/"unverified"/"unavailable"
# are excluded by default (campaign.require_verified_email) to protect SES
# sender reputation.
SENDABLE_EMAIL_STATUSES = frozenset({"verified"})

# Apollo masks locked emails with this sentinel local-part.
_LOCKED_EMAIL_MARKER = "email_not_unlocked"


class ApolloError(Exception):
    """Raised when the Apollo API key is missing or the API call fails."""


class ApolloContact(BaseModel):
    """One person returned by Apollo, normalised to the fields outreach needs."""

    model_config = ConfigDict(extra="ignore")

    person_id: str = ""
    first_name: str = ""
    name: str = ""
    title: str = ""
    email: str = ""
    email_status: str = ""        # apollo: verified / guessed / unverified / unavailable
    linkedin_url: str = ""
    company: str = ""
    company_domain: str = ""
    industry: str = ""
    city: str = ""
    state: str = ""
    country: str = ""
    phone: str = ""
    # G8 — populated by the outreach pre-flight warmth gate from the
    # highest-confidence LegitimacySignal kind ("public_registry",
    # "chamber_roster", "prior_inbound", "rfp", "open_job_listing").
    # Empty means no warmth signal was found; such contacts are diverted
    # to needs_warm_path and never reach build_messages.
    legitimacy_signal: str = ""

    @property
    def email_locked(self) -> bool:
        """True when Apollo returned a masked placeholder instead of a real address."""
        return _LOCKED_EMAIL_MARKER in (self.email or "").lower()

    @property
    def sendable(self) -> bool:
        """A deliverable, unmasked, verified-status address."""
        return (
            bool(self.email)
            and not self.email_locked
            and self.email_status in SENDABLE_EMAIL_STATUSES
        )


def _api_key() -> str:
    key = os.getenv("APOLLO_API_KEY", "").strip()
    if not key:
        raise ApolloError(
            "APOLLO_API_KEY is not set. Seed it into the Samus DPAPI store "
            "(Set-HfSecret -Scope Samus -Name ApolloApiKey) — the host wrapper "
            "Run-OutreachDaily.ps1 exports it into the environment for this run."
        )
    return key


def _client(client: httpx.Client | None = None) -> tuple[httpx.Client, bool]:
    """Return (client, owned). When ``client`` is injected (tests), owned=False."""
    if client is not None:
        return client, False
    owned = httpx.Client(
        base_url=_API_BASE,
        timeout=_DEFAULT_TIMEOUT,
        headers={
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "X-Api-Key": _api_key(),
        },
    )
    return owned, True


def _g11_preflight(endpoint: str, *, units: int = 1, prospect_id: str | None = None) -> float:
    """G11 + Codex audit gate before every Apollo HTTP call.

    Returns the USD estimate that the caller must record on success via
    :func:`_g11_postflight`. Raises:

      * :class:`ApolloBudgetExceeded` if the spend would breach today's cap.
      * Re-raises ``CodexUnavailable`` from check_action (fail-closed by Codex contract).

    The codex hook is action_kind="other" with target="apollo_call" — it's
    audit-only for now (no VR-G11 blocking rule yet); the parent reviews
    whether to add a blocking rule when consolidating.
    """
    estimated_usd = estimate_call_cost(endpoint, units=units)
    # Codex audit — surfaces every Apollo call in the validator stream so
    # operator tools can see cost-incurring activity even pre-VR-G11.
    try:
        check_action(ProposedAction(
            service="outreach",
            capability="apollo_source",
            action_kind="other",
            payload={
                "target": "apollo_call",
                "endpoint": endpoint,
                "estimated_usd": estimated_usd,
                "prospect_id": prospect_id,
            },
            proposed_by="outreach.apollo_source",
            correlation_id=None,
        ))
    except CodexUnavailable:
        # Codex contract is fail-closed; re-raise so the caller doesn't
        # silently bypass the audit chain.
        raise
    # Budget gate — fail-CLOSED on cap, fail-OPEN on persistence (raises only
    # when the daily total would breach the cap).
    _apollo_budget_store().assert_allows(estimated_usd)
    return estimated_usd


def _g11_postflight(
    endpoint: str,
    estimated_usd: float,
    *,
    prospect_id: str | None = None,
) -> None:
    """Record the actual spend after a successful Apollo call.

    Apollo public endpoints don't return a billed-cost header, so we
    charge the pre-flight estimate. If a future Apollo response carries
    a usage block, ``actual_cost`` would replace ``estimated_usd`` here.
    """
    try:
        _apollo_budget_store().record_spend(
            estimated_usd,
            endpoint=endpoint,
            prospect_id=prospect_id,
        )
    except ApolloBudgetExceeded:
        # A concurrent caller raced us past the cap between pre-flight
        # and post-flight. Surface it — the call already consumed the
        # credit, the operator needs to see the breach.
        raise


def _parse_person(raw: dict[str, Any]) -> ApolloContact:
    """Map one Apollo person object to an ApolloContact, defensively."""
    org = raw.get("organization") or {}
    return ApolloContact(
        person_id=str(raw.get("id") or ""),
        first_name=str(raw.get("first_name") or ""),
        name=str(raw.get("name") or "").strip()
        or " ".join(p for p in (raw.get("first_name"), raw.get("last_name")) if p).strip(),
        title=str(raw.get("title") or ""),
        email=str(raw.get("email") or ""),
        email_status=str(raw.get("email_status") or ""),
        linkedin_url=str(raw.get("linkedin_url") or ""),
        company=str(org.get("name") or raw.get("organization_name") or ""),
        company_domain=str(org.get("primary_domain") or raw.get("organization_domain") or ""),
        industry=str(org.get("industry") or raw.get("industry") or ""),
        city=str(raw.get("city") or ""),
        state=str(raw.get("state") or ""),
        country=str(raw.get("country") or ""),
        phone=str(raw.get("phone") or (org.get("phone") if isinstance(org, dict) else "") or ""),
    )


def search_people(
    *,
    titles: list[str] | None = None,
    industries: list[str] | None = None,
    locations: list[str] | None = None,
    per_page: int = 25,
    page: int = 1,
    client: httpx.Client | None = None,
) -> list[ApolloContact]:
    """Discover people via Apollo People Search.

    ``locations`` are person locations ("Sacramento, California, US" etc.).
    Returns the parsed people for one page; the caller paginates by bumping
    ``page``. Raises :class:`ApolloError` on HTTP / transport failure.
    """
    body: dict[str, Any] = {
        "page": max(1, page),
        "per_page": max(1, min(100, per_page)),
    }
    if titles:
        body["person_titles"] = titles
    if industries:
        # Apollo accepts free-text industry keyword tags here.
        body["q_organization_keyword_tags"] = industries
    if locations:
        body["person_locations"] = locations

    # G11 pre-flight: budget + codex audit BEFORE the HTTP call.
    # Apollo bills People Search per page returned, units=1 per call.
    estimated_usd = _g11_preflight("people_search", units=1)

    cli, owned = _client(client)
    try:
        resp = cli.post(_SEARCH_PATH, json=body)
        if resp.status_code >= 400:
            raise ApolloError(f"apollo search HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
    except httpx.HTTPError as exc:
        raise ApolloError(f"apollo search transport error: {exc}") from exc
    finally:
        if owned:
            cli.close()

    # G11 post-flight: record actual spend (estimate stands in for billed cost).
    _g11_postflight("people_search", estimated_usd)

    people = data.get("people") or data.get("contacts") or []
    parsed = [_parse_person(p) for p in people if isinstance(p, dict)]
    _LOG.info(
        "apollo search page=%s per_page=%s -> %d people (%d sendable)",
        page, per_page, len(parsed), sum(1 for p in parsed if p.sendable),
    )
    return parsed


def enrich_person(
    contact: ApolloContact,
    *,
    client: httpx.Client | None = None,
) -> ApolloContact:
    """Reveal a locked email via Apollo People Match (consumes a credit).

    Returns a NEW ApolloContact with email / email_status filled from the match
    response. If the match yields nothing usable the original contact is
    returned unchanged. Best-effort: a transport error is logged and the
    original contact returned, so one bad unlock never aborts the campaign.
    """
    if not contact.person_id:
        return contact
    # G11 pre-flight: budget + codex audit BEFORE the HTTP call.
    # email_unlock = 1 credit per match. Cap hit -> raises before the credit
    # is consumed; the campaign caller must catch ApolloBudgetExceeded.
    estimated_usd = _g11_preflight(
        "email_unlock", units=1, prospect_id=contact.person_id,
    )

    body = {"id": contact.person_id, "reveal_personal_emails": False}
    cli, owned = _client(client)
    call_succeeded = False
    try:
        resp = cli.post(_MATCH_PATH, json=body)
        if resp.status_code >= 400:
            _LOG.warning("apollo match HTTP %s for person=%s", resp.status_code, contact.person_id)
            return contact
        person = (resp.json() or {}).get("person") or {}
        call_succeeded = True
    except httpx.HTTPError as exc:
        _LOG.warning("apollo match transport error person=%s: %s", contact.person_id, exc)
        return contact
    finally:
        if owned:
            cli.close()
        # Apollo bills email_unlock on successful match only; HTTP errors
        # / 4xx are not charged (Apollo's standard behaviour). Record
        # spend only on success.
        if call_succeeded:
            _g11_postflight(
                "email_unlock", estimated_usd, prospect_id=contact.person_id,
            )

    revealed = _parse_person(person) if person else contact
    # Preserve search-time fields the match might not echo back.
    return contact.model_copy(update={
        "email": revealed.email or contact.email,
        "email_status": revealed.email_status or contact.email_status,
        "linkedin_url": revealed.linkedin_url or contact.linkedin_url,
        "phone": revealed.phone or contact.phone,
    })
