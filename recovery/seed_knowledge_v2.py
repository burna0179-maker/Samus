#!/usr/bin/env python3
"""
HF / Samus Knowledge Seeder v2.0.0
Source: ChatGPT recovery chat 01

Canonical relationship:
- [EXPANDS §17 roadmap] vector-backed semantic memory (deferred in v1.0)
- [EXPANDS §6 data plane] adds content-hash dedupe + chunking metadata
- [NEW] trust_level metadata field (per-doc data trust, distinct from §10 peer trust)

Capabilities added vs original:
- Schema validation (id + document required)
- SHA-256 content hashing for dedupe
- Chunking (CHUNK_SIZE=800, CHUNK_OVERLAP=100)
- Batched upserts (BATCH_SIZE=64)
- Enriched metadata (parent_id, chunk_index, content_hash)
- Failure-exit on missing deps / files
"""

from __future__ import annotations

import json
import sys
import hashlib
from pathlib import Path
from typing import List, Dict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


COLLECTION_NAME = "samus_knowledge"
BATCH_SIZE = 64
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100


def hash_content(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_doc(doc: Dict) -> bool:
    return "id" in doc and "document" in doc


def chunk_text(text: str) -> List[str]:
    chunks = []
    start = 0
    while start < len(text):
        end = start + CHUNK_SIZE
        chunks.append(text[start:end])
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


def main() -> None:
    try:
        import os
        os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
        import chromadb
    except ImportError:
        print("ERROR: chromadb missing. pip install chromadb>=0.6.0")
        sys.exit(1)

    seed_path = PROJECT_ROOT / "data" / "samus" / "knowledge" / "seed_documents.json"
    db_path = PROJECT_ROOT / "data" / "samus" / "vectorstore"

    if not seed_path.exists():
        print(f"ERROR: missing seed file: {seed_path}")
        sys.exit(1)

    with open(seed_path, encoding="utf-8") as f:
        raw_docs = json.load(f)

    if not raw_docs:
        print("WARNING: empty dataset")
        return

    db_path.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(db_path))
    collection = client.get_or_create_collection(COLLECTION_NAME)

    processed_ids = set()
    total_chunks = 0
    batch_ids, batch_docs, batch_meta = [], [], []

    for doc in raw_docs:
        if not validate_doc(doc):
            continue

        base_id = doc["id"]
        text = doc["document"]
        metadata = doc.get("metadata", {})
        content_hash = hash_content(text)

        if content_hash in processed_ids:
            continue
        processed_ids.add(content_hash)

        chunks = chunk_text(text)

        for i, chunk in enumerate(chunks):
            cid = f"{base_id}_chunk_{i}"
            batch_ids.append(cid)
            batch_docs.append(chunk)
            batch_meta.append({
                **metadata,
                "parent_id": base_id,
                "chunk_index": i,
                "content_hash": content_hash,
            })

            if len(batch_ids) >= BATCH_SIZE:
                collection.upsert(
                    ids=batch_ids,
                    documents=batch_docs,
                    metadatas=batch_meta,
                )
                total_chunks += len(batch_ids)
                batch_ids, batch_docs, batch_meta = [], [], []

    if batch_ids:
        collection.upsert(ids=batch_ids, documents=batch_docs, metadatas=batch_meta)
        total_chunks += len(batch_ids)

    print(f"[HF] Seed complete")
    print(f"[HF] Total chunks indexed: {total_chunks}")
    print(f"[HF] Collection size: {collection.count()}")


if __name__ == "__main__":
    main()
