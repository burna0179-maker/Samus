"""Harm-signal collectors for G7 reward subtraction.

Three pure read-only counters keyed off an ``opportunity_id``:

* :func:`retracted_claims_for` — artifacts on this opportunity whose
  ``kind`` or ``title`` indicates a retraction (operator clawed back a
  generated claim, or an artifact superseded an earlier one).
* :func:`unsubscribes_for` — outreach conversations marked ``unsubscribe``
  for this opportunity's prospect.
* :func:`complaints_for` — SES complaint feedback events whose
  ``recipients`` overlap any Contact email belonging to this opportunity's
  prospect.

All three fail-OPEN (return 0 + log a warning) on lookup failure: a
transient DDB hiccup must not block a reward computation. The caller
(:func:`backend.strategy.reward_density.compute_reward`) is responsible for
fail-CLOSED behaviour when ALL three collectors error simultaneously —
that pattern (every read down at once) is itself the safety failure.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

__all__ = [
    "HarmSignalStore",
    "DefaultHarmSignalStore",
    "retracted_claims_for",
    "unsubscribes_for",
    "complaints_for",
]


_LOG = logging.getLogger("samus.strategy.harm_signals")


# Token strings that mark a retraction in artifact kind / title. Kept narrow
# so a content_draft whose title happens to mention "supersede" in passing
# does not score. ``ArtifactKind`` literal does not yet have a "retracted"
# member — operators flag a retraction by appending the token to ``title``
# or by writing a new artifact with ``kind == "other"`` and ``title``
# starting with the marker.
_RETRACTION_MARKERS: tuple[str, ...] = ("retracted", "superseded")


class HarmSignalStore(Protocol):
    """Pluggable read-side for harm collectors.

    Production implementations call into the CRM workcell; tests pass a
    stub that returns pre-canned rows. The protocol shape stays narrow on
    purpose — three list-returning reads is the full surface.
    """

    def artifacts_for_opportunity(self, opportunity_id: str) -> list[dict[str, Any]]: ...

    def conversations_for_prospect(self, prospect_id: str) -> list[dict[str, Any]]: ...

    def contacts_for_prospect(self, prospect_id: str) -> list[dict[str, Any]]: ...

    def opportunity(self, opportunity_id: str) -> dict[str, Any] | None: ...

    def complaint_recipients(self) -> list[str]: ...


class DefaultHarmSignalStore:
    """Production store — wraps the CRM service-layer scans + feedback events.

    Imports are deferred to method bodies so this module stays importable
    on a host where AWS credentials are not configured (mirrors the
    crm.persistence test-time pattern).
    """

    def artifacts_for_opportunity(self, opportunity_id: str) -> list[dict[str, Any]]:
        from backend.crm import service as crm_service
        result = crm_service.list_artifacts(owner_entity_id=opportunity_id, limit=500)
        return [a.model_dump() for a in result.artifacts]

    def conversations_for_prospect(self, prospect_id: str) -> list[dict[str, Any]]:
        from backend.crm import service as crm_service
        result = crm_service.list_conversations(prospect_id=prospect_id, limit=500)
        return [c.model_dump() for c in result.conversations]

    def contacts_for_prospect(self, prospect_id: str) -> list[dict[str, Any]]:
        from backend.crm import service as crm_service
        result = crm_service.list_contacts(prospect_id=prospect_id, limit=500)
        return [c.model_dump() for c in result.contacts]

    def opportunity(self, opportunity_id: str) -> dict[str, Any] | None:
        from backend.crm import service as crm_service
        opp = crm_service.get_opportunity(opportunity_id)
        return opp.model_dump() if opp is not None else None

    def complaint_recipients(self) -> list[str]:
        # SES complaint feedback events live in the feedback workcell's
        # events table. Phase-1 reads via a full scan filtered to
        # ``notification_type == 'Complaint'``; aggregating all complainant
        # addresses across the table is intentional — a complaint signal
        # for a prospect's email is a hard harm regardless of which task
        # surfaced it.
        from backend.common import aws
        from backend.common.settings import get_settings
        settings = get_settings()
        table = aws.table(settings.ddb_feedback_table, settings.aws_region)
        out: list[str] = []
        try:
            resp = table.scan(Limit=500)
        except Exception as exc:  # noqa: BLE001 — fail-OPEN at the collector
            _LOG.warning("complaint feedback scan failed: %s", exc)
            return out
        for row in resp.get("Items", []) or []:
            if row.get("notification_type") != "Complaint":
                continue
            for addr in row.get("recipients", []) or []:
                if isinstance(addr, str) and addr:
                    out.append(addr.strip().lower())
        return out


def _matches_retraction(row: dict[str, Any]) -> bool:
    kind = (row.get("kind") or "").strip().lower()
    title = (row.get("title") or "").strip().lower()
    for marker in _RETRACTION_MARKERS:
        if marker in kind or title.startswith(marker):
            return True
    return False


def retracted_claims_for(
    opportunity_id: str, *, store: HarmSignalStore | None = None,
) -> int:
    """Count artifacts on this opportunity that mark a retraction.

    Fail-OPEN: any exception returns 0 + a warning log. The reward
    computation upstream catches the all-three-down case and fails CLOSED.
    """
    s = store if store is not None else DefaultHarmSignalStore()
    try:
        rows = s.artifacts_for_opportunity(opportunity_id)
    except Exception as exc:  # noqa: BLE001 — fail-OPEN
        _LOG.warning(
            "retracted_claims_for lookup failed opp=%s: %s", opportunity_id, exc,
        )
        return 0
    return sum(1 for row in rows if _matches_retraction(row))


def unsubscribes_for(
    opportunity_id: str, *, store: HarmSignalStore | None = None,
) -> int:
    """Count outreach conversations marked ``unsubscribe`` for this prospect.

    The CRM Conversation FSM marks an inbound unsubscribe via the
    ``outcome`` field. Fail-OPEN on any lookup failure.
    """
    s = store if store is not None else DefaultHarmSignalStore()
    try:
        opp = s.opportunity(opportunity_id)
        if opp is None:
            return 0
        prospect_id = opp.get("prospect_id") or ""
        if not prospect_id:
            return 0
        rows = s.conversations_for_prospect(prospect_id)
    except Exception as exc:  # noqa: BLE001 — fail-OPEN
        _LOG.warning(
            "unsubscribes_for lookup failed opp=%s: %s", opportunity_id, exc,
        )
        return 0
    return sum(
        1 for row in rows
        if (row.get("outcome") or "").strip().lower() == "unsubscribe"
    )


def complaints_for(
    opportunity_id: str, *, store: HarmSignalStore | None = None,
) -> int:
    """Count SES complaint events overlapping any Contact email on this prospect.

    Cross-references the feedback workcell's complaint events
    (``notification_type == 'Complaint'``) against the prospect's Contact
    rows. Fail-OPEN on any lookup failure.
    """
    s = store if store is not None else DefaultHarmSignalStore()
    try:
        opp = s.opportunity(opportunity_id)
        if opp is None:
            return 0
        prospect_id = opp.get("prospect_id") or ""
        if not prospect_id:
            return 0
        contacts = s.contacts_for_prospect(prospect_id)
        prospect_emails = {
            (c.get("email") or "").strip().lower()
            for c in contacts
            if (c.get("email") or "").strip()
        }
        if not prospect_emails:
            return 0
        complainants = s.complaint_recipients()
    except Exception as exc:  # noqa: BLE001 — fail-OPEN
        _LOG.warning(
            "complaints_for lookup failed opp=%s: %s", opportunity_id, exc,
        )
        return 0
    return sum(1 for addr in complainants if addr in prospect_emails)
