#!/usr/bin/env python3
"""
SMMS ChatGPT Export Ingestion Pipeline
Source: ChatGPT recovery chat 36

Canonical relationship:
- [NEW pack] business/smms — knowledge-base ingestion
- [EXPANDS §6 context.tiers] long-term memory bootstrap from external dataset
- [META-CIRCULAR] this very recovery session IS the kind of source this pipeline ingests

ChatGPT data export structure:
  /conversations/        — folder with one file per conversation
  /images/               — uploaded images
  /uploads/              — other uploaded files
  /account.json          — account metadata
  /history.json          — chat history index
  /conversations.json    — full conversation export (canonical source)

Target knowledge base layout (per chat 36 + organizational_planes.md):
  knowledge_base/
    chatgpt_raw/         — verbatim unpacked export
    conversations/       — normalized per-conversation JSON
    messages/            — flattened message rows
    attachments/         — extracted files
    summaries/           — extracted insights
    entity_maps/         — entities + intent graphs
    automation_hooks/    — actionable patterns
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


@dataclass
class IngestionStats:
    conversations: int = 0
    messages: int = 0
    attachments: int = 0
    summaries_generated: int = 0
    entities_extracted: int = 0
    errors: List[Dict[str, Any]] = field(default_factory=list)


_MAX_TOTAL_UNCOMPRESSED = 2 * 1024 * 1024 * 1024   # 2 GiB cap (bomb guard)
_MAX_ENTRIES = 100_000


def unzip_export(zip_path: Path, dest: Path) -> Path:
    """Extract ChatGPT export ZIP into knowledge_base/chatgpt_raw/ (zip-slip safe).

    A downloaded export is third-party-transported data: validate every entry
    stays under raw_dir, reject symlinks/absolute paths, and cap total size +
    entry count so a malicious/poisoned archive cannot write outside the dir or
    exhaust disk.
    """
    raw_dir = (dest / "chatgpt_raw").resolve()
    raw_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > _MAX_ENTRIES:
            raise ValueError(f"archive has too many entries ({len(infos)})")
        total = sum(i.file_size for i in infos)
        if total > _MAX_TOTAL_UNCOMPRESSED:
            raise ValueError(f"archive uncompressed size {total} exceeds cap")
        for info in infos:
            # reject symlinks (unix mode high bits 0o120000)
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError(f"refusing symlink entry: {info.filename}")
            target = (raw_dir / info.filename).resolve()
            if not target.is_relative_to(raw_dir):
                raise ValueError(f"zip-slip blocked: {info.filename}")
        zf.extractall(raw_dir)   # safe: every entry validated above
    return raw_dir


def iter_conversations(raw_dir: Path) -> Iterable[Dict[str, Any]]:
    """Stream conversations from conversations.json (the canonical export shape)."""
    convos_file = raw_dir / "conversations.json"
    if not convos_file.exists():
        return
    data = json.loads(convos_file.read_text(encoding="utf-8"))
    if isinstance(data, list):
        for conv in data:
            yield conv
    elif isinstance(data, dict):
        for conv in data.get("conversations", []):
            yield conv


def flatten_messages(conversation: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Walk the ChatGPT message tree → flat list ordered by parent links."""
    mapping = conversation.get("mapping") or {}
    if not isinstance(mapping, dict):
        return []
    messages: List[Dict[str, Any]] = []
    for node_id, node in mapping.items():
        msg = node.get("message") if isinstance(node, dict) else None
        if not msg:
            continue
        author = (msg.get("author") or {}).get("role", "unknown")
        content = msg.get("content") or {}
        parts = content.get("parts") if isinstance(content, dict) else None
        text = "".join(str(p) for p in parts) if isinstance(parts, list) else ""
        messages.append({
            "node_id": node_id,
            "role": author,
            "text": text,
            "create_time": msg.get("create_time"),
            "model_slug": (msg.get("metadata") or {}).get("model_slug"),
        })
    messages.sort(key=lambda m: m.get("create_time") or 0)
    return messages


def write_normalized(kb_dir: Path, conversation: Dict[str, Any], messages: List[Dict[str, Any]]) -> Dict[str, Path]:
    """Write per-conversation JSON + flattened messages JSONL."""
    conv_id = conversation.get("id") or hashlib.sha1(
        json.dumps(conversation.get("title", "untitled")).encode()
    ).hexdigest()[:12]

    convo_dir = kb_dir / "conversations"
    msgs_dir = kb_dir / "messages"
    convo_dir.mkdir(parents=True, exist_ok=True)
    msgs_dir.mkdir(parents=True, exist_ok=True)

    convo_path = convo_dir / f"{conv_id}.json"
    convo_path.write_text(
        json.dumps({
            "id": conv_id,
            "title": conversation.get("title"),
            "create_time": conversation.get("create_time"),
            "update_time": conversation.get("update_time"),
            "message_count": len(messages),
        }, indent=2),
        encoding="utf-8",
    )

    msgs_path = msgs_dir / f"{conv_id}.jsonl"
    with msgs_path.open("w", encoding="utf-8") as f:
        for m in messages:
            f.write(json.dumps(m) + "\n")

    return {"conversation": convo_path, "messages": msgs_path}


def extract_attachments(raw_dir: Path, kb_dir: Path) -> int:
    """Move uploads/images into knowledge_base/attachments/."""
    count = 0
    out_dir = kb_dir / "attachments"
    out_dir.mkdir(parents=True, exist_ok=True)
    for source_name in ("uploads", "images"):
        src = raw_dir / source_name
        if not src.exists():
            continue
        for f in src.rglob("*"):
            if f.is_file():
                target = out_dir / f"{source_name}_{f.name}"
                target.write_bytes(f.read_bytes())
                count += 1
    return count


def ingest_export(zip_path: Path, kb_dir: Path,
                  summarize_fn=None, entity_extract_fn=None) -> IngestionStats:
    """
    Full pipeline:
      1. unzip export
      2. iterate conversations
      3. flatten messages
      4. write normalized JSON + JSONL
      5. extract attachments
      6. optionally summarize each conversation
      7. optionally extract entities

    summarize_fn(messages) -> str   (e.g. local LLM call)
    entity_extract_fn(messages) -> List[Dict]
    """
    stats = IngestionStats()
    kb_dir.mkdir(parents=True, exist_ok=True)

    try:
        raw_dir = unzip_export(zip_path, kb_dir)
    except Exception as e:
        stats.errors.append({"stage": "unzip", "error": str(e)})
        return stats

    for conv in iter_conversations(raw_dir):
        try:
            msgs = flatten_messages(conv)
            write_normalized(kb_dir, conv, msgs)
            stats.conversations += 1
            stats.messages += len(msgs)

            if summarize_fn:
                summary = summarize_fn(msgs)
                summary_dir = kb_dir / "summaries"
                summary_dir.mkdir(parents=True, exist_ok=True)
                summary_path = summary_dir / f"{conv.get('id', 'unknown')}.md"
                summary_path.write_text(summary, encoding="utf-8")
                stats.summaries_generated += 1

            if entity_extract_fn:
                entities = entity_extract_fn(msgs)
                if entities:
                    ent_dir = kb_dir / "entity_maps"
                    ent_dir.mkdir(parents=True, exist_ok=True)
                    ent_path = ent_dir / f"{conv.get('id', 'unknown')}.json"
                    ent_path.write_text(json.dumps(entities, indent=2), encoding="utf-8")
                    stats.entities_extracted += len(entities)
        except Exception as e:
            stats.errors.append({"stage": "process_conversation",
                                 "conv_id": conv.get("id"), "error": str(e)})

    stats.attachments = extract_attachments(raw_dir, kb_dir)
    return stats


# ----- Recommended downstream pipeline steps -----
RECOMMENDED_DOWNSTREAM = """
After ingestion, SMMS should:
  1. Build vector embeddings (chunk → embed → Qdrant) from messages JSONL
  2. Run topic clustering on summaries
  3. Build entity graph (people / projects / domains)
  4. Identify automation hook candidates (repeated patterns, decision trees)
  5. Emit cross-reference index linking conversations to current pods/agents
  6. Optionally: feed into MetaCognitionEngine's HeuristicRegistry as
     situation-type priors for the bias layer
"""
