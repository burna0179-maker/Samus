"""Neo4j entity schemas + query allowlist for memory workcell.

Centralizes the labels, relationships, indexes, and the set of allowed
Cypher templates the gateway will route to. Anything outside the allowlist
gets rejected at the API boundary so arbitrary Cypher injection is impossible.

Per doc Section 10 (Neo4j Knowledge Graph Layer).
"""
from __future__ import annotations

from typing import Final


# Node labels with their expected property names.
# Insertion order matters: the FIRST entry of each set is treated as the
# primary key for that label (used by write_node MERGE patterns).
NODE_LABELS: Final[dict[str, list[str]]] = {
    "Account":     ["account_id", "name", "industry", "size_tier", "created_at"],
    "Contact":     ["contact_id", "account_id", "name", "email", "role"],
    "Prospect":    ["prospect_id", "account_id", "company_name", "zipcode", "industry"],
    "Opportunity": ["opportunity_id", "account_id", "stage", "deal_size_est"],
    "Conversation": ["conversation_id", "prospect_id", "contact_id", "channel",
                     "status", "direction", "outcome", "started_at"],
    "Activity":    ["activity_id", "account_id", "contact_id", "kind", "outcome", "ts"],
    "Task":        ["task_id", "service", "action", "status", "ts"],
    "AuditEvent":  ["event_id", "service", "task_id", "action", "status", "ts"],
}


# (source_label, rel_type, target_label) triples.
RELATIONSHIPS: Final[set[tuple[str, str, str]]] = {
    ("Account", "HAS_CONTACT",      "Contact"),
    ("Account", "HAS_PROSPECT",     "Prospect"),
    ("Account", "HAS_OPPORTUNITY",  "Opportunity"),
    ("Contact", "PARTICIPATES_IN",  "Activity"),
    ("Account", "HAS_ACTIVITY",     "Activity"),
    ("Task",    "EMITTED",          "AuditEvent"),
    ("Task",    "TARGETED",         "Account"),
    ("Task",    "TARGETED",         "Prospect"),
    # --- CRM Hivemind projection (CRM Phase-2) ------------------------------
    # The CRM workcell folds the Account concept INTO the Prospect (see
    # backend/crm/models.py — "accounts folded into prospects"), so the CRM
    # projection hangs Contact + Opportunity directly off the Prospect node
    # rather than an Account root. These edges are additive — the
    # Account-rooted edges above are untouched.
    ("Prospect", "HAS_CONTACT",     "Contact"),
    ("Prospect", "HAS_OPPORTUNITY", "Opportunity"),
    ("Contact",  "PARTICIPATES_IN", "Opportunity"),
    # Conversation thread (call transcript / email chain) hung off the prospect,
    # with the contact (when known) as a participant. Additive — mirrors the
    # Contact/Opportunity shape so the Hivemind can traverse "which prospect /
    # contact owns this conversation" alongside the deal sub-graph.
    ("Prospect", "HAS_CONVERSATION", "Conversation"),
    ("Contact",  "PARTICIPATES_IN",  "Conversation"),
}


# Indexes to create at init time (label, property).
INDEXES: Final[tuple[tuple[str, str], ...]] = (
    ("Account",     "account_id"),
    ("Contact",     "contact_id"),
    ("Prospect",    "prospect_id"),
    ("Opportunity", "opportunity_id"),
    ("Conversation", "conversation_id"),
    ("Activity",    "activity_id"),
    ("Task",        "task_id"),
    ("AuditEvent",  "event_id"),
)


# Allowed query templates. Key = template name, value = Cypher with $param
# placeholders. Anything else is rejected at the API boundary.
QUERY_ALLOWLIST: Final[dict[str, str]] = {
    "account_by_id":
        "MATCH (a:Account {account_id: $account_id}) RETURN a LIMIT 1",
    "contacts_for_account":
        "MATCH (a:Account {account_id: $account_id})-[:HAS_CONTACT]->(c:Contact) RETURN c",
    "prospects_for_zipcode":
        "MATCH (p:Prospect {zipcode: $zipcode}) RETURN p LIMIT $limit",
    "task_lineage":
        "MATCH (t:Task {task_id: $task_id})-[:EMITTED]->(e:AuditEvent) "
        "RETURN t, collect(e) AS events",
    "recent_audit_events_for_service":
        "MATCH (e:AuditEvent {service: $service}) "
        "RETURN e ORDER BY e.ts DESC LIMIT $limit",
}


# --- knowledge-graph tiers (W-4 — doc CLOUD_RUN_DEPLOY.md §5.2 / D-6) ------
# The standalone GCP AuraDB instance is single-database, so the private vs.
# promotable-"hivemind" split is represented as a node ``tier`` *property*
# rather than a separate database. ``private`` is the default for every
# ingested node; ``hivemind`` marks a node promoted for export to the
# ecosystem — the §7 experience feed reads exactly the hivemind-tier set.
# The convention is dormant unless ``SAMUS_KG_TIER_MODE=label`` (Settings
# field ``kg_tier_mode``); the local Compose stack writes no ``tier`` at all.
TIER_PROPERTY: Final = "tier"
TIER_PRIVATE: Final = "private"
TIER_HIVEMIND: Final = "hivemind"
VALID_TIERS: Final[frozenset[str]] = frozenset({TIER_PRIVATE, TIER_HIVEMIND})


def primary_key(label: str) -> str:
    """Return the conventional primary-key property for a label."""
    if label not in NODE_LABELS:
        raise ValueError(f"unknown label: {label}")
    return NODE_LABELS[label][0]


def validate_node(label: str, properties: dict) -> None:
    """Raise ValueError if label is unknown or the primary key is missing.

    Extra props beyond the schema are allowed (forward-compat). The primary
    key is the first property in NODE_LABELS[label].
    """
    if label not in NODE_LABELS:
        raise ValueError(f"unknown label: {label}")
    if not isinstance(properties, dict):
        raise ValueError("properties must be a dict")
    pk = NODE_LABELS[label][0]
    if pk not in properties:
        raise ValueError(f"label {label} requires primary key '{pk}'")
    if properties[pk] in (None, ""):
        raise ValueError(f"label {label} primary key '{pk}' cannot be empty")


def validate_relationship(source_label: str, rel_type: str, target_label: str) -> None:
    """Raise ValueError if the (source, rel, target) triple is not on the allowlist."""
    if (source_label, rel_type, target_label) not in RELATIONSHIPS:
        raise ValueError(
            f"relationship not allowed: ({source_label})-[{rel_type}]->({target_label})"
        )


def allowed_query(name: str) -> str:
    """Return the Cypher template for an allowlisted query name."""
    if name not in QUERY_ALLOWLIST:
        raise ValueError(f"query not in allowlist: {name}")
    return QUERY_ALLOWLIST[name]
