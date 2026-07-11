"""TestClient coverage for the /graph/* endpoints on backend.memory.app.

The GraphClient is mocked so the tests don't require a running Neo4j.
"""
from __future__ import annotations

from typing import Any

import pytest


class _FakeGraphClient:
    """Drop-in stand-in for backend.common.graph_client.GraphClient."""

    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.init_called = False
        self.node_writes: list[tuple[str, dict[str, Any]]] = []
        self.rel_writes: list[tuple[str, Any, str, str, Any]] = []
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.query_rows: list[dict[str, Any]] = []

    def init_schema(self) -> bool:
        self.init_called = True
        return self.available

    def write_node(self, label: str, properties: dict[str, Any]) -> bool:
        self.node_writes.append((label, dict(properties)))
        return self.available

    def write_relationship(
        self,
        source_label: str,
        source_key: Any,
        rel_type: str,
        target_label: str,
        target_key: Any,
    ) -> bool:
        self.rel_writes.append(
            (source_label, source_key, rel_type, target_label, target_key)
        )
        return self.available

    def query(self, name: str, **params: Any) -> list[dict[str, Any]]:
        self.queries.append((name, dict(params)))
        return list(self.query_rows)


@pytest.fixture
def fake_client(monkeypatch):
    import backend.memory.app as app_mod

    fake = _FakeGraphClient(available=True)
    monkeypatch.setattr(app_mod, "_resolve_client", lambda: fake)
    return fake


@pytest.fixture
def unavailable_client(monkeypatch):
    import backend.memory.app as app_mod

    fake = _FakeGraphClient(available=False)
    monkeypatch.setattr(app_mod, "_resolve_client", lambda: fake)
    return fake


def _client():
    from fastapi.testclient import TestClient
    from backend.memory.app import app
    return TestClient(app)


# --- /graph/init -----------------------------------------------------------

def test_graph_init_ok(fake_client):
    r = _client().post("/graph/init", json={})
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert fake_client.init_called is True


def test_graph_init_unavailable(unavailable_client):
    r = _client().post("/graph/init", json={})
    assert r.status_code == 200
    assert r.json() == {"status": "unavailable"}
    assert unavailable_client.init_called is False


# --- /graph/write/node -----------------------------------------------------

def test_graph_write_node_ok(fake_client):
    r = _client().post(
        "/graph/write/node",
        json={"label": "Account", "properties": {"account_id": "A-1", "name": "Acme"}},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok"}
    assert fake_client.node_writes == [("Account", {"account_id": "A-1", "name": "Acme"})]


def test_graph_write_node_rejects_unknown_label(fake_client):
    r = _client().post(
        "/graph/write/node",
        json={"label": "Bogus", "properties": {"x": 1}},
    )
    assert r.status_code == 422
    assert "unknown label" in r.json()["detail"]
    assert fake_client.node_writes == []


def test_graph_write_node_rejects_missing_pk(fake_client):
    r = _client().post(
        "/graph/write/node",
        json={"label": "Account", "properties": {"name": "Acme"}},
    )
    assert r.status_code == 422
    assert "primary key" in r.json()["detail"]


def test_graph_write_node_unavailable(unavailable_client):
    r = _client().post(
        "/graph/write/node",
        json={"label": "Account", "properties": {"account_id": "A-1"}},
    )
    assert r.status_code == 200
    assert r.json() == {"status": "unavailable"}
    assert unavailable_client.node_writes == []


# --- /graph/write/relationship --------------------------------------------

def test_graph_write_relationship_ok(fake_client):
    r = _client().post(
        "/graph/write/relationship",
        json={
            "source_label": "Account",
            "source_key": "A-1",
            "rel_type": "HAS_CONTACT",
            "target_label": "Contact",
            "target_key": "C-1",
        },
    )
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert fake_client.rel_writes == [("Account", "A-1", "HAS_CONTACT", "Contact", "C-1")]


def test_graph_write_relationship_rejects_disallowed_triple(fake_client):
    r = _client().post(
        "/graph/write/relationship",
        json={
            "source_label": "Account",
            "source_key": "A-1",
            "rel_type": "BOGUS",
            "target_label": "Contact",
            "target_key": "C-1",
        },
    )
    assert r.status_code == 422
    assert "not allowed" in r.json()["detail"]
    assert fake_client.rel_writes == []


def test_graph_write_relationship_unavailable(unavailable_client):
    r = _client().post(
        "/graph/write/relationship",
        json={
            "source_label": "Account",
            "source_key": "A-1",
            "rel_type": "HAS_CONTACT",
            "target_label": "Contact",
            "target_key": "C-1",
        },
    )
    assert r.status_code == 200
    assert r.json() == {"status": "unavailable"}


# --- /graph/query ----------------------------------------------------------

def test_graph_query_ok(fake_client):
    fake_client.query_rows = [{"a": {"account_id": "A-1"}}]
    r = _client().post(
        "/graph/query",
        json={"name": "account_by_id", "params": {"account_id": "A-1"}},
    )
    assert r.status_code == 200
    assert r.json() == {"rows": [{"a": {"account_id": "A-1"}}]}
    assert fake_client.queries == [("account_by_id", {"account_id": "A-1"})]


def test_graph_query_rejects_unknown_template(fake_client):
    r = _client().post(
        "/graph/query",
        json={"name": "not_a_real_query", "params": {}},
    )
    assert r.status_code == 422
    assert "not in allowlist" in r.json()["detail"]
    assert fake_client.queries == []


def test_graph_query_unavailable(unavailable_client):
    r = _client().post(
        "/graph/query",
        json={"name": "account_by_id", "params": {"account_id": "A-1"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "unavailable"
    assert body["rows"] == []


# --- capability check ------------------------------------------------------

def test_graph_capability_exists():
    from backend.common.capabilities import SERVICE_CAPABILITIES
    assert "graph" in SERVICE_CAPABILITIES["memory"]
