"""Tests for backend.memory.store."""

from __future__ import annotations

import time


def test_write_and_read_roundtrip():
    from backend.memory.store import MemoryStore

    s = MemoryStore()
    s.write("ns", "k1", {"a": 1})
    value, found = s.read("ns", "k1")
    assert found is True
    assert value == {"a": 1}


def test_read_miss_returns_not_found():
    from backend.memory.store import MemoryStore

    s = MemoryStore()
    value, found = s.read("ns", "missing")
    assert found is False
    assert value is None


def test_ttl_expires():
    from backend.memory.store import MemoryStore

    s = MemoryStore()
    s.write("ns", "k", "v", ttl_seconds=1)
    # Force-expire via direct ts manipulation rather than sleeping.
    entry = s._data["ns:k"]
    entry.expires_at = time.time() - 1
    _, found = s.read("ns", "k")
    assert found is False


def test_query_prefix_and_pagination():
    from backend.memory.store import MemoryStore

    s = MemoryStore()
    for i in range(5):
        s.write("ns", f"user_{i}", i)
    s.write("ns", "other_1", "x")

    items, cursor = s.query("ns", "user_", limit=3)
    assert [it["key"] for it in items] == ["user_0", "user_1", "user_2"]
    assert cursor is not None

    page2, cursor2 = s.query("ns", "user_", limit=3, cursor=cursor)
    assert [it["key"] for it in page2] == ["user_3", "user_4"]
    assert cursor2 is None


def test_delete_and_stats():
    from backend.memory.store import MemoryStore

    s = MemoryStore()
    s.write("ns", "a", 1)
    s.write("ns", "b", 2)
    assert s.stats("ns")["count"] == 2
    assert s.delete("ns", "a") is True
    assert s.delete("ns", "a") is False
    stats = s.stats("ns")
    assert stats["count"] == 1
    assert stats["oldest_ts"] is not None
    assert stats["newest_ts"] is not None


def test_namespaces_are_isolated():
    from backend.memory.store import MemoryStore

    s = MemoryStore()
    s.write("alpha", "k", 1)
    s.write("beta", "k", 2)
    assert s.read("alpha", "k") == (1, True)
    assert s.read("beta", "k") == (2, True)
    assert s.stats("alpha")["count"] == 1
    assert s.stats("beta")["count"] == 1
