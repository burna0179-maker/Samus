"""Bounded ChromaDB vector layer for semantic retrieval (Phase-3 S8).

A thin, self-contained wrapper around a ChromaDB collection (create / upsert /
query) behind a small backend-agnostic interface. This is the SEMANTIC-SEARCH
sibling of ``backend.memory.knowledge_ingest`` (which writes chunk text to the
Neo4j graph): the same chunk corpus can additionally be mirrored into a vector
collection so callers can do similarity retrieval (prospect / CRM / knowledge
nearest-neighbour) instead of exact graph lookups.

Doctrine (mirrors the rest of the Samus stack):

- **Flag-gated, default-OFF.** ``SAMUS_VECTOR_STORE_ENABLED`` (Settings field
  ``vector_store_enabled``) defaults False. When OFF, :class:`VectorStore` is a
  no-op: ``upsert`` reports ``skipped`` and ``query`` returns an empty result.
  Nothing imports chromadb on the flag-off path.

- **Keyless / serverless-safe degrade.** chromadb is an OPTIONAL dependency and
  is NOT imported at module top. The import is guarded and only attempted the
  first time a configured-and-enabled store actually needs the backend. If the
  package is missing, or a configured backend is unreachable, the layer
  *fails closed* — it returns a structured degraded result and never raises out
  to the caller for backend faults (so a vector outage can never take down a
  dispatch path that consults it).

- **Injectable backend.** The concrete ChromaDB client is reached through the
  :class:`VectorBackend` protocol. Tests inject a fake backend; production
  resolves the real :class:`_ChromaBackend` lazily. The real backend is never
  exercised in the test suite (chromadb is not in the venv) — wiring the real
  backend requires adding ``chromadb`` to requirements.txt.

This unit deliberately does NOT wire the store into any live dispatch decision;
it is the layer + interface + tests only. Consumers are a follow-up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

_LOG = logging.getLogger("samus.memory.vector_store")


# ---------------------------------------------------------------------------
# Result / status types
# ---------------------------------------------------------------------------


class VectorStatus:
    """String status codes returned in result objects (stable for callers)."""

    OK = "ok"
    SKIPPED = "skipped"  # flag-off / nothing to do — benign no-op
    DEGRADED = "degraded"  # configured but backend missing/unreachable


@dataclass
class UpsertResult:
    status: str = VectorStatus.SKIPPED
    upserted: int = 0
    detail: str = ""


@dataclass
class QueryHit:
    id: str
    document: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    distance: float | None = None


@dataclass
class QueryResult:
    status: str = VectorStatus.SKIPPED
    hits: list[QueryHit] = field(default_factory=list)
    detail: str = ""


# ---------------------------------------------------------------------------
# Backend protocol — the seam tests inject a fake through
# ---------------------------------------------------------------------------


@runtime_checkable
class VectorBackend(Protocol):
    """Minimal collection interface the VectorStore needs.

    Mirrors the subset of the chromadb ``Collection`` API used here. A backend
    method MAY raise; :class:`VectorStore` catches and converts to a degraded
    result so callers are insulated from backend faults.
    """

    def upsert(
        self,
        *,
        ids: list[str],
        documents: list[str] | None,
        metadatas: list[dict[str, Any]] | None,
        embeddings: list[list[float]] | None,
    ) -> None: ...

    def query(
        self,
        *,
        query_texts: list[str] | None,
        query_embeddings: list[list[float]] | None,
        n_results: int,
    ) -> dict[str, Any]: ...

    def count(self) -> int: ...


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VectorStoreConfig:
    """Resolved configuration for a :class:`VectorStore`.

    ``host``/``port`` (when both set) select the chromadb HTTP client; otherwise
    the persistent on-disk client at ``path`` is used.
    """

    enabled: bool = False
    collection: str = "samus_knowledge"
    path: str = "/opt/samus/data/vectorstore"
    host: str = ""
    port: int = 0

    @classmethod
    def from_settings(cls, settings: Any | None = None) -> "VectorStoreConfig":
        """Build from a Settings instance (or the cached singleton)."""
        if settings is None:
            from backend.common.config import get_settings

            settings = get_settings()
        return cls(
            enabled=bool(getattr(settings, "vector_store_enabled", False)),
            collection=str(getattr(settings, "vector_store_collection", "samus_knowledge")),
            path=str(getattr(settings, "vector_store_path", "/opt/samus/data/vectorstore")),
            host=str(getattr(settings, "vector_store_host", "") or ""),
            port=int(getattr(settings, "vector_store_port", 0) or 0),
        )


# ---------------------------------------------------------------------------
# Real ChromaDB backend — lazily imported, never at module top
# ---------------------------------------------------------------------------


def _resolve_chroma_backend(config: VectorStoreConfig) -> VectorBackend:
    """Construct the real chromadb-backed collection wrapper.

    Imports chromadb lazily. Raises ``RuntimeError('chromadb_unavailable')`` if
    the package is missing or the backend cannot be reached — the caller
    (:class:`VectorStore`) converts that into a degraded result.
    """
    try:
        import os

        # chromadb phones home by default; opt out before import like the
        # recovery seeder did.
        os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
        import chromadb  # type: ignore[import-not-found]
    except ImportError as exc:  # package not installed — keyless degrade
        raise RuntimeError("chromadb_unavailable") from exc

    try:
        if config.host and config.port:
            client = chromadb.HttpClient(host=config.host, port=config.port)
        else:
            client = chromadb.PersistentClient(path=config.path)
        collection = client.get_or_create_collection(config.collection)
    except Exception as exc:  # noqa: BLE001 — any backend fault → fail closed
        raise RuntimeError("chromadb_unavailable") from exc

    return _ChromaCollectionAdapter(collection)


class _ChromaCollectionAdapter:
    """Adapt a chromadb Collection to the :class:`VectorBackend` protocol.

    chromadb rejects ``None`` kwargs for some versions; this adapter only passes
    the arguments that are actually populated.
    """

    def __init__(self, collection: Any) -> None:
        self._c = collection

    def upsert(
        self,
        *,
        ids: list[str],
        documents: list[str] | None,
        metadatas: list[dict[str, Any]] | None,
        embeddings: list[list[float]] | None,
    ) -> None:
        kwargs: dict[str, Any] = {"ids": ids}
        if documents is not None:
            kwargs["documents"] = documents
        if metadatas is not None:
            kwargs["metadatas"] = metadatas
        if embeddings is not None:
            kwargs["embeddings"] = embeddings
        self._c.upsert(**kwargs)

    def query(
        self,
        *,
        query_texts: list[str] | None,
        query_embeddings: list[list[float]] | None,
        n_results: int,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"n_results": n_results}
        if query_embeddings is not None:
            kwargs["query_embeddings"] = query_embeddings
        elif query_texts is not None:
            kwargs["query_texts"] = query_texts
        return self._c.query(**kwargs)

    def count(self) -> int:
        return int(self._c.count())


# ---------------------------------------------------------------------------
# VectorStore — the public layer
# ---------------------------------------------------------------------------


class VectorStore:
    """Flag-gated, fail-closed vector store wrapper.

    Construct via :meth:`from_settings` for production wiring, or pass an
    explicit ``backend`` (a :class:`VectorBackend`) for tests / custom backends.
    All public methods are non-raising for backend faults: they return a
    result object carrying a :class:`VectorStatus`.
    """

    def __init__(
        self,
        config: VectorStoreConfig | None = None,
        *,
        backend: VectorBackend | None = None,
    ) -> None:
        self._config = config or VectorStoreConfig()
        # An explicitly injected backend wins (tests / custom wiring) and marks
        # the store as already-resolved so the lazy chromadb path is never hit.
        self._backend: VectorBackend | None = backend
        self._resolved = backend is not None

    @classmethod
    def from_settings(cls, settings: Any | None = None) -> "VectorStore":
        return cls(VectorStoreConfig.from_settings(settings))

    # --- properties ------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self._config.enabled)

    @property
    def config(self) -> VectorStoreConfig:
        return self._config

    # --- backend resolution ---------------------------------------------

    def _get_backend(self) -> VectorBackend | None:
        """Resolve the backend lazily, fail-closed.

        Returns ``None`` (never raises) when the backend can't be constructed
        — the package is missing or unreachable. Resolution is attempted at
        most once; a failed attempt is sticky for this instance's lifetime so a
        hot path never re-pays an import/connect cost on every call.
        """
        if self._resolved:
            return self._backend
        self._resolved = True
        try:
            self._backend = _resolve_chroma_backend(self._config)
        except RuntimeError as exc:
            _LOG.warning("vector_store backend unavailable (failing closed): %s", exc)
            self._backend = None
        return self._backend

    # --- public API ------------------------------------------------------

    def upsert(
        self,
        *,
        ids: list[str],
        documents: list[str] | None = None,
        metadatas: list[dict[str, Any]] | None = None,
        embeddings: list[list[float]] | None = None,
    ) -> UpsertResult:
        """Upsert vectors/documents. No-op when the flag is OFF.

        Either ``documents`` (Chroma embeds them with its default embedder) or
        ``embeddings`` should be supplied; both may be. Lengths must match
        ``ids``. A backend fault returns a ``DEGRADED`` result rather than
        raising.
        """
        if not self._config.enabled:
            return UpsertResult(status=VectorStatus.SKIPPED, detail="vector_store_disabled")
        if not ids:
            return UpsertResult(status=VectorStatus.SKIPPED, detail="empty_ids")

        self._validate_lengths(ids, documents, metadatas, embeddings)

        backend = self._get_backend()
        if backend is None:
            return UpsertResult(status=VectorStatus.DEGRADED, detail="backend_unavailable")

        try:
            backend.upsert(
                ids=ids,
                documents=documents,
                metadatas=metadatas,
                embeddings=embeddings,
            )
        except Exception as exc:  # noqa: BLE001 — insulate callers from faults
            _LOG.warning("vector_store upsert failed (failing closed): %s", exc)
            return UpsertResult(status=VectorStatus.DEGRADED, detail=f"upsert_error: {exc}")

        return UpsertResult(status=VectorStatus.OK, upserted=len(ids))

    def query(
        self,
        *,
        query_texts: list[str] | None = None,
        query_embeddings: list[list[float]] | None = None,
        n_results: int = 5,
    ) -> QueryResult:
        """Similarity query. Returns an empty ``SKIPPED`` result when OFF.

        Provide ``query_texts`` (Chroma embeds them) or ``query_embeddings``.
        A backend fault returns a ``DEGRADED`` result rather than raising.
        """
        if not self._config.enabled:
            return QueryResult(status=VectorStatus.SKIPPED, detail="vector_store_disabled")
        if not query_texts and not query_embeddings:
            return QueryResult(status=VectorStatus.SKIPPED, detail="no_query")
        if n_results <= 0:
            return QueryResult(status=VectorStatus.SKIPPED, detail="n_results_non_positive")

        backend = self._get_backend()
        if backend is None:
            return QueryResult(status=VectorStatus.DEGRADED, detail="backend_unavailable")

        try:
            raw = backend.query(
                query_texts=query_texts,
                query_embeddings=query_embeddings,
                n_results=n_results,
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("vector_store query failed (failing closed): %s", exc)
            return QueryResult(status=VectorStatus.DEGRADED, detail=f"query_error: {exc}")

        return QueryResult(status=VectorStatus.OK, hits=_parse_query_response(raw))

    def count(self) -> int:
        """Best-effort collection size. Returns 0 when OFF/degraded (no raise)."""
        if not self._config.enabled:
            return 0
        backend = self._get_backend()
        if backend is None:
            return 0
        try:
            return int(backend.count())
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("vector_store count failed (failing closed): %s", exc)
            return 0

    # --- helpers ---------------------------------------------------------

    @staticmethod
    def _validate_lengths(
        ids: list[str],
        documents: list[str] | None,
        metadatas: list[dict[str, Any]] | None,
        embeddings: list[list[float]] | None,
    ) -> None:
        n = len(ids)
        for label, seq in (
            ("documents", documents),
            ("metadatas", metadatas),
            ("embeddings", embeddings),
        ):
            if seq is not None and len(seq) != n:
                raise ValueError(f"{label} length {len(seq)} does not match ids length {n}")
        if documents is None and embeddings is None:
            raise ValueError("upsert requires documents or embeddings")


def _parse_query_response(raw: dict[str, Any]) -> list[QueryHit]:
    """Flatten a chromadb query response (single query) into QueryHit list.

    chromadb returns parallel lists nested one level per query text, e.g.
    ``{"ids": [["a","b"]], "documents": [["..","..]], ...}``. This unit issues
    one query at a time, so we read index 0 of each top-level list.
    """

    def _first(key: str) -> list[Any]:
        outer = raw.get(key)
        if not outer:
            return []
        inner = outer[0]
        return list(inner) if inner is not None else []

    ids = _first("ids")
    documents = _first("documents")
    metadatas = _first("metadatas")
    distances = _first("distances")

    hits: list[QueryHit] = []
    for i, _id in enumerate(ids):
        hits.append(
            QueryHit(
                id=str(_id),
                document=documents[i] if i < len(documents) else None,
                metadata=(metadatas[i] if i < len(metadatas) and metadatas[i] else {}) or {},
                distance=distances[i] if i < len(distances) else None,
            )
        )
    return hits


__all__ = [
    "VectorStatus",
    "UpsertResult",
    "QueryHit",
    "QueryResult",
    "VectorBackend",
    "VectorStoreConfig",
    "VectorStore",
]
