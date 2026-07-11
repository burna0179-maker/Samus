"""Tests for backend.common.graph_client — no real Neo4j required.

The neo4j Python driver is monkeypatched: ``GraphDatabase.driver`` returns a
fake driver whose .session().run() captures the Cypher + params for asserts.
"""
from __future__ import annotations

from typing import Any

import pytest

from backend.common import graph_client as gc_mod
from backend.common.graph_client import GraphClient


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []

    def __iter__(self):
        for r in self._rows:
            yield _FakeRecord(r)


class _FakeRecord:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def data(self) -> dict[str, Any]:
        return dict(self._data)


class _FakeSession:
    def __init__(self, driver: "_FakeDriver") -> None:
        self._driver = driver

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def run(self, cypher: str, **params: Any) -> _FakeResult:
        self._driver.calls.append((cypher, params))
        return _FakeResult(self._driver.next_rows)


class _FakeDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.next_rows: list[dict[str, Any]] = []
        self.closed = False
        self.verify_called = False

    def verify_connectivity(self) -> None:
        self.verify_called = True

    def session(self, *, database: str | None = None) -> _FakeSession:
        self.last_database = database
        return _FakeSession(self)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_driver(monkeypatch):
    """Patch neo4j.GraphDatabase.driver to return a _FakeDriver."""
    import neo4j

    driver = _FakeDriver()

    def _factory(uri, auth=None, **_kw):
        return driver

    monkeypatch.setattr(neo4j.GraphDatabase, "driver", _factory)
    yield driver
    gc_mod.reset_client()


@pytest.fixture
def unavailable_driver(monkeypatch):
    """Patch the driver to raise ServiceUnavailable on verify_connectivity()."""
    import neo4j
    from neo4j.exceptions import ServiceUnavailable

    class _BrokenDriver:
        def verify_connectivity(self):
            raise ServiceUnavailable("simulated outage")

        def session(self, *, database=None):  # pragma: no cover - never reached
            raise ServiceUnavailable("nope")

        def close(self):
            pass

    monkeypatch.setattr(neo4j.GraphDatabase, "driver", lambda *a, **kw: _BrokenDriver())
    yield
    gc_mod.reset_client()


# --- happy path ------------------------------------------------------------

def test_available_true_when_driver_connects(fake_driver):
    client = GraphClient(password="x")
    assert client.available is True
    assert fake_driver.verify_called is True


def test_write_node_issues_merge_with_pk(fake_driver):
    client = GraphClient(password="x")
    client.write_node("Account", {"account_id": "A-1", "name": "Acme"})

    assert len(fake_driver.calls) == 1
    cypher, params = fake_driver.calls[0]
    assert "MERGE (n:Account" in cypher
    assert "account_id: $pk_value" in cypher
    assert params["pk_value"] == "A-1"
    assert params["properties"]["name"] == "Acme"


def test_write_node_validates_before_running(fake_driver):
    client = GraphClient(password="x")
    with pytest.raises(ValueError, match="unknown label"):
        client.write_node("NoSuchLabel", {"id": "x"})
    assert fake_driver.calls == []


def test_write_relationship_validates_triple_first(fake_driver):
    client = GraphClient(password="x")
    with pytest.raises(ValueError, match="not allowed"):
        client.write_relationship("Account", "A-1", "BOGUS", "Contact", "C-1")
    assert fake_driver.calls == []


def test_write_relationship_issues_match_merge(fake_driver):
    client = GraphClient(password="x")
    client.write_relationship("Account", "A-1", "HAS_CONTACT", "Contact", "C-1")

    assert len(fake_driver.calls) == 1
    cypher, params = fake_driver.calls[0]
    assert "MATCH (s:Account" in cypher
    assert "(t:Contact" in cypher
    assert "MERGE (s)-[r:HAS_CONTACT]->(t)" in cypher
    assert params == {"source_key": "A-1", "target_key": "C-1"}


def test_query_routes_through_allowlist(fake_driver):
    fake_driver.next_rows = [{"a": {"account_id": "A-1"}}]
    client = GraphClient(password="x")

    rows = client.query("account_by_id", account_id="A-1")

    assert rows == [{"a": {"account_id": "A-1"}}]
    cypher, params = fake_driver.calls[0]
    assert "MATCH (a:Account" in cypher
    assert params == {"account_id": "A-1"}


def test_query_rejects_unknown_template(fake_driver):
    client = GraphClient(password="x")
    with pytest.raises(ValueError, match="not in allowlist"):
        client.query("not_a_real_query")
    assert fake_driver.calls == []


def test_init_schema_creates_one_index_per_entry(fake_driver):
    from backend.common import graph_schema

    client = GraphClient(password="x")
    ok = client.init_schema()

    assert ok is True
    assert len(fake_driver.calls) == len(graph_schema.INDEXES)
    for (label, prop), (cypher, _params) in zip(graph_schema.INDEXES, fake_driver.calls):
        assert f"FOR (n:{label})" in cypher
        assert f"ON (n.{prop})" in cypher


def test_close_releases_driver(fake_driver):
    client = GraphClient(password="x")
    _ = client.available  # force connect
    client.close()
    assert fake_driver.closed is True


def test_get_client_returns_singleton(fake_driver):
    c1 = gc_mod.get_client()
    c2 = gc_mod.get_client()
    assert c1 is c2


# --- unavailability paths --------------------------------------------------

def test_unavailable_when_service_unreachable(unavailable_driver):
    client = GraphClient(password="x", required=False)
    assert client.available is False


def test_writes_are_noop_when_unavailable(unavailable_driver):
    client = GraphClient(password="x", required=False)
    # validate_node still runs (raises on bad input), but on valid input + no
    # driver, write_node simply returns False without raising.
    result = client.write_node("Account", {"account_id": "A-1"})
    assert result is False


def test_query_returns_empty_when_unavailable(unavailable_driver):
    client = GraphClient(password="x", required=False)
    rows = client.query("account_by_id", account_id="A-1")
    assert rows == []


def test_init_schema_returns_false_when_unavailable(unavailable_driver):
    client = GraphClient(password="x", required=False)
    assert client.init_schema() is False


def test_required_mode_raises_on_unreachable(unavailable_driver):
    from neo4j.exceptions import ServiceUnavailable

    client = GraphClient(password="x", required=True)
    with pytest.raises(ServiceUnavailable):
        _ = client.available  # triggers connect attempt


def test_session_yields_none_when_unavailable(unavailable_driver):
    client = GraphClient(password="x", required=False)
    with client.session() as sess:
        assert sess is None


# --- W-4: knowledge-graph tiering ------------------------------------------


def test_tier_mode_defaults_off(fake_driver):
    client = GraphClient(password="x")
    assert client.tier_mode == "off"
    assert client.tier_enabled is False


def test_tier_mode_label_when_env_set(fake_driver, monkeypatch):
    from backend.common.settings import reload_settings

    monkeypatch.setenv("SAMUS_KG_TIER_MODE", "label")
    reload_settings()
    client = GraphClient(password="x")
    assert client.tier_mode == "label"
    assert client.tier_enabled is True


def test_promote_node_sets_hivemind_tier(fake_driver):
    fake_driver.next_rows = [{"n": {"chunk_id": "c-1"}}]
    client = GraphClient(password="x")

    ok = client.promote_node("KnowledgeChunk", "c-1", key_property="chunk_id")

    assert ok is True
    cypher, params = fake_driver.calls[0]
    assert "MATCH (n:KnowledgeChunk {chunk_id: $key_value})" in cypher
    assert "SET n.tier = $tier" in cypher
    assert params == {"key_value": "c-1", "tier": "hivemind"}


def test_promote_node_uses_schema_primary_key_by_default(fake_driver):
    fake_driver.next_rows = [{"n": {"account_id": "A-1"}}]
    client = GraphClient(password="x")

    client.promote_node("Account", "A-1")

    cypher, _params = fake_driver.calls[0]
    assert "MATCH (n:Account {account_id: $key_value})" in cypher


def test_promote_node_false_when_no_node_matched(fake_driver):
    fake_driver.next_rows = []  # MATCH found nothing
    client = GraphClient(password="x")
    assert client.promote_node("Account", "missing") is False


def test_promote_node_unknown_label_raises(fake_driver):
    client = GraphClient(password="x")
    with pytest.raises(ValueError, match="unknown label"):
        client.promote_node("NoSuchLabel", "x")  # primary_key() rejects it
    assert fake_driver.calls == []


def test_promote_node_rejects_unsafe_label(fake_driver):
    client = GraphClient(password="x")
    with pytest.raises(ValueError, match="unsafe node label"):
        client.promote_node(
            "Account) DETACH DELETE n //", "x", key_property="account_id",
        )
    assert fake_driver.calls == []


def test_promote_node_rejects_unsafe_key_property(fake_driver):
    client = GraphClient(password="x")
    with pytest.raises(ValueError, match="unsafe key property"):
        client.promote_node("Account", "x", key_property="id} REMOVE n //")
    assert fake_driver.calls == []


def test_promote_node_noop_when_unavailable(unavailable_driver):
    client = GraphClient(password="x", required=False)
    assert client.promote_node("Account", "A-1") is False


def test_nodes_in_tier_queries_by_tier_and_label(fake_driver):
    fake_driver.next_rows = [{"n": {"chunk_id": "c-1"}}]
    client = GraphClient(password="x")

    rows = client.nodes_in_tier("hivemind", label="KnowledgeChunk", limit=5)

    assert rows == [{"n": {"chunk_id": "c-1"}}]
    cypher, params = fake_driver.calls[0]
    assert "MATCH (n:KnowledgeChunk) WHERE n.tier = $tier" in cypher
    assert params == {"tier": "hivemind", "limit": 5}


def test_nodes_in_tier_without_label_matches_any_node(fake_driver):
    client = GraphClient(password="x")
    client.nodes_in_tier("private")
    cypher, _params = fake_driver.calls[0]
    assert "MATCH (n) WHERE n.tier = $tier" in cypher


def test_nodes_in_tier_rejects_unknown_tier(fake_driver):
    client = GraphClient(password="x")
    with pytest.raises(ValueError, match="unknown tier"):
        client.nodes_in_tier("bogus")
    assert fake_driver.calls == []


def test_nodes_in_tier_rejects_unsafe_label(fake_driver):
    client = GraphClient(password="x")
    with pytest.raises(ValueError, match="unsafe node label"):
        client.nodes_in_tier("hivemind", label="X) DETACH DELETE n //")
    assert fake_driver.calls == []


def test_nodes_in_tier_empty_when_unavailable(unavailable_driver):
    client = GraphClient(password="x", required=False)
    assert client.nodes_in_tier("hivemind") == []
