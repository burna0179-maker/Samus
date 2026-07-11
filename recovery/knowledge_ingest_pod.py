#!/usr/bin/env python3
"""
KnowledgeIngestPod — Samus orchestrator-callable ingestion pod skeleton
Source: ChatGPT recovery chat 01 (strategic next-steps section)

Canonical relationship:
- [NEW pod, EXPANDS §6 agents plane] callable via Samus orchestrator dispatch
- [EXPANDS §8 mutation plane] ingestion is a STATE mutation; gated through @mutation_scope
- [EXPANDS §6 data plane] writes to vectorstore = canonical data-backend extension point
- [NEW] trust_level enum: verified | internal | external
- [DEFERRED] HMAC / minisign signature verification before ingest
- [DEFERRED] DLQ replay archive for failed chunk inserts → dlq_replayed.jsonl

NOTE: skeleton only — apply_fn/verify_fn/revert_fn require wiring to live
backend.core.mutation.pipeline.get_pipeline() at recreation time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


class TrustLevel(str, Enum):
    VERIFIED = "verified"   # signed authority source
    INTERNAL = "internal"   # in-house generated
    EXTERNAL = "external"   # third-party / scraped


@dataclass
class IngestRequest:
    source_path: Path
    trust_level: TrustLevel = TrustLevel.INTERNAL
    signature: Optional[str] = None       # HMAC or minisign (deferred)
    chunk_size: int = 800
    chunk_overlap: int = 100
    batch_size: int = 64
    dry_run: bool = False


@dataclass
class IngestReceipt:
    docs_processed: int = 0
    chunks_indexed: int = 0
    duplicates_skipped: int = 0
    failures: List[Dict[str, Any]] = field(default_factory=list)
    content_hashes: List[str] = field(default_factory=list)


class KnowledgeIngestPod:
    """
    Pod contract:
      - handle(req: IngestRequest) -> IngestReceipt
      - mutation_scope (declared at module level when wired):
          state_paths=("data/samus/vectorstore/**",)
          mutation_types={MutationType.STATE}
          requires=("hmac_signature",) when trust_level=VERIFIED
    """

    POD_ID = "knowledge_ingest"

    def __init__(self, collection_name: str = "samus_knowledge"):
        self.collection_name = collection_name
        self._verify_signature_hook: Optional[Callable[[bytes, str], bool]] = None
        self._dlq_writer: Optional[Callable[[Dict[str, Any]], None]] = None

    def attach_signature_verifier(self, fn: Callable[[bytes, str], bool]) -> None:
        self._verify_signature_hook = fn

    def attach_dlq_writer(self, fn: Callable[[Dict[str, Any]], None]) -> None:
        self._dlq_writer = fn

    def handle(self, req: IngestRequest) -> IngestReceipt:
        receipt = IngestReceipt()
        if req.trust_level == TrustLevel.VERIFIED and req.signature is None:
            raise PermissionError("VERIFIED ingest requires signature")
        # actual ingest happens via the mutation pipeline; this is the skeleton.
        return receipt

    def get_router(self):
        """FastAPI router stub — wires /api/knowledge/ingest at pack load."""
        try:
            from fastapi import APIRouter
        except ImportError:
            return None
        r = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

        @r.post("/ingest")
        def ingest(payload: Dict[str, Any]):
            req = IngestRequest(
                source_path=Path(payload["source_path"]),
                trust_level=TrustLevel(payload.get("trust_level", "internal")),
                signature=payload.get("signature"),
                dry_run=bool(payload.get("dry_run", False)),
            )
            return self.handle(req).__dict__

        return r


def get_pods() -> Dict[str, Any]:
    return {KnowledgeIngestPod.POD_ID: KnowledgeIngestPod()}
