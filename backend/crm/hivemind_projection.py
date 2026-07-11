"""CRM -> Hivemind (Neo4j) projection — CRM Phase-2 (code-half).

Projects the CRM deal sub-graph (Prospect -> Contact -> Opportunity, plus the
opportunity's pipeline stage / win-probability) into the LOCAL Neo4j knowledge
graph through :mod:`backend.common.graph_client` — the exact same path the
memory workcell uses. DynamoDB stays the authoritative store; this is a
secondary, best-effort mirror so the rest of the ecosystem (the Hivemind) can
traverse "which contacts / deals belong to this prospect, and where each deal
sits in the pipeline".

Design constraints (doctrine):

  * **Flag-gated, default OFF.** Nothing here runs unless
    ``settings.crm_hivemind_projection_enabled`` is True. When the flag is off
    every public function is a no-op that returns a structured "skipped"
    result — the CRM mutation paths that call it are unchanged.

  * **Local-default + fail-closed.** The projection writes go through the local
    :class:`GraphClient`, which degrades to a no-op when Neo4j is unreachable
    and ``neo4j_required`` is False (the default). A graph outage therefore can
    never fail or block a CRM mutation — the DDB write has already committed by
    the time we are called, and a missed mirror is acceptable. NO AWS / cloud
    dependency: importing or running this module requires neither.

  * **No arbitrary Cypher.** Every write is a schema-validated
    ``write_node`` / ``write_relationship`` against the allowlist in
    :mod:`backend.common.graph_schema` (which now carries the CRM
    Prospect-rooted relationships).

  * **Best-effort, never raises.** Any unexpected error is logged and
    swallowed; the caller treats projection as fire-and-forget.

The DDB GSI / cloud-persistence half of CRM Phase-2 is a SEPARATE, creds-gated
opt-in and is intentionally not implemented here.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.common.config import get_settings
from backend.common.graph_client import GraphClient, get_client
from backend.common import graph_schema

from .models import Contact, Conversation, Opportunity, Prospect
from .scoring import effective_deal_size_usd

_LOG = logging.getLogger("samus.crm.hivemind_projection")


# Result reason codes (returned, never raised) so callers + tests can branch.
SKIPPED_FLAG_OFF = "skipped_flag_off"
SKIPPED_GRAPH_UNAVAILABLE = "skipped_graph_unavailable"
SKIPPED_NO_PROSPECT = "skipped_no_prospect_id"
PROJECTED = "projected"
ERROR = "error"


def projection_enabled() -> bool:
    """True iff the CRM Hivemind projection is armed (flag-gated, default OFF)."""
    return bool(get_settings().crm_hivemind_projection_enabled)


def _tier_property() -> dict[str, str]:
    """The ``tier`` property to stamp on projected nodes, per W-4 tiering.

    Mirrors the memory workcell convention: when ``SAMUS_KG_TIER_MODE=label``
    every node carries a ``tier`` (``private`` on write; promotion to
    ``hivemind`` is a separate explicit action). When the mode is ``off``
    (default) no ``tier`` property is written at all, so the local Compose
    stack is unchanged.
    """
    if get_settings().kg_tier_mode == "label":
        return {graph_schema.TIER_PROPERTY: graph_schema.TIER_PRIVATE}
    return {}


def _opportunity_node_props(opp: Opportunity) -> dict[str, Any]:
    """Map a CRM :class:`Opportunity` onto the graph_schema Opportunity node.

    Carries the load-bearing pipeline fields (stage + win probability + deal
    size). The schema permits extra properties beyond its declared set
    (forward-compat — see graph_schema.validate_node), so projecting
    ``deal_size_usd`` / ``close_probability`` alongside the canonical
    ``opportunity_id`` PK is safe.
    """
    props: dict[str, Any] = {
        "opportunity_id": opp.opportunity_id,
        "prospect_id": opp.prospect_id,
        "stage": opp.stage,
        "deal_size_usd": effective_deal_size_usd(float(opp.deal_size_usd or 0.0)),
        "close_probability": float(opp.close_probability or 0.0),
        "updated_at": opp.updated_at,
    }
    if opp.contact_id:
        props["contact_id"] = opp.contact_id
    props.update(_tier_property())
    return props


def project_opportunity(
    opp: Opportunity,
    *,
    prospect: Prospect | None = None,
    contact: Contact | None = None,
    client: GraphClient | None = None,
) -> dict[str, Any]:
    """Project an Opportunity (+ its prospect / contact) into the local graph.

    This is the single projection entry-point the CRM service calls after a
    successful opportunity create/advance. It:

      1. MERGEs a :Prospect node (by ``prospect_id``) — minimal stub if a full
         :class:`Prospect` was not supplied, so the edge has a valid endpoint.
      2. MERGEs the :Opportunity node carrying its current pipeline stage.
      3. MERGEs ``(:Prospect)-[:HAS_OPPORTUNITY]->(:Opportunity)``.
      4. If a contact is known, MERGEs the :Contact node + the
         ``(:Prospect)-[:HAS_CONTACT]->(:Contact)`` and
         ``(:Contact)-[:PARTICIPATES_IN]->(:Opportunity)`` edges.

    Returns a structured ``{"status": ..., "reason": ...}`` dict and NEVER
    raises. Reasons: ``projected`` (writes issued), ``skipped_flag_off``,
    ``skipped_graph_unavailable`` (Neo4j down — local-default no-op),
    ``skipped_no_prospect_id`` (nothing to root the sub-graph on), ``error``
    (unexpected; logged).
    """
    if not projection_enabled():
        return {"status": "skipped", "reason": SKIPPED_FLAG_OFF}

    prospect_id = (opp.prospect_id or (prospect.prospect_id if prospect else "")).strip()
    if not prospect_id:
        # No prospect to hang the deal off — the schema requires a Prospect PK
        # for the HAS_OPPORTUNITY edge. Nothing to project; not an error.
        _LOG.debug(
            "crm projection skipped: opportunity %s has no prospect_id",
            opp.opportunity_id,
        )
        return {"status": "skipped", "reason": SKIPPED_NO_PROSPECT}

    gc = client or get_client()
    if not gc.available:
        # Local-default: Neo4j down + not required -> clean no-op. The DDB row
        # is already authoritative; a missed mirror is acceptable.
        _LOG.debug(
            "crm projection skipped: graph unavailable (opportunity %s)",
            opp.opportunity_id,
        )
        return {"status": "skipped", "reason": SKIPPED_GRAPH_UNAVAILABLE}

    try:
        # 1. Prospect node (full props if supplied, else a minimal stub).
        prospect_props: dict[str, Any] = {"prospect_id": prospect_id}
        if prospect is not None:
            company = (
                getattr(prospect, "company_name", "")
                or getattr(prospect, "company", "")
                or ""
            )
            if company:
                prospect_props["company_name"] = company
            industry = getattr(prospect, "industry", "") or ""
            if industry:
                prospect_props["industry"] = industry
        prospect_props.update(_tier_property())
        gc.write_node("Prospect", prospect_props)

        # 2. Opportunity node (carries the pipeline stage).
        gc.write_node("Opportunity", _opportunity_node_props(opp))

        # 3. Prospect -> Opportunity edge.
        gc.write_relationship(
            "Prospect", prospect_id, "HAS_OPPORTUNITY",
            "Opportunity", opp.opportunity_id,
        )

        # 4. Contact, if known.
        contact_id = (opp.contact_id or (contact.contact_id if contact else "")).strip()
        if contact_id:
            contact_props: dict[str, Any] = {
                "contact_id": contact_id,
                "prospect_id": prospect_id,
            }
            if contact is not None:
                if contact.name:
                    contact_props["name"] = contact.name
                if contact.email:
                    contact_props["email"] = contact.email
                if contact.role:
                    contact_props["role"] = contact.role
            contact_props.update(_tier_property())
            gc.write_node("Contact", contact_props)
            gc.write_relationship(
                "Prospect", prospect_id, "HAS_CONTACT",
                "Contact", contact_id,
            )
            gc.write_relationship(
                "Contact", contact_id, "PARTICIPATES_IN",
                "Opportunity", opp.opportunity_id,
            )

        return {
            "status": "ok",
            "reason": PROJECTED,
            "opportunity_id": opp.opportunity_id,
            "prospect_id": prospect_id,
            "stage": opp.stage,
        }
    except Exception as exc:  # noqa: BLE001 — best-effort mirror, never raise
        _LOG.warning(
            "crm projection failed for opportunity %s: %s",
            opp.opportunity_id, exc,
        )
        return {"status": "error", "reason": ERROR, "error": str(exc)}


def _conversation_node_props(conv: Conversation) -> dict[str, Any]:
    """Map a CRM :class:`Conversation` onto the graph_schema Conversation node.

    Carries the thread's routing/outcome fields (channel + status + direction +
    outcome) alongside the canonical ``conversation_id`` PK. Long free-text
    fields (transcript / summary) are intentionally NOT projected — the graph
    is a topology mirror, not a document store; the authoritative text stays in
    the DDB row.
    """
    props: dict[str, Any] = {
        "conversation_id": conv.conversation_id,
        "prospect_id": conv.prospect_id,
        "channel": conv.channel,
        "status": conv.status,
        "direction": conv.direction,
    }
    if conv.contact_id:
        props["contact_id"] = conv.contact_id
    if conv.outcome:
        props["outcome"] = conv.outcome
    if conv.started_at:
        props["started_at"] = conv.started_at
    if conv.ended_at:
        props["ended_at"] = conv.ended_at
    props.update(_tier_property())
    return props


def project_conversation(
    conv: Conversation,
    *,
    prospect: Prospect | None = None,
    contact: Contact | None = None,
    client: GraphClient | None = None,
) -> dict[str, Any]:
    """Project a Conversation (+ its prospect / contact) into the local graph.

    The single projection entry-point the CRM service calls after a successful
    ``upsert_conversation``. It:

      1. MERGEs a :Prospect node (by ``prospect_id``) — minimal stub if a full
         :class:`Prospect` was not supplied, so the edge has a valid endpoint.
      2. MERGEs the :Conversation node carrying its channel / status / outcome.
      3. MERGEs ``(:Prospect)-[:HAS_CONVERSATION]->(:Conversation)``.
      4. If a contact is known, MERGEs the :Contact node + the
         ``(:Prospect)-[:HAS_CONTACT]->(:Contact)`` and
         ``(:Contact)-[:PARTICIPATES_IN]->(:Conversation)`` edges.

    Same contract as :func:`project_opportunity`: returns a structured
    ``{"status": ..., "reason": ...}`` dict and NEVER raises. Reasons:
    ``projected``, ``skipped_flag_off``, ``skipped_graph_unavailable``,
    ``skipped_no_prospect_id``, ``error``.
    """
    if not projection_enabled():
        return {"status": "skipped", "reason": SKIPPED_FLAG_OFF}

    prospect_id = (
        conv.prospect_id or (prospect.prospect_id if prospect else "")
    ).strip()
    if not prospect_id:
        _LOG.debug(
            "crm projection skipped: conversation %s has no prospect_id",
            conv.conversation_id,
        )
        return {"status": "skipped", "reason": SKIPPED_NO_PROSPECT}

    gc = client or get_client()
    if not gc.available:
        _LOG.debug(
            "crm projection skipped: graph unavailable (conversation %s)",
            conv.conversation_id,
        )
        return {"status": "skipped", "reason": SKIPPED_GRAPH_UNAVAILABLE}

    try:
        # 1. Prospect node (full props if supplied, else a minimal stub).
        prospect_props: dict[str, Any] = {"prospect_id": prospect_id}
        if prospect is not None:
            company = (
                getattr(prospect, "company_name", "")
                or getattr(prospect, "company", "")
                or ""
            )
            if company:
                prospect_props["company_name"] = company
            industry = getattr(prospect, "industry", "") or ""
            if industry:
                prospect_props["industry"] = industry
        prospect_props.update(_tier_property())
        gc.write_node("Prospect", prospect_props)

        # 2. Conversation node (carries channel / status / outcome).
        gc.write_node("Conversation", _conversation_node_props(conv))

        # 3. Prospect -> Conversation edge.
        gc.write_relationship(
            "Prospect", prospect_id, "HAS_CONVERSATION",
            "Conversation", conv.conversation_id,
        )

        # 4. Contact, if known.
        contact_id = (
            conv.contact_id or (contact.contact_id if contact else "")
        ).strip()
        if contact_id:
            contact_props: dict[str, Any] = {
                "contact_id": contact_id,
                "prospect_id": prospect_id,
            }
            if contact is not None:
                if contact.name:
                    contact_props["name"] = contact.name
                if contact.email:
                    contact_props["email"] = contact.email
                if contact.role:
                    contact_props["role"] = contact.role
            contact_props.update(_tier_property())
            gc.write_node("Contact", contact_props)
            gc.write_relationship(
                "Prospect", prospect_id, "HAS_CONTACT",
                "Contact", contact_id,
            )
            gc.write_relationship(
                "Contact", contact_id, "PARTICIPATES_IN",
                "Conversation", conv.conversation_id,
            )

        return {
            "status": "ok",
            "reason": PROJECTED,
            "conversation_id": conv.conversation_id,
            "prospect_id": prospect_id,
            "channel": conv.channel,
        }
    except Exception as exc:  # noqa: BLE001 — best-effort mirror, never raise
        _LOG.warning(
            "crm projection failed for conversation %s: %s",
            conv.conversation_id, exc,
        )
        return {"status": "error", "reason": ERROR, "error": str(exc)}


__all__ = [
    "ERROR",
    "PROJECTED",
    "SKIPPED_FLAG_OFF",
    "SKIPPED_GRAPH_UNAVAILABLE",
    "SKIPPED_NO_PROSPECT",
    "project_conversation",
    "project_opportunity",
    "projection_enabled",
]
