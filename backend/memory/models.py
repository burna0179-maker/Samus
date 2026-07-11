"""Pydantic models for the memory workcell.

Covers HTTP request/response shapes for the knowledge ingestion pod route
(POST /api/knowledge/ingest) and the /work envelope dispatcher.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class KnowledgeIngestPayload(BaseModel):
    """HTTP request body for POST /api/knowledge/ingest and /work action=ingest_knowledge.

    Either source_path (path to a JSON file containing a list of doc dicts) or
    documents (inline list) must be provided; both may not be omitted.
    """

    source_path: str | None = Field(
        default=None,
        description="Filesystem path to a JSON file containing a list of {id, document, metadata} dicts.",
    )
    documents: list[dict[str, Any]] | None = Field(
        default=None,
        description="Inline document list; alternative to source_path.",
    )
    trust_level: Literal["verified", "internal", "external"] = Field(
        default="internal",
        description="Trust classification applied to every chunk written.",
    )
    signature: str | None = Field(
        default=None,
        description="HMAC or minisign blob; required when trust_level='verified'.",
    )
    chunk_size: int = Field(
        default=800,
        ge=1,
        description="Maximum character length per chunk window.",
    )
    chunk_overlap: int = Field(
        default=100,
        ge=0,
        description="Character overlap between consecutive chunk windows.",
    )
    batch_size: int = Field(
        default=64,
        ge=1,
        description="(Reserved) batch size hint for future batched writers.",
    )
    dry_run: bool = Field(
        default=False,
        description="When True, compute receipt without performing any writes.",
    )
    embed: bool = Field(
        default=False,
        description="When True, call the injected embedder per chunk. Requires embedder attached to pod.",
    )
    collection_label: str = Field(
        default="samus_knowledge",
        min_length=1,
        description="Neo4j collection_label property written to every KnowledgeChunk node.",
    )


class KnowledgeIngestResponse(BaseModel):
    """HTTP response body for POST /api/knowledge/ingest.

    Mirrors IngestReceipt from backend.memory.knowledge_ingest.
    """

    docs_processed: int = 0
    chunks_indexed: int = 0
    duplicates_skipped: int = 0
    failures: list[dict[str, Any]] = Field(default_factory=list)
    content_hashes: list[str] = Field(default_factory=list)
    dry_run: bool = False


__all__ = ["KnowledgeIngestPayload", "KnowledgeIngestResponse"]
