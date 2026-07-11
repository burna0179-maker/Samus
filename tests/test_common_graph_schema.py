"""Tests for backend.common.graph_schema — pure validation logic."""
from __future__ import annotations

import pytest

from backend.common import graph_schema


# --- validate_node ---------------------------------------------------------

def test_validate_node_accepts_known_label_with_pk():
    graph_schema.validate_node("Account", {"account_id": "A-1", "name": "Acme"})


def test_validate_node_allows_extra_properties():
    graph_schema.validate_node(
        "Contact",
        {"contact_id": "C-1", "account_id": "A-1", "extra": "ok"},
    )


def test_validate_node_rejects_unknown_label():
    with pytest.raises(ValueError, match="unknown label"):
        graph_schema.validate_node("NotALabel", {"id": "x"})


def test_validate_node_rejects_missing_primary_key():
    with pytest.raises(ValueError, match="requires primary key"):
        graph_schema.validate_node("Account", {"name": "Acme"})


def test_validate_node_rejects_empty_primary_key():
    with pytest.raises(ValueError, match="cannot be empty"):
        graph_schema.validate_node("Account", {"account_id": ""})


def test_validate_node_rejects_none_primary_key():
    with pytest.raises(ValueError, match="cannot be empty"):
        graph_schema.validate_node("Account", {"account_id": None})


def test_validate_node_rejects_non_dict_props():
    with pytest.raises(ValueError, match="properties must be a dict"):
        graph_schema.validate_node("Account", "not-a-dict")  # type: ignore[arg-type]


def test_primary_key_returns_first_property():
    assert graph_schema.primary_key("Account") == "account_id"
    assert graph_schema.primary_key("Task") == "task_id"


def test_primary_key_raises_for_unknown_label():
    with pytest.raises(ValueError, match="unknown label"):
        graph_schema.primary_key("NoSuch")


# --- validate_relationship -------------------------------------------------

def test_validate_relationship_accepts_allowed_triple():
    graph_schema.validate_relationship("Account", "HAS_CONTACT", "Contact")
    graph_schema.validate_relationship("Task", "EMITTED", "AuditEvent")
    graph_schema.validate_relationship("Task", "TARGETED", "Prospect")


def test_validate_relationship_rejects_disallowed_triple():
    with pytest.raises(ValueError, match="not allowed"):
        graph_schema.validate_relationship("Account", "BOGUS_REL", "Contact")
    with pytest.raises(ValueError, match="not allowed"):
        graph_schema.validate_relationship("Contact", "HAS_CONTACT", "Account")  # reversed
    with pytest.raises(ValueError, match="not allowed"):
        graph_schema.validate_relationship("Account", "HAS_CONTACT", "NotALabel")


# --- allowed_query ---------------------------------------------------------

def test_allowed_query_returns_cypher_for_known_name():
    cypher = graph_schema.allowed_query("account_by_id")
    assert "MATCH (a:Account" in cypher
    assert "$account_id" in cypher


def test_allowed_query_raises_for_unknown_name():
    with pytest.raises(ValueError, match="not in allowlist"):
        graph_schema.allowed_query("DELETE_ALL_THE_THINGS")


def test_allowlist_covers_documented_queries():
    expected = {
        "account_by_id",
        "contacts_for_account",
        "prospects_for_zipcode",
        "task_lineage",
        "recent_audit_events_for_service",
    }
    assert expected.issubset(set(graph_schema.QUERY_ALLOWLIST))


# --- structural invariants -------------------------------------------------

def test_every_indexed_label_is_a_known_label():
    for label, _prop in graph_schema.INDEXES:
        assert label in graph_schema.NODE_LABELS


def test_every_relationship_endpoint_is_a_known_label():
    for src, _rel, tgt in graph_schema.RELATIONSHIPS:
        assert src in graph_schema.NODE_LABELS
        assert tgt in graph_schema.NODE_LABELS
