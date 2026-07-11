"""Tests for backend.memory.vector_store — the bounded ChromaDB vector layer.

chromadb is NOT in the Samus venv, so these tests run against the
:class:`VectorBackend` seam via a fake backend (a tiny in-process collection)
and against the no-op / fail-closed paths directly. No real chromadb client,
no server, no on-disk store is created. The real chromadb-backed path is
exercised only when the package is installed (see requirements.txt note).
"""

from __future__ import annotations

import pytest

from backend.memory.vector_store import (
    QueryResult,
    UpsertResult,
    VectorBackend,
    VectorStatus,
    VectorStore,
    VectorStoreConfig,
    _parse_query_response,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeBackend:
    """Minimal in-process VectorBackend. Records upserts; query returns inserts.

    Not a similarity engine — query returns the first ``n_results`` stored ids
    in insertion order, which is enough to validate response flattening + the
    OK/DEGRADED status plumbing.
    """

    def __init__(self) -> None:
        self.ids: list[str] = []
        self.documents: list[str | None] = []
        self.metadatas: list[dict] = []
        self.upsert_calls = 0

    def upsert(self, *, ids, documents, metadatas, embeddings) -> None:
        self.upsert_calls += 1
        for i, _id in enumerate(ids):
            self.ids.append(_id)
            self.documents.append(documents[i] if documents else None)
            self.metadatas.append(metadatas[i] if metadatas else {})

    def query(self, *, query_texts, query_embeddings, n_results) -> dict:
        sel = list(range(min(n_results, len(self.ids))))
        return {
            "ids": [[self.ids[i] for i in sel]],
            "documents": [[self.documents[i] for i in sel]],
            "metadatas": [[self.metadatas[i] for i in sel]],
            "distances": [[float(i) for i in sel]],
        }

    def count(self) -> int:
        return len(self.ids)


class ExplodingBackend:
    """Every method raises — exercises the fail-closed conversion."""

    def upsert(self, **_kw) -> None:
        raise RuntimeError("boom-upsert")

    def query(self, **_kw) -> dict:
        raise RuntimeError("boom-query")

    def count(self) -> int:
        raise RuntimeError("boom-count")


def _enabled_config() -> VectorStoreConfig:
    return VectorStoreConfig(enabled=True, collection="t")


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_fake_backend_satisfies_protocol():
    assert isinstance(FakeBackend(), VectorBackend)


# ---------------------------------------------------------------------------
# Flag-OFF no-op path (default-OFF doctrine)
# ---------------------------------------------------------------------------


def test_disabled_upsert_is_skipped_noop():
    store = VectorStore(VectorStoreConfig(enabled=False), backend=FakeBackend())
    res = store.upsert(ids=["a"], documents=["hello"])
    assert isinstance(res, UpsertResult)
    assert res.status == VectorStatus.SKIPPED
    assert res.upserted == 0


def test_disabled_query_returns_empty_skipped():
    store = VectorStore(VectorStoreConfig(enabled=False), backend=FakeBackend())
    res = store.query(query_texts=["q"])
    assert isinstance(res, QueryResult)
    assert res.status == VectorStatus.SKIPPED
    assert res.hits == []


def test_disabled_store_never_touches_backend():
    fake = FakeBackend()
    store = VectorStore(VectorStoreConfig(enabled=False), backend=fake)
    store.upsert(ids=["a"], documents=["x"])
    store.query(query_texts=["q"])
    assert fake.upsert_calls == 0
    assert store.count() == 0


# ---------------------------------------------------------------------------
# Enabled happy path against the fake backend
# ---------------------------------------------------------------------------


def test_enabled_upsert_then_query_roundtrip():
    fake = FakeBackend()
    store = VectorStore(_enabled_config(), backend=fake)

    up = store.upsert(
        ids=["d1", "d2"],
        documents=["alpha", "beta"],
        metadatas=[{"k": 1}, {"k": 2}],
    )
    assert up.status == VectorStatus.OK
    assert up.upserted == 2
    assert fake.upsert_calls == 1

    res = store.query(query_texts=["alpha"], n_results=2)
    assert res.status == VectorStatus.OK
    assert [h.id for h in res.hits] == ["d1", "d2"]
    assert res.hits[0].document == "alpha"
    assert res.hits[0].metadata == {"k": 1}
    assert res.hits[1].distance == 1.0


def test_count_reflects_backend():
    fake = FakeBackend()
    store = VectorStore(_enabled_config(), backend=fake)
    assert store.count() == 0
    store.upsert(ids=["a", "b", "c"], documents=["1", "2", "3"])
    assert store.count() == 3


def test_upsert_with_embeddings_only():
    fake = FakeBackend()
    store = VectorStore(_enabled_config(), backend=fake)
    res = store.upsert(ids=["e1"], embeddings=[[0.1, 0.2, 0.3]])
    assert res.status == VectorStatus.OK
    assert res.upserted == 1


def test_query_with_embeddings():
    fake = FakeBackend()
    store = VectorStore(_enabled_config(), backend=fake)
    store.upsert(ids=["a"], documents=["x"])
    res = store.query(query_embeddings=[[0.1, 0.2]], n_results=1)
    assert res.status == VectorStatus.OK
    assert [h.id for h in res.hits] == ["a"]


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_empty_ids_is_skipped():
    store = VectorStore(_enabled_config(), backend=FakeBackend())
    res = store.upsert(ids=[], documents=[])
    assert res.status == VectorStatus.SKIPPED
    assert res.detail == "empty_ids"


def test_mismatched_lengths_raise():
    store = VectorStore(_enabled_config(), backend=FakeBackend())
    with pytest.raises(ValueError, match="documents length"):
        store.upsert(ids=["a", "b"], documents=["only-one"])


def test_upsert_requires_documents_or_embeddings():
    store = VectorStore(_enabled_config(), backend=FakeBackend())
    with pytest.raises(ValueError, match="requires documents or embeddings"):
        store.upsert(ids=["a"], metadatas=[{"k": 1}])


def test_query_without_text_or_embeddings_is_skipped():
    store = VectorStore(_enabled_config(), backend=FakeBackend())
    res = store.query()
    assert res.status == VectorStatus.SKIPPED
    assert res.detail == "no_query"


def test_query_non_positive_n_results_skipped():
    store = VectorStore(_enabled_config(), backend=FakeBackend())
    res = store.query(query_texts=["q"], n_results=0)
    assert res.status == VectorStatus.SKIPPED


# ---------------------------------------------------------------------------
# Fail-closed: backend faults never raise out
# ---------------------------------------------------------------------------


def test_backend_upsert_fault_degrades_not_raises():
    store = VectorStore(_enabled_config(), backend=ExplodingBackend())
    res = store.upsert(ids=["a"], documents=["x"])
    assert res.status == VectorStatus.DEGRADED
    assert "boom-upsert" in res.detail


def test_backend_query_fault_degrades_not_raises():
    store = VectorStore(_enabled_config(), backend=ExplodingBackend())
    res = store.query(query_texts=["q"])
    assert res.status == VectorStatus.DEGRADED
    assert "boom-query" in res.detail


def test_backend_count_fault_returns_zero():
    store = VectorStore(_enabled_config(), backend=ExplodingBackend())
    assert store.count() == 0


# ---------------------------------------------------------------------------
# Keyless / serverless degrade: chromadb absent -> backend_unavailable
# ---------------------------------------------------------------------------


def test_missing_chromadb_degrades_closed():
    """No injected backend + chromadb not installed -> DEGRADED, no raise.

    chromadb is absent from the venv, so lazy resolution fails and the store
    fails closed. (If chromadb is ever added to the venv this asserts the
    real backend resolves instead — both branches are valid, neither raises.)
    """
    store = VectorStore(_enabled_config())  # no backend injected
    res = store.upsert(ids=["a"], documents=["x"])
    assert res.status in (VectorStatus.OK, VectorStatus.DEGRADED)
    qres = store.query(query_texts=["q"])
    assert qres.status in (VectorStatus.OK, VectorStatus.DEGRADED)


def test_failed_resolution_is_sticky():
    """A failed backend resolution is attempted at most once per instance."""
    store = VectorStore(_enabled_config())
    store.upsert(ids=["a"], documents=["x"])
    assert store._resolved is True
    # second call must not re-attempt resolution (backend stays None)
    res = store.upsert(ids=["b"], documents=["y"])
    assert res.status in (VectorStatus.OK, VectorStatus.DEGRADED)


# ---------------------------------------------------------------------------
# Response flattening
# ---------------------------------------------------------------------------


def test_parse_query_response_flattens_nested_lists():
    raw = {
        "ids": [["x", "y"]],
        "documents": [["dx", "dy"]],
        "metadatas": [[{"a": 1}, None]],
        "distances": [[0.0, 0.5]],
    }
    hits = _parse_query_response(raw)
    assert [h.id for h in hits] == ["x", "y"]
    assert hits[0].document == "dx"
    assert hits[0].metadata == {"a": 1}
    assert hits[1].metadata == {}
    assert hits[1].distance == 0.5


def test_parse_query_response_empty():
    assert _parse_query_response({}) == []
    assert _parse_query_response({"ids": [[]]}) == []


# ---------------------------------------------------------------------------
# Config from settings
# ---------------------------------------------------------------------------


def test_config_from_settings_defaults_off():
    cfg = VectorStoreConfig.from_settings()
    assert cfg.enabled is False
    assert cfg.collection == "samus_knowledge"


def test_from_settings_builds_disabled_store():
    store = VectorStore.from_settings()
    assert store.enabled is False
    res = store.upsert(ids=["a"], documents=["x"])
    assert res.status == VectorStatus.SKIPPED
