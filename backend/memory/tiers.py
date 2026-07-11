"""Knowledge-graph tier helpers for the memory workcell (work-item W-4).

The standalone GCP AuraDB instance is single-database, so the private vs.
promotable-``hivemind`` split (CLOUD_RUN_DEPLOY.md §5.2, decision D-6 option
6b) is represented as a node ``tier`` *property* rather than a separate
database. This module is the memory workcell's entry point to that
convention:

* :func:`stamp_default_tier` — stamp ``tier='private'`` on a node record at
  ingest time, when ``SAMUS_KG_TIER_MODE=label``.
* :func:`promote_node` — flip an existing node to ``tier='hivemind'``.
* :func:`list_hivemind_nodes` — read the promoted set (the source the §7
  experience feed / W-6 exports to the ecosystem).

When ``SAMUS_KG_TIER_MODE`` is ``off`` — the default, and the posture of the
local Docker Compose stack — :func:`stamp_default_tier` is a no-op and nothing
about graph writes changes. ``promote_node`` / ``list_hivemind_nodes`` are
direct graph operations and work regardless of the mode; the mode only governs
the default ``tier`` stamped at write time.
"""
from __future__ import annotations

from typing import Any

from backend.common.graph_client import GraphClient, get_client
from backend.common.graph_schema import (
    TIER_HIVEMIND,
    TIER_PRIVATE,
    TIER_PROPERTY,
    VALID_TIERS,
)
from backend.common.settings import get_settings

__all__ = [
    "TIER_PROPERTY",
    "TIER_PRIVATE",
    "TIER_HIVEMIND",
    "VALID_TIERS",
    "AUTO_PROMOTE_TRUST_LEVELS",
    "tier_mode",
    "tier_enabled",
    "stamp_default_tier",
    "promote_node",
    "should_auto_promote",
    "auto_promote_on_ingest",
    "list_hivemind_nodes",
]


# Trust levels whose knowledge auto-promotes to the ``hivemind`` tier at ingest.
# ``verified`` is a signed-authority source (see
# backend.memory.knowledge_ingest.TrustLevel) — exactly the class worth
# exporting to the ecosystem experience-feed. ``internal`` / ``external``
# knowledge stays ``private`` until an operator explicitly promotes it via
# POST /graph/promote.
AUTO_PROMOTE_TRUST_LEVELS: frozenset[str] = frozenset({"verified"})


def tier_mode() -> str:
    """Return the live ``SAMUS_KG_TIER_MODE`` value (``off`` | ``label``)."""
    return get_settings().kg_tier_mode


def tier_enabled() -> bool:
    """True when tiering is active (``SAMUS_KG_TIER_MODE=label``)."""
    return tier_mode() == "label"


def stamp_default_tier(record: dict[str, Any]) -> dict[str, Any]:
    """Return ``record`` with ``tier`` defaulted to ``private``.

    A no-op — the record is returned unchanged — when tiering is ``off`` or
    the record already carries an explicit ``tier``. A new dict is returned
    when a tier is added, so the caller's input is never mutated.
    """
    if not tier_enabled() or TIER_PROPERTY in record:
        return record
    return {**record, TIER_PROPERTY: TIER_PRIVATE}


def promote_node(
    label: str,
    key_value: Any,
    *,
    key_property: str | None = None,
    client: GraphClient | None = None,
) -> bool:
    """Promote a node to the ``hivemind`` tier.

    Thin wrapper over :meth:`GraphClient.promote_node` that resolves the
    module-level client when one is not supplied. Returns True if a node was
    matched and promoted. See the ``GraphClient`` method for the matching and
    error semantics.
    """
    return (client or get_client()).promote_node(
        label, key_value, key_property=key_property,
    )


def should_auto_promote(trust_level: str | None) -> bool:
    """True iff a node with this ``trust_level`` should auto-promote to hivemind.

    Gated on tiering being active (``SAMUS_KG_TIER_MODE=label``) — when tiering
    is ``off`` (the default local posture) this is always False and nothing is
    promoted. Case-insensitive on the trust level.
    """
    if not tier_enabled():
        return False
    return str(trust_level or "").lower() in AUTO_PROMOTE_TRUST_LEVELS


def auto_promote_on_ingest(
    label: str,
    key_value: Any,
    trust_level: str | None,
    *,
    key_property: str | None = None,
    client: GraphClient | None = None,
) -> bool:
    """Auto-promote a freshly-ingested node to the hivemind tier when warranted.

    This is the memory workcell's *auto-producer* for :func:`promote_node`: the
    knowledge-ingest path calls it after each successful chunk write so that
    signed-authority (``verified``) knowledge is exported to the ecosystem
    experience-feed without a manual POST /graph/promote. A clean no-op —
    returns False without touching the graph — when tiering is off or the
    trust level is not auto-promotable (see :data:`AUTO_PROMOTE_TRUST_LEVELS`).
    Returns True when a node was matched and promoted.
    """
    if not should_auto_promote(trust_level):
        return False
    return promote_node(label, key_value, key_property=key_property, client=client)


def list_hivemind_nodes(
    *,
    label: str | None = None,
    limit: int = 100,
    client: GraphClient | None = None,
) -> list[dict[str, Any]]:
    """Return the ``hivemind``-tier node set — the W-6 experience-feed source.

    Optionally filtered to a single ``label``. Returns [] when the graph
    driver is unavailable.
    """
    return (client or get_client()).nodes_in_tier(
        TIER_HIVEMIND, label=label, limit=limit,
    )
