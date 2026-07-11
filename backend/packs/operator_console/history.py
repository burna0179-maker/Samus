"""Per-chat operator-console history in SQLite WAL (Samus)."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from backend.standard.chat import ChatEnrichmentBag, SpiceState

__tier__ = "PACKS"


@dataclass(frozen=True)
class StoredMessage:
    message_id: int
    chat_id: str
    turn: int
    role: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "chat_id": self.chat_id,
            "turn": self.turn,
            "role": self.role,
            "content": self.content,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class StoredChat:
    chat_id: str
    name: str
    persona_id: str
    bag: ChatEnrichmentBag
    spice_state: SpiceState
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "name": self.name,
            "persona_id": self.persona_id,
            "bag": self.bag.model_dump(),
            "spice_state": {
                "category_id": self.spice_state.category_id,
                "index": self.spice_state.index,
                "turn": self.spice_state.turn,
                "current": self.spice_state.current,
            },
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    persona_id  TEXT NOT NULL,
    bag_json    TEXT NOT NULL,
    spice_state TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    TEXT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    turn       INTEGER NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    metadata   TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, message_id);
"""


class ConsoleHistory:
    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._open_and_init()

    def _open_and_init(self) -> None:
        self._conn = sqlite3.connect(
            str(self._path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        for stmt in _SCHEMA.strip().split(";"):
            s = stmt.strip()
            if s:
                self._conn.execute(s)

    @property
    def path(self) -> Path:
        return self._path

    def create_chat(self, *, name, persona_id, bag, spice_category="default"):
        chat_id = uuid.uuid4().hex
        now = time.time()
        state = SpiceState(category_id=spice_category)
        with self._lock:
            self._conn.execute(
                "INSERT INTO chats(chat_id, name, persona_id, bag_json, spice_state, created_at, updated_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    chat_id,
                    name,
                    persona_id,
                    json.dumps(bag.model_dump(), sort_keys=True, separators=(",", ":")),
                    json.dumps(_spice_to_dict(state), sort_keys=True, separators=(",", ":")),
                    now,
                    now,
                ),
            )
        return StoredChat(
            chat_id=chat_id,
            name=name,
            persona_id=persona_id,
            bag=bag,
            spice_state=state,
            created_at=now,
            updated_at=now,
        )

    def get_chat(self, chat_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT chat_id, name, persona_id, bag_json, spice_state, created_at, updated_at"
                " FROM chats WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return _row_to_chat(row)

    def list_chats(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT chat_id, name, persona_id, bag_json, spice_state, created_at, updated_at"
                " FROM chats ORDER BY updated_at DESC"
            ).fetchall()
        return [c for c in (_row_to_chat(r) for r in rows) if c is not None]

    def update_chat(self, chat):
        now = time.time()
        updated = StoredChat(
            chat_id=chat.chat_id,
            name=chat.name,
            persona_id=chat.persona_id,
            bag=chat.bag,
            spice_state=chat.spice_state,
            created_at=chat.created_at,
            updated_at=now,
        )
        with self._lock:
            self._conn.execute(
                "UPDATE chats SET name = ?, persona_id = ?, bag_json = ?, spice_state = ?, updated_at = ?"
                " WHERE chat_id = ?",
                (
                    updated.name,
                    updated.persona_id,
                    json.dumps(updated.bag.model_dump(), sort_keys=True, separators=(",", ":")),
                    json.dumps(
                        _spice_to_dict(updated.spice_state), sort_keys=True, separators=(",", ":")
                    ),
                    now,
                    updated.chat_id,
                ),
            )
        return updated

    def delete_chat(self, chat_id):
        with self._lock:
            cur = self._conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
            return cur.rowcount > 0

    def append_message(self, *, chat_id, role, content, metadata=None):
        now = time.time()
        with self._lock:
            turn = self._conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()["n"]
            cur = self._conn.execute(
                "INSERT INTO messages(chat_id, turn, role, content, metadata, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?)",
                (
                    chat_id,
                    turn,
                    role,
                    content,
                    json.dumps(metadata or {}, sort_keys=True, separators=(",", ":")),
                    now,
                ),
            )
            message_id = int(cur.lastrowid or 0)
        return StoredMessage(
            message_id=message_id,
            chat_id=chat_id,
            turn=turn,
            role=role,
            content=content,
            metadata=dict(metadata or {}),
            created_at=now,
        )

    def list_messages(self, chat_id, *, limit=None):
        sql = (
            "SELECT message_id, chat_id, turn, role, content, metadata, created_at"
            " FROM messages WHERE chat_id = ? ORDER BY message_id"
        )
        params: tuple = (chat_id,)
        if limit is not None and limit > 0:
            sql += " LIMIT ?"
            params = (chat_id, int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_message(r) for r in rows]

    def chat_count(self):
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) AS n FROM chats").fetchone()["n"]

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover
                pass


def _spice_to_dict(state):
    return {
        "category_id": state.category_id,
        "index": state.index,
        "turn": state.turn,
        "current": state.current,
    }


def _dict_to_spice(raw):
    return SpiceState(
        category_id=str(raw.get("category_id", "default")),
        index=int(raw.get("index", 0) or 0),
        turn=int(raw.get("turn", 0) or 0),
        current=str(raw.get("current", "") or ""),
    )


def _row_to_chat(row):
    if row is None:
        return None
    try:
        bag = ChatEnrichmentBag.model_validate(json.loads(row["bag_json"]))
    except Exception:
        bag = ChatEnrichmentBag()
    try:
        spice_state = _dict_to_spice(json.loads(row["spice_state"]))
    except Exception:
        spice_state = SpiceState()
    return StoredChat(
        chat_id=str(row["chat_id"]),
        name=str(row["name"]),
        persona_id=str(row["persona_id"]),
        bag=bag,
        spice_state=spice_state,
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _row_to_message(row):
    try:
        metadata = json.loads(row["metadata"])
        if not isinstance(metadata, dict):
            metadata = {}
    except Exception:
        metadata = {}
    return StoredMessage(
        message_id=int(row["message_id"]),
        chat_id=str(row["chat_id"]),
        turn=int(row["turn"]),
        role=str(row["role"]),
        content=str(row["content"]),
        metadata=metadata,
        created_at=float(row["created_at"]),
    )


__all__ = ["ConsoleHistory", "StoredChat", "StoredMessage"]
