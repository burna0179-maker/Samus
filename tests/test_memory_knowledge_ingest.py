"""Tests for backend.memory.knowledge_ingest — all run in-memory with mocked callables.

No Neo4j connection or LM Studio required; graph_writer, embedder, dlq_writer, and
signature_verifier are injected as mock callables.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.memory.knowledge_ingest import (
    IngestRequest,
    KnowledgeIngestPod,
    TrustLevel,
    _chunk_text,
    _hash_content,
    _validate_doc,
)


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


def test_validate_doc_requires_id_and_document():
    assert _validate_doc({"id": "x", "document": "text"}) is True
    assert _validate_doc({"id": "x"}) is False
    assert _validate_doc({"document": "text"}) is False
    assert _validate_doc({}) is False


def test_validate_doc_allows_extra_keys():
    assert _validate_doc({"id": "x", "document": "t", "extra": 123}) is True


def test_chunk_text_respects_size_and_overlap():
    text = "A" * 10
    chunks = _chunk_text(text, chunk_size=6, chunk_overlap=2)
    # stride = 4; windows: [0:6], [4:10], [8:14] (truncated to 2 chars)
    assert chunks[0] == "A" * 6
    assert chunks[1] == "A" * 6
    assert chunks[2] == "A" * 2


def test_chunk_text_single_chunk_when_text_shorter_than_size():
    chunks = _chunk_text("hello", chunk_size=100, chunk_overlap=10)
    assert chunks == ["hello"]


def test_chunk_text_empty_string_returns_empty_list():
    assert _chunk_text("", chunk_size=100, chunk_overlap=10) == []


def test_hash_content_stable():
    h1 = _hash_content("hello world")
    h2 = _hash_content("hello world")
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_hash_content_differs_for_different_text():
    assert _hash_content("foo") != _hash_content("bar")


# ---------------------------------------------------------------------------
# Pod construction helpers
# ---------------------------------------------------------------------------


def _make_pod() -> tuple[KnowledgeIngestPod, list[dict]]:
    """Return (pod, write_calls_list) with a mock graph_writer tracking writes."""
    write_calls: list[dict] = []
    pod = KnowledgeIngestPod(graph_writer=lambda rec: write_calls.append(dict(rec)))
    return pod, write_calls


# ---------------------------------------------------------------------------
# handle() tests
# ---------------------------------------------------------------------------


def test_handle_empty_documents_returns_zero_receipt():
    pod, _ = _make_pod()
    req = IngestRequest(documents=[])
    receipt = pod.handle(req)
    assert receipt.docs_processed == 0
    assert receipt.chunks_indexed == 0
    assert receipt.duplicates_skipped == 0
    assert receipt.failures == []


def test_handle_inline_documents_writes_via_graph_writer():
    pod, calls = _make_pod()
    req = IngestRequest(
        documents=[{"id": "doc1", "document": "Hello Neo4j"}],
    )
    receipt = pod.handle(req)
    assert receipt.docs_processed == 1
    assert receipt.chunks_indexed == 1
    assert len(calls) == 1
    rec = calls[0]
    assert rec["parent_id"] == "doc1"
    assert rec["chunk_id"] == "doc1_chunk_0"
    assert "Hello Neo4j" in rec["document"]
    assert rec["trust_level"] == "internal"
    assert rec["collection_label"] == "samus_knowledge"


def test_handle_chunks_long_doc_into_multiple_writes():
    pod, calls = _make_pod()
    long_text = "X" * 1000
    req = IngestRequest(
        documents=[{"id": "big", "document": long_text}],
        chunk_size=800,
        chunk_overlap=100,
    )
    receipt = pod.handle(req)
    # stride=700; chunks at 0, 700 → 2 chunks
    assert receipt.chunks_indexed == 2
    assert len(calls) == 2
    assert calls[0]["chunk_id"] == "big_chunk_0"
    assert calls[1]["chunk_id"] == "big_chunk_1"
    assert calls[0]["chunk_index"] == 0
    assert calls[1]["chunk_index"] == 1


def test_handle_dedupe_by_content_hash_skips_duplicate_doc():
    pod, calls = _make_pod()
    doc = {"id": "d1", "document": "same text"}
    req = IngestRequest(documents=[doc, {"id": "d2", "document": "same text"}])
    receipt = pod.handle(req)
    # Second doc has same hash → skipped
    assert receipt.docs_processed == 1
    assert receipt.duplicates_skipped == 1
    assert receipt.chunks_indexed == 1


def test_handle_dry_run_no_writes():
    write_calls: list[dict] = []
    pod = KnowledgeIngestPod(graph_writer=lambda rec: write_calls.append(rec))
    req = IngestRequest(
        documents=[{"id": "d1", "document": "some content"}],
        dry_run=True,
    )
    receipt = pod.handle(req)
    # dry_run: no actual graph writes
    assert len(write_calls) == 0
    # But receipt still counts chunks
    assert receipt.chunks_indexed == 1
    assert receipt.dry_run is True


def test_handle_invalid_doc_recorded_in_failures():
    pod, calls = _make_pod()
    req = IngestRequest(
        documents=[
            {"id": "good", "document": "valid doc"},
            {"missing_id": True, "document": "no id key"},  # invalid: no 'id'
        ]
    )
    receipt = pod.handle(req)
    assert receipt.docs_processed == 1  # only valid doc processed
    assert len(receipt.failures) == 1
    assert receipt.failures[0]["chunk_id"] is None
    assert "invalid_doc" in receipt.failures[0]["error"]


def test_handle_verified_without_signature_raises_permission_error():
    pod, _ = _make_pod()
    req = IngestRequest(
        documents=[{"id": "d", "document": "text"}],
        trust_level=TrustLevel.VERIFIED,
        signature=None,
    )
    with pytest.raises(PermissionError, match="signature"):
        pod.handle(req)


def test_handle_verified_with_signature_calls_verifier():
    verified_calls: list[tuple] = []

    def mock_verifier(doc_bytes: bytes, sig: str) -> bool:
        verified_calls.append((doc_bytes, sig))
        return True

    pod = KnowledgeIngestPod(
        graph_writer=lambda rec: None,
        signature_verifier=mock_verifier,
    )
    req = IngestRequest(
        documents=[{"id": "d", "document": "text"}],
        trust_level=TrustLevel.VERIFIED,
        signature="valid-sig",
    )
    receipt = pod.handle(req)
    assert len(verified_calls) == 1
    assert verified_calls[0][1] == "valid-sig"
    assert receipt.docs_processed == 1


def test_handle_verified_signature_failure_raises_permission_error():
    pod = KnowledgeIngestPod(
        graph_writer=lambda rec: None,
        signature_verifier=lambda b, s: False,  # always fails
    )
    req = IngestRequest(
        documents=[{"id": "d", "document": "text"}],
        trust_level=TrustLevel.VERIFIED,
        signature="bad-sig",
    )
    with pytest.raises(PermissionError, match="verification failed"):
        pod.handle(req)


def test_handle_embedder_called_when_embed_true():
    embed_calls: list[str] = []

    def mock_embedder(text: str) -> list[float]:
        embed_calls.append(text)
        return [0.1, 0.2, 0.3]

    write_calls: list[dict] = []
    pod = KnowledgeIngestPod(
        graph_writer=lambda rec: write_calls.append(dict(rec)),
        embedder=mock_embedder,
    )
    req = IngestRequest(
        documents=[{"id": "d", "document": "embed me"}],
        embed=True,
    )
    receipt = pod.handle(req)
    assert len(embed_calls) == 1
    assert embed_calls[0] == "embed me"
    assert write_calls[0]["embedding"] == [0.1, 0.2, 0.3]
    assert receipt.chunks_indexed == 1


def test_handle_embedder_skipped_when_embed_false():
    embed_calls: list[str] = []

    def mock_embedder(text: str) -> list[float]:
        embed_calls.append(text)
        return [0.0]

    write_calls: list[dict] = []
    pod = KnowledgeIngestPod(
        graph_writer=lambda rec: write_calls.append(dict(rec)),
        embedder=mock_embedder,
    )
    req = IngestRequest(
        documents=[{"id": "d", "document": "no embed"}],
        embed=False,
    )
    pod.handle(req)
    assert embed_calls == []
    assert "embedding" not in write_calls[0]


def test_handle_graph_writer_exception_recorded_in_failures_and_dlq():
    dlq_calls: list[dict] = []

    def failing_writer(rec: dict) -> None:
        raise RuntimeError("neo4j_down")

    def dlq_sink(rec: dict) -> None:
        dlq_calls.append(dict(rec))

    pod = KnowledgeIngestPod(
        graph_writer=failing_writer,
        dlq_writer=dlq_sink,
    )
    req = IngestRequest(
        documents=[{"id": "d", "document": "will fail"}],
    )
    receipt = pod.handle(req)
    assert receipt.chunks_indexed == 0
    assert len(receipt.failures) == 1
    assert "neo4j_down" in receipt.failures[0]["error"]
    assert receipt.failures[0]["chunk_id"] == "d_chunk_0"
    # DLQ received the chunk record
    assert len(dlq_calls) == 1
    assert dlq_calls[0]["chunk_id"] == "d_chunk_0"


def test_handle_source_path_loads_json_file(tmp_path: Path, monkeypatch):
    # source_path ingest is jailed under SAMUS_KNOWLEDGE_INGEST_ROOT; point the
    # root at tmp_path so the seed file resolves inside the jail.
    monkeypatch.setenv("SAMUS_KNOWLEDGE_INGEST_ROOT", str(tmp_path))
    docs = [{"id": "f1", "document": "from file"}]
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(docs), encoding="utf-8")

    write_calls: list[dict] = []
    pod = KnowledgeIngestPod(graph_writer=lambda rec: write_calls.append(dict(rec)))
    req = IngestRequest(source_path=seed)
    receipt = pod.handle(req)
    assert receipt.docs_processed == 1
    assert write_calls[0]["parent_id"] == "f1"


def test_handle_source_path_traversal_rejected(tmp_path: Path, monkeypatch):
    """A source_path escaping the ingest root is rejected (CWE-22)."""
    monkeypatch.setenv("SAMUS_KNOWLEDGE_INGEST_ROOT", str(tmp_path / "jail"))
    # A file deliberately outside the jail root.
    outside = tmp_path / "secret.json"
    outside.write_text(json.dumps([{"id": "x", "document": "leak"}]), encoding="utf-8")

    pod = KnowledgeIngestPod(graph_writer=lambda rec: None)
    req = IngestRequest(source_path=outside)
    with pytest.raises(ValueError, match="escapes the knowledge ingest root"):
        pod.handle(req)


def test_handle_no_source_raises_value_error():
    pod, _ = _make_pod()
    req = IngestRequest()  # both source_path and documents are None
    with pytest.raises(ValueError, match="must provide source_path or documents"):
        pod.handle(req)


# ---------------------------------------------------------------------------
# Router test
# ---------------------------------------------------------------------------


def test_get_router_returns_fastapi_router_when_available():
    pod, _ = _make_pod()
    router = pod.get_router()
    # fastapi is installed in Samus venv; router must not be None
    assert router is not None
    # Check at least one route is registered
    routes = [r.path for r in router.routes]
    assert any("/ingest" in p for p in routes)


def test_get_router_ingest_endpoint_dry_run():
    """FastAPI TestClient smoke: POST /api/knowledge/ingest with dry_run=True."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    write_calls: list[dict] = []
    pod = KnowledgeIngestPod(graph_writer=lambda rec: write_calls.append(dict(rec)))
    mini_app = FastAPI()
    router = pod.get_router()
    mini_app.include_router(router)

    client = TestClient(mini_app)
    resp = client.post(
        "/api/knowledge/ingest",
        json={
            "documents": [{"id": "t1", "document": "test doc"}],
            "dry_run": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["docs_processed"] == 1
    assert body["chunks_indexed"] == 1
    assert body["dry_run"] is True
    assert write_calls == []  # no actual writes on dry_run
