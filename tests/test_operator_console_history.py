"""ConsoleHistory -- SQLite-backed per-chat store (Samus)."""
from __future__ import annotations

from pathlib import Path

from backend.packs.operator_console.history import ConsoleHistory
from backend.standard.chat import ChatEnrichmentBag


def _bag() -> ChatEnrichmentBag:
    return ChatEnrichmentBag()


def test_create_chat_persists(tmp_path: Path):
    h = ConsoleHistory(tmp_path / "history.db")
    chat = h.create_chat(name="first", persona_id="samus_console", bag=_bag())
    assert chat.chat_id
    assert ConsoleHistory(tmp_path / "history.db").get_chat(chat.chat_id) is not None


def test_append_message_assigns_monotonic_turns(tmp_path: Path):
    h = ConsoleHistory(tmp_path / "history.db")
    chat = h.create_chat(name="x", persona_id="p", bag=_bag())
    a = h.append_message(chat_id=chat.chat_id, role="user", content="hi")
    b = h.append_message(chat_id=chat.chat_id, role="assistant", content="ack")
    assert a.turn == 0 and b.turn == 1


def test_delete_chat_cascades_to_messages(tmp_path: Path):
    h = ConsoleHistory(tmp_path / "history.db")
    chat = h.create_chat(name="x", persona_id="p", bag=_bag())
    h.append_message(chat_id=chat.chat_id, role="user", content="m")
    assert h.delete_chat(chat.chat_id)
    assert h.list_messages(chat.chat_id) == []


def test_list_chats_ordered_by_updated_at_desc(tmp_path: Path):
    h = ConsoleHistory(tmp_path / "history.db")
    a = h.create_chat(name="a", persona_id="p", bag=_bag())
    h.create_chat(name="b", persona_id="p", bag=_bag())
    h.update_chat(a)
    assert [c.name for c in h.list_chats()][:2] == ["a", "b"]


def test_update_chat_preserves_id_and_created_at(tmp_path: Path):
    h = ConsoleHistory(tmp_path / "history.db")
    chat = h.create_chat(name="x", persona_id="p", bag=_bag())
    updated = h.update_chat(chat)
    assert updated.chat_id == chat.chat_id and updated.created_at == chat.created_at
