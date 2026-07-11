"""W-4: knowledge-graph tier convention (private / hivemind).

Covers ``SAMUS_KG_TIER_MODE`` resolution, the ``backend.memory.tiers``
helpers, the tier stamp on the default knowledge-ingest writer, and the
POST /graph/promote endpoint. No real Neo4j required — the graph client is
faked throughout.

See CLOUD_RUN_DEPLOY.md §5.2 / decision D-6 option 6b.
"""

from __future__ import annotations

from typing import Any

from backend.common.settings import reload_settings


# --- SAMUS_KG_TIER_MODE resolution -----------------------------------------


def test_kg_tier_mode_defaults_off(monkeypatch):
    monkeypatch.delenv("SAMUS_KG_TIER_MODE", raising=False)
    assert reload_settings().kg_tier_mode == "off"


def test_kg_tier_mode_label(monkeypatch):
    monkeypatch.setenv("SAMUS_KG_TIER_MODE", "label")
    assert reload_settings().kg_tier_mode == "label"


def test_kg_tier_mode_unrecognised_falls_back_to_off(monkeypatch):
    monkeypatch.setenv("SAMUS_KG_TIER_MODE", "bogus")
    assert reload_settings().kg_tier_mode == "off"


# --- tiers.stamp_default_tier ----------------------------------------------


def test_stamp_default_tier_is_noop_when_mode_off(monkeypatch):
    from backend.memory import tiers

    monkeypatch.setenv("SAMUS_KG_TIER_MODE", "off")
    reload_settings()
    record = {"chunk_id": "c-1"}
    out = tiers.stamp_default_tier(record)
    assert out == {"chunk_id": "c-1"}
    assert "tier" not in out


def test_stamp_default_tier_adds_private_when_label(monkeypatch):
    from backend.memory import tiers

    monkeypatch.setenv("SAMUS_KG_TIER_MODE", "label")
    reload_settings()
    record = {"chunk_id": "c-1"}
    out = tiers.stamp_default_tier(record)
    assert out == {"chunk_id": "c-1", "tier": "private"}
    # The caller's input dict is never mutated.
    assert record == {"chunk_id": "c-1"}


def test_stamp_default_tier_respects_explicit_tier(monkeypatch):
    from backend.memory import tiers

    monkeypatch.setenv("SAMUS_KG_TIER_MODE", "label")
    reload_settings()
    out = tiers.stamp_default_tier({"chunk_id": "c-1", "tier": "hivemind"})
    assert out["tier"] == "hivemind"


def test_tier_mode_and_tier_enabled_reflect_settings(monkeypatch):
    from backend.memory import tiers

    monkeypatch.setenv("SAMUS_KG_TIER_MODE", "label")
    reload_settings()
    assert tiers.tier_mode() == "label"
    assert tiers.tier_enabled() is True


# --- tiers.promote_node / list_hivemind_nodes delegate to the client -------


class _RecordingClient:
    """Captures promote_node / nodes_in_tier calls."""

    def __init__(self) -> None:
        self.promote_calls: list[tuple] = []
        self.tier_calls: list[tuple] = []

    def promote_node(self, label, key_value, *, key_property=None) -> bool:
        self.promote_calls.append((label, key_value, key_property))
        return True

    def nodes_in_tier(self, tier, *, label=None, limit=100) -> list[dict]:
        self.tier_calls.append((tier, label, limit))
        return [{"n": {"chunk_id": "c-1"}}]


def test_promote_node_helper_delegates_to_client():
    from backend.memory import tiers

    client = _RecordingClient()
    assert tiers.promote_node("Account", "A-1", client=client) is True
    assert client.promote_calls == [("Account", "A-1", None)]


# --- auto-promotion producer (verified knowledge -> hivemind tier) ----------


def test_should_auto_promote_false_when_mode_off(monkeypatch):
    from backend.memory import tiers

    monkeypatch.setenv("SAMUS_KG_TIER_MODE", "off")
    reload_settings()
    assert tiers.should_auto_promote("verified") is False


def test_should_auto_promote_only_verified_when_label(monkeypatch):
    from backend.memory import tiers

    monkeypatch.setenv("SAMUS_KG_TIER_MODE", "label")
    reload_settings()
    assert tiers.should_auto_promote("verified") is True
    assert tiers.should_auto_promote("VERIFIED") is True  # case-insensitive
    assert tiers.should_auto_promote("internal") is False
    assert tiers.should_auto_promote("external") is False
    assert tiers.should_auto_promote(None) is False


def test_auto_promote_on_ingest_promotes_verified(monkeypatch):
    from backend.memory import tiers

    monkeypatch.setenv("SAMUS_KG_TIER_MODE", "label")
    reload_settings()
    client = _RecordingClient()
    assert (
        tiers.auto_promote_on_ingest(
            "KnowledgeChunk",
            "c-1",
            "verified",
            key_property="chunk_id",
            client=client,
        )
        is True
    )
    assert client.promote_calls == [("KnowledgeChunk", "c-1", "chunk_id")]


def test_auto_promote_on_ingest_skips_internal(monkeypatch):
    from backend.memory import tiers

    monkeypatch.setenv("SAMUS_KG_TIER_MODE", "label")
    reload_settings()
    client = _RecordingClient()
    assert (
        tiers.auto_promote_on_ingest(
            "KnowledgeChunk",
            "c-1",
            "internal",
            client=client,
        )
        is False
    )
    assert client.promote_calls == []


def test_auto_promote_on_ingest_noop_when_mode_off(monkeypatch):
    from backend.memory import tiers

    monkeypatch.setenv("SAMUS_KG_TIER_MODE", "off")
    reload_settings()
    client = _RecordingClient()
    assert (
        tiers.auto_promote_on_ingest(
            "KnowledgeChunk",
            "c-1",
            "verified",
            client=client,
        )
        is False
    )
    assert client.promote_calls == []


# --- end-to-end: ingest a verified doc -> auto-promote -> hivemind listing --


class _LifecycleClient:
    """Records promotions and lists them back as hivemind-tier nodes."""

    def __init__(self) -> None:
        self.promoted: list[tuple] = []

    def promote_node(self, label, key_value, *, key_property=None) -> bool:
        self.promoted.append((label, key_value))
        return True

    def nodes_in_tier(self, tier, *, label=None, limit=100) -> list[dict]:
        return [{"chunk_id": key} for (lbl, key) in self.promoted if label in (None, lbl)]


def test_ingest_verified_auto_promotes_to_hivemind(monkeypatch):
    from backend.memory import tiers
    from backend.memory.knowledge_ingest import (
        IngestRequest,
        KnowledgeIngestPod,
        TrustLevel,
    )

    monkeypatch.setenv("SAMUS_KG_TIER_MODE", "label")
    reload_settings()
    client = _LifecycleClient()
    monkeypatch.setattr(tiers, "get_client", lambda: client)

    pod = KnowledgeIngestPod(graph_writer=lambda rec: None)
    receipt = pod.handle(
        IngestRequest(
            documents=[{"id": "doc1", "document": "authoritative fact"}],
            trust_level=TrustLevel.VERIFIED,
            signature="sig",  # VERIFIED requires a signature; no verifier -> passes
        )
    )

    assert receipt.chunks_indexed == 1
    # The verified chunk was auto-promoted...
    assert client.promoted == [("KnowledgeChunk", "doc1_chunk_0")]
    # ...and now shows up in the hivemind-tier listing.
    rows = tiers.list_hivemind_nodes(label="KnowledgeChunk")
    assert rows == [{"chunk_id": "doc1_chunk_0"}]


def test_ingest_internal_stays_private(monkeypatch):
    from backend.memory import tiers
    from backend.memory.knowledge_ingest import (
        IngestRequest,
        KnowledgeIngestPod,
        TrustLevel,
    )

    monkeypatch.setenv("SAMUS_KG_TIER_MODE", "label")
    reload_settings()
    client = _LifecycleClient()
    monkeypatch.setattr(tiers, "get_client", lambda: client)

    pod = KnowledgeIngestPod(graph_writer=lambda rec: None)
    receipt = pod.handle(
        IngestRequest(
            documents=[{"id": "doc2", "document": "an internal note"}],
            trust_level=TrustLevel.INTERNAL,
        )
    )

    assert receipt.chunks_indexed == 1
    assert client.promoted == []  # internal knowledge is not auto-promoted


def test_list_hivemind_nodes_delegates_to_client():
    from backend.memory import tiers

    client = _RecordingClient()
    rows = tiers.list_hivemind_nodes(label="KnowledgeChunk", limit=10, client=client)
    assert rows == [{"n": {"chunk_id": "c-1"}}]
    assert client.tier_calls == [("hivemind", "KnowledgeChunk", 10)]


# --- _default_neo4j_writer stamps the tier ---------------------------------


class _FakeGraphRun:
    """A graph client exposing just the ._run() the writer uses."""

    available = True

    def __init__(self) -> None:
        self.last_params: dict[str, Any] | None = None

    def _run(self, cypher: str, params: dict[str, Any]) -> list:
        self.last_params = params
        return []


def test_default_writer_stamps_private_tier_when_label(monkeypatch):
    import backend.common.graph_client as gc
    from backend.memory.knowledge_ingest import _default_neo4j_writer

    monkeypatch.setenv("SAMUS_KG_TIER_MODE", "label")
    reload_settings()
    fake = _FakeGraphRun()
    monkeypatch.setattr(gc, "get_client", lambda: fake)

    _default_neo4j_writer({"chunk_id": "c-1", "document": "hello"})

    assert fake.last_params["props"]["tier"] == "private"


def test_default_writer_omits_tier_when_off(monkeypatch):
    import backend.common.graph_client as gc
    from backend.memory.knowledge_ingest import _default_neo4j_writer

    monkeypatch.setenv("SAMUS_KG_TIER_MODE", "off")
    reload_settings()
    fake = _FakeGraphRun()
    monkeypatch.setattr(gc, "get_client", lambda: fake)

    _default_neo4j_writer({"chunk_id": "c-1", "document": "hello"})

    assert "tier" not in fake.last_params["props"]


# --- POST /graph/promote ---------------------------------------------------


class _FakePromoteClient:
    """Drop-in graph client for the /graph/promote endpoint tests."""

    def __init__(self, *, available=True, promoted=True, raises=None) -> None:
        self.available = available
        self._promoted = promoted
        self._raises = raises
        self.calls: list[tuple] = []

    def promote_node(self, label, key_value, *, key_property=None) -> bool:
        self.calls.append((label, key_value, key_property))
        if self._raises is not None:
            raise self._raises
        return self._promoted


def _memory_test_client(monkeypatch, fake):
    import backend.memory.app as app_mod
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app_mod, "_resolve_client", lambda: fake)
    return TestClient(app_mod.app)


def test_graph_promote_ok(monkeypatch):
    fake = _FakePromoteClient(promoted=True)
    client = _memory_test_client(monkeypatch, fake)

    r = client.post(
        "/graph/promote",
        json={"label": "KnowledgeChunk", "key_value": "c-1", "key_property": "chunk_id"},
    )

    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok", "promoted": True}
    assert fake.calls == [("KnowledgeChunk", "c-1", "chunk_id")]


def test_graph_promote_reports_no_match(monkeypatch):
    fake = _FakePromoteClient(promoted=False)
    client = _memory_test_client(monkeypatch, fake)

    r = client.post("/graph/promote", json={"label": "Account", "key_value": "x"})

    assert r.status_code == 200
    assert r.json() == {"status": "ok", "promoted": False}


def test_graph_promote_unavailable(monkeypatch):
    fake = _FakePromoteClient(available=False, promoted=False)
    client = _memory_test_client(monkeypatch, fake)

    r = client.post("/graph/promote", json={"label": "Account", "key_value": "A-1"})

    assert r.status_code == 200
    assert r.json() == {"status": "unavailable"}


def test_graph_promote_value_error_is_422(monkeypatch):
    fake = _FakePromoteClient(raises=ValueError("unknown label: Bogus"))
    client = _memory_test_client(monkeypatch, fake)

    r = client.post("/graph/promote", json={"label": "Bogus", "key_value": "x"})

    assert r.status_code == 422
    # detail is an opaque code (LEAK-03): no Neo4j/graph internals leaked to client
    assert r.json()["detail"] == "graph_promote_error"
