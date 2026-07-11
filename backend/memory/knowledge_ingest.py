"""Knowledge ingestion pod — backend-agnostic with pluggable graph_writer + embedder.

Default wiring: Hivemind/Neo4j graph layer (canonical per Samus memory workcell).
Ports SHA-256 dedupe + 800/100 chunking from recovery/seed_knowledge_v2.py without
the Chroma dependency.

Design notes:
- All callables (graph_writer, embedder, dlq_writer, signature_verifier) are injected
  at construction; the pod never imports a concrete backend at module level.
- _default_neo4j_writer uses client._run() directly (same pattern as
  backend/memory/customers.py) because KnowledgeChunk is a workcell-local node type
  not registered in backend.common.graph_schema.NODE_LABELS — adding it to the shared
  schema allowlist would require schema migration; the _run bypass is intentional and
  consistent with the Customer/StateEvent precedent.
- dry_run=True computes the full receipt without any writes (graph, dlq, or embedder).
- embed=False (default) writes chunk text only; embeddings can be backfilled later by
  a separate scheduled task.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

_LOG = logging.getLogger("samus.memory.knowledge_ingest")


# ---------------------------------------------------------------------------
# File-ingest jail (CWE-22 / CWE-862)
# ---------------------------------------------------------------------------
# ``source_path`` arrives over the HTTP surface (POST /api/knowledge/ingest and
# the /work dispatcher). It MUST NOT be able to read arbitrary host files
# (``/opt/samus/.env``, ``../../../../proc/self/environ``, …). Every file-path
# ingest is resolved against a fixed root and rejected if it escapes that root.
# Default mirrors the other Samus data roots (cf. dlq._dlq_root,
# storage._DEFAULT_ROOT) — an absolute path under the Samus data dir.


def _ingest_root() -> Path:
    """Return the resolved root that file-path ingests are jailed under."""
    root = Path(os.getenv("SAMUS_KNOWLEDGE_INGEST_ROOT", "/opt/samus/data/knowledge_ingest"))
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _jail_source_path(source_path: Path | str) -> Path:
    """Resolve ``source_path`` inside the ingest root or raise ``ValueError``.

    A relative path is taken relative to the ingest root; an absolute path is
    accepted only if it already resolves inside the ingest root. Anything that
    escapes the root (``..`` traversal, absolute paths elsewhere) is rejected.
    """
    root = _ingest_root()
    candidate = Path(source_path)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"source_path escapes the knowledge ingest root: {source_path!r}")
    return resolved


# ---------------------------------------------------------------------------
# Enums and dataclasses
# ---------------------------------------------------------------------------


class TrustLevel(str, Enum):
    VERIFIED = "verified"  # signed authority source
    INTERNAL = "internal"  # in-house generated
    EXTERNAL = "external"  # third-party / scraped


@dataclass
class IngestRequest:
    """Request shape accepted by KnowledgeIngestPod.handle()."""

    source_path: Path | None = None
    documents: list[dict] | None = None
    trust_level: TrustLevel = TrustLevel.INTERNAL
    signature: str | None = None  # HMAC/minisign (deferred verification)
    chunk_size: int = 800
    chunk_overlap: int = 100
    batch_size: int = 64
    dry_run: bool = False
    embed: bool = False
    collection_label: str = "samus_knowledge"


@dataclass
class IngestReceipt:
    """Result returned by KnowledgeIngestPod.handle()."""

    docs_processed: int = 0
    chunks_indexed: int = 0
    duplicates_skipped: int = 0
    failures: list[dict] = field(default_factory=list)  # {chunk_id, error}
    content_hashes: list[str] = field(default_factory=list)
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Pure-functional helpers (ported from recovery/seed_knowledge_v2.py)
# ---------------------------------------------------------------------------


def _hash_content(text: str) -> str:
    """Return the SHA-256 hex digest of UTF-8 encoded text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_doc(doc: dict) -> bool:
    """Return True iff doc contains both 'id' and 'document' keys."""
    return "id" in doc and "document" in doc


def _chunk_text(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """Sliding-window chunker; returns list of text slices.

    Each window is chunk_size characters; stride is chunk_size - chunk_overlap.
    The last window may be shorter than chunk_size if text length is not a
    multiple of the stride.
    """
    if not text:
        return []
    chunks: list[str] = []
    stride = max(1, chunk_size - chunk_overlap)
    start = 0
    while start < len(text):
        chunks.append(text[start : start + chunk_size])
        start += stride
    return chunks


# ---------------------------------------------------------------------------
# Default Neo4j writer — module-level fallback
# ---------------------------------------------------------------------------


def _default_neo4j_writer(chunk_record: dict) -> None:
    """Write a KnowledgeChunk node to Neo4j via the Samus graph client.

    Uses client._run() directly (same pattern as memory/customers.py) because
    KnowledgeChunk is a workcell-local label not in graph_schema.NODE_LABELS.
    The MERGE key is chunk_id; SET c += props updates all remaining fields.

    Raises RuntimeError("graph_writer_not_configured") if:
      - the neo4j package is missing, or
      - the client is not available (Hivemind unreachable).
    """
    try:
        from backend.common.graph_client import get_client
    except ImportError as exc:
        raise RuntimeError("graph_writer_not_configured") from exc

    client = get_client()
    if not client.available:
        raise RuntimeError("graph_writer_not_configured")

    chunk_id = chunk_record.get("chunk_id")
    if not chunk_id:
        raise ValueError("chunk_record must contain 'chunk_id'")

    props = {k: v for k, v in chunk_record.items() if k != "chunk_id"}
    # W-4: stamp the default knowledge-graph tier (tier='private') when
    # SAMUS_KG_TIER_MODE=label. A no-op for the local stack (mode 'off').
    from backend.memory.tiers import stamp_default_tier

    props = stamp_default_tier(props)
    # Neo4j rejects list/dict property values; serialize embedding list to JSON
    if "embedding" in props and isinstance(props["embedding"], list):
        props["embedding"] = json.dumps(props["embedding"])

    cypher = "MERGE (c:KnowledgeChunk {chunk_id: $chunk_id}) SET c += $props RETURN c"
    client._run(cypher, {"chunk_id": chunk_id, "props": props})  # noqa: SLF001


# ---------------------------------------------------------------------------
# KnowledgeIngestPod
# ---------------------------------------------------------------------------


class KnowledgeIngestPod:
    """Backend-agnostic knowledge ingestion pod.

    Callable via the Samus orchestrator /work dispatcher (action='ingest_knowledge')
    and directly via the FastAPI route POST /api/knowledge/ingest.

    All four backend callables are injected; the pod never touches a concrete
    backend at import time. Use the attach_* helpers to wire live backends at
    boot without recreating the pod instance.
    """

    POD_ID = "knowledge_ingest"

    def __init__(
        self,
        *,
        graph_writer: Callable[[dict], None] | None = None,
        embedder: Callable[[str], list[float]] | None = None,
        dlq_writer: Callable[[dict], None] | None = None,
        signature_verifier: Callable[[bytes, str], bool] | None = None,
    ) -> None:
        self._graph_writer: Callable[[dict], None] = (
            graph_writer if graph_writer is not None else _default_neo4j_writer
        )
        self._embedder: Callable[[str], list[float]] | None = embedder
        self._dlq_writer: Callable[[dict], None] | None = dlq_writer
        self._signature_verifier: Callable[[bytes, str], bool] | None = signature_verifier

    # --- late-bind hooks ----------------------------------------------------

    def attach_graph_writer(self, fn: Callable[[dict], None]) -> None:
        """Replace the graph_writer callable (e.g. swap Neo4j for a test mock)."""
        self._graph_writer = fn

    def attach_embedder(self, fn: Callable[[str], list[float]]) -> None:
        """Attach an embedder; enables embed=True paths."""
        self._embedder = fn

    def attach_dlq_writer(self, fn: Callable[[dict], None]) -> None:
        """Attach a DLQ writer; receives chunk records that failed to index."""
        self._dlq_writer = fn

    def attach_signature_verifier(self, fn: Callable[[bytes, str], bool]) -> None:
        """Attach an HMAC/minisign verifier for VERIFIED trust-level ingests."""
        self._signature_verifier = fn

    # --- core logic ---------------------------------------------------------

    def handle(self, req: IngestRequest) -> IngestReceipt:
        """Process an IngestRequest and return an IngestReceipt.

        Steps:
          1. Signature gate for VERIFIED trust level.
          2. Load documents from inline list or file path.
          3. For each doc: validate, dedupe by SHA-256 hash, chunk, write to graph.
          4. Errors per chunk are collected in receipt.failures; failed chunks
             are forwarded to dlq_writer if attached.
        """
        receipt = IngestReceipt(dry_run=req.dry_run)

        # 1. Signature gate
        if req.trust_level == TrustLevel.VERIFIED and req.signature is None:
            raise PermissionError("VERIFIED ingest requires signature")

        if (
            req.trust_level == TrustLevel.VERIFIED
            and self._signature_verifier is not None
            and req.signature is not None
        ):
            doc_bytes = json.dumps(req.documents or [], sort_keys=True).encode("utf-8")
            if not self._signature_verifier(doc_bytes, req.signature):
                raise PermissionError("VERIFIED ingest signature verification failed")

        # 2. Load documents
        if req.documents is not None:
            raw_docs = req.documents
        elif req.source_path is not None:
            # Jail the path under the fixed ingest root — file-path ingest must
            # not be able to read arbitrary host files (CWE-22).
            jailed = _jail_source_path(req.source_path)
            with open(jailed, encoding="utf-8") as fh:
                raw_docs = json.load(fh)
        else:
            raise ValueError("must provide source_path or documents")

        processed_hashes: set[str] = set()
        now_iso = datetime.now(tz=timezone.utc).isoformat()

        # 3. Per-doc loop
        for doc in raw_docs:
            if not _validate_doc(doc):
                receipt.failures.append(
                    {
                        "chunk_id": None,
                        "error": f"invalid_doc: missing id or document; got keys={list(doc.keys())}",
                    }
                )
                continue

            base_id: str = doc["id"]
            text: str = doc["document"]
            metadata: dict = doc.get("metadata") or {}
            content_hash = _hash_content(text)

            # Dedupe
            if content_hash in processed_hashes:
                receipt.duplicates_skipped += 1
                continue

            chunks = _chunk_text(text, req.chunk_size, req.chunk_overlap)

            for chunk_index, chunk_text_slice in enumerate(chunks):
                chunk_id = f"{base_id}_chunk_{chunk_index}"
                chunk_record: dict[str, Any] = {
                    "chunk_id": chunk_id,
                    "parent_id": base_id,
                    "chunk_index": chunk_index,
                    "content_hash": content_hash,
                    "document": chunk_text_slice,
                    "trust_level": req.trust_level.value,
                    "ingested_at": now_iso,
                    "collection_label": req.collection_label,
                    **metadata,
                }

                # Optional embedding
                if req.embed and self._embedder is not None:
                    try:
                        chunk_record["embedding"] = self._embedder(chunk_text_slice)
                    except Exception as exc:  # noqa: BLE001
                        _LOG.warning("embedder_failed chunk_id=%s err=%s", chunk_id, exc)
                        receipt.failures.append(
                            {"chunk_id": chunk_id, "error": f"embedder_error: {exc}"}
                        )
                        if self._dlq_writer is not None:
                            self._dlq_writer(chunk_record)
                        continue

                # Graph write (skip on dry_run)
                if not req.dry_run:
                    try:
                        self._graph_writer(chunk_record)
                        receipt.chunks_indexed += 1
                        # W-4 auto-tiering: signed-authority (verified) knowledge
                        # auto-promotes to the hivemind tier so the ecosystem
                        # experience-feed can export it; internal / external
                        # knowledge stays private. No-op unless
                        # SAMUS_KG_TIER_MODE=label AND the trust level warrants
                        # promotion — the default local stack is unchanged.
                        try:
                            from backend.memory import tiers as _tiers

                            _tiers.auto_promote_on_ingest(
                                "KnowledgeChunk",
                                chunk_id,
                                req.trust_level.value,
                            )
                        except Exception as promo_exc:  # noqa: BLE001
                            _LOG.warning(
                                "auto_promote_failed chunk_id=%s err=%s",
                                chunk_id,
                                promo_exc,
                            )
                    except Exception as exc:  # noqa: BLE001
                        _LOG.warning("graph_writer_failed chunk_id=%s err=%s", chunk_id, exc)
                        receipt.failures.append({"chunk_id": chunk_id, "error": str(exc)})
                        if self._dlq_writer is not None:
                            try:
                                self._dlq_writer(chunk_record)
                            except Exception as dlq_exc:  # noqa: BLE001
                                _LOG.warning(
                                    "dlq_writer_failed chunk_id=%s err=%s",
                                    chunk_id,
                                    dlq_exc,
                                )
                else:
                    # dry_run: count as indexed (no actual write)
                    receipt.chunks_indexed += 1

            processed_hashes.add(content_hash)
            receipt.content_hashes.append(content_hash)
            receipt.docs_processed += 1

        return receipt

    # --- FastAPI router stub ------------------------------------------------

    def get_router(self):
        """Return an APIRouter for /api/knowledge, or None if fastapi is missing."""
        try:
            from fastapi import APIRouter, HTTPException
        except ImportError:
            return None

        from backend.common.capabilities import check_capability

        from .models import KnowledgeIngestPayload, KnowledgeIngestResponse

        router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

        pod_ref = self  # capture for closure

        # FastAPI resolves type hints via typing.get_type_hints(fn, globalns=fn.__globals__).
        # When the handler is defined inside a method, KnowledgeIngestPayload is only in
        # the local scope — not in __globals__ — so FastAPI degrades to Query param.
        # Fix: build the handler via exec() in a fresh globals dict that includes the
        # Pydantic model, matching the pattern used by other Samus workcell route builders.
        _globals: dict = {
            "__builtins__": __builtins__,
            "KnowledgeIngestPayload": KnowledgeIngestPayload,
            "KnowledgeIngestResponse": KnowledgeIngestResponse,
            "Path": Path,
            "IngestRequest": IngestRequest,
            "TrustLevel": TrustLevel,
            "asdict": asdict,
            "check_capability": check_capability,
            "HTTPException": HTTPException,
            "_pod": pod_ref,
        }
        exec(
            "async def ingest_endpoint(body: KnowledgeIngestPayload) -> dict:\n"
            "    check_capability('memory', 'ingest_knowledge')\n"
            "    source = Path(body.source_path) if body.source_path else None\n"
            "    req = IngestRequest(\n"
            "        source_path=source,\n"
            "        documents=body.documents,\n"
            "        trust_level=TrustLevel(body.trust_level),\n"
            "        signature=body.signature,\n"
            "        chunk_size=body.chunk_size,\n"
            "        chunk_overlap=body.chunk_overlap,\n"
            "        batch_size=body.batch_size,\n"
            "        dry_run=body.dry_run,\n"
            "        embed=body.embed,\n"
            "        collection_label=body.collection_label,\n"
            "    )\n"
            "    try:\n"
            "        receipt = _pod.handle(req)\n"
            "    except (PermissionError, ValueError) as exc:\n"
            "        raise HTTPException(status_code=422, detail=str(exc)) from exc\n"
            "    return asdict(receipt)\n",
            _globals,
        )
        router.add_api_route(
            "/ingest",
            _globals["ingest_endpoint"],
            methods=["POST"],
            response_model=KnowledgeIngestResponse,
        )

        return router


# ---------------------------------------------------------------------------
# Module exports
# ---------------------------------------------------------------------------

__all__ = [
    "TrustLevel",
    "IngestRequest",
    "IngestReceipt",
    "KnowledgeIngestPod",
    "_hash_content",
    "_validate_doc",
    "_chunk_text",
    "_default_neo4j_writer",
]
