"""G8 aggregator — run available collectors, build a LegitimacyAssessment.

The aggregator is intentionally tolerant of prospect shape: it accepts
either a pydantic ``ProspectRecord``, an ``ApolloContact``, or a plain
mapping with the same field names. It picks off whatever fields are
present (business_name/company, email, phone, city) and dispatches each
collector accordingly. Missing fields skip the matching collector — they
never raise.

Collectors run sequentially (each is small, deterministic, and the
network-bound CA SOS lookup has a tight 2s timeout). Results are
deduplicated by ``kind``: only the first signal per kind survives, so a
prospect that hits both a chamber roster and the registry produces two
distinct signals and a prospect that double-hits the registry produces
one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .legitimacy import LegitimacyAssessment, LegitimacySignal, has_warmth
from .sources.ca_sos import lookup_ca_sos
from .sources.chamber_roster import lookup_chamber_roster
from .sources.prior_inbound import lookup_prior_inbound

_LOG = logging.getLogger("samus.prospecting.legitimacy_check")


def _get(prospect: Any, *names: str) -> str:
    """Pull the first non-empty field from prospect by any of the given names."""
    for name in names:
        try:
            value = getattr(prospect, name)
        except AttributeError:
            value = None
        if value is None and isinstance(prospect, dict):
            value = prospect.get(name)
        if value:
            return str(value).strip()
    return ""


def _prospect_id(prospect: Any) -> str:
    return _get(prospect, "prospect_id", "person_id", "id")


def collect_signals(prospect: Any) -> list[LegitimacySignal]:
    """Run every applicable collector against ``prospect``; dedupe by kind."""
    business_name = _get(prospect, "company_name", "company")
    email = _get(prospect, "owner_email", "email")
    phone = _get(prospect, "phone")
    city = _get(prospect, "city")

    raw: list[LegitimacySignal] = []

    if business_name:
        try:
            sig = lookup_ca_sos(business_name)
            if sig is not None:
                raw.append(sig)
        except Exception as exc:  # noqa: BLE001
            _LOG.info("ca_sos collector raised: %s", exc)

    if business_name and city:
        try:
            sig = lookup_chamber_roster(business_name, city=city)
            if sig is not None:
                raw.append(sig)
        except Exception as exc:  # noqa: BLE001
            _LOG.info("chamber_roster collector raised: %s", exc)

    if email or phone:
        try:
            sig = lookup_prior_inbound(email=email, phone=phone)
            if sig is not None:
                raw.append(sig)
        except Exception as exc:  # noqa: BLE001
            _LOG.info("prior_inbound collector raised: %s", exc)

    seen: set[str] = set()
    deduped: list[LegitimacySignal] = []
    for sig in raw:
        if sig.kind in seen:
            continue
        seen.add(sig.kind)
        deduped.append(sig)
    return deduped


def assess_warmth(prospect: Any) -> LegitimacyAssessment:
    """Run collect_signals + wrap in a LegitimacyAssessment."""
    signals = collect_signals(prospect)
    return LegitimacyAssessment(
        prospect_id=_prospect_id(prospect),
        signals=signals,
        has_warmth=has_warmth(signals),
        assessed_at=datetime.now(timezone.utc),
    )


_CONFIDENCE_RANK = {"high": 2, "medium": 1}


def highest_confidence_kind(signals: list[LegitimacySignal]) -> str:
    """Return the kind of the highest-confidence signal; "" when empty.

    Ties broken by the order signals appear in the list (collector order).
    Used to populate the scalar ``legitimacy_signal`` field on outreach
    payloads — the value the Codex VW-G8 advisory keys off.
    """
    best: LegitimacySignal | None = None
    for sig in signals:
        if best is None:
            best = sig
            continue
        if _CONFIDENCE_RANK.get(sig.confidence, 0) > _CONFIDENCE_RANK.get(best.confidence, 0):
            best = sig
    return best.kind if best is not None else ""


__all__ = [
    "assess_warmth",
    "collect_signals",
    "highest_confidence_kind",
]
