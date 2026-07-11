"""In-process namespaced k/v store with TTL, prefix query, pagination (doc §8).

This is the Phase-A in-memory backing for the memory workcell. A Neo4j /
DynamoDB persistence layer lands in a later phase; the graph endpoints in
``app.py`` are stubs that report ``status=deferred`` for now.

Thread-safe via a single re-entrant lock; values are stored as opaque JSON-able
objects in an OrderedDict keyed on ``f"{namespace}:{key}"``.
"""
from __future__ import annotations

import base64
import threading
import time
from collections import OrderedDict
from typing import Any


class _Entry:
    __slots__ = ("value", "created_at", "expires_at")

    def __init__(self, value: Any, created_at: float, expires_at: float | None) -> None:
        self.value = value
        self.created_at = created_at
        self.expires_at = expires_at

    def is_expired(self, now: float) -> bool:
        return self.expires_at is not None and now >= self.expires_at


class MemoryStore:
    """Thread-safe namespaced key-value store."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # composite_key -> _Entry. OrderedDict preserves insertion order for
        # stable prefix-query pagination.
        self._data: "OrderedDict[str, _Entry]" = OrderedDict()

    # --- helpers ---------------------------------------------------------

    @staticmethod
    def _composite(namespace: str, key: str) -> str:
        return f"{namespace}:{key}"

    def _evict_expired(self, now: float) -> None:
        """Drop expired entries. Caller holds the lock."""
        expired = [k for k, e in self._data.items() if e.is_expired(now)]
        for k in expired:
            self._data.pop(k, None)

    @staticmethod
    def _encode_cursor(value: str) -> str:
        return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")

    @staticmethod
    def _decode_cursor(value: str | None) -> str:
        if not value:
            return ""
        try:
            return base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return ""

    # --- public API ------------------------------------------------------

    def write(
        self,
        namespace: str,
        key: str,
        value: Any,
        ttl_seconds: int | None = None,
    ) -> None:
        now = time.time()
        expires_at = (now + float(ttl_seconds)) if ttl_seconds else None
        composite = self._composite(namespace, key)
        with self._lock:
            self._data[composite] = _Entry(value=value, created_at=now, expires_at=expires_at)
            self._data.move_to_end(composite)

    def read(self, namespace: str, key: str) -> tuple[Any, bool]:
        """Return ``(value, found)``. ``found`` is False on miss or expiry."""
        composite = self._composite(namespace, key)
        now = time.time()
        with self._lock:
            entry = self._data.get(composite)
            if entry is None:
                return None, False
            if entry.is_expired(now):
                self._data.pop(composite, None)
                return None, False
            return entry.value, True

    def query(
        self,
        namespace: str,
        prefix: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return ``(items, next_cursor)`` for keys starting with ``prefix``."""
        limit = max(1, min(int(limit or 50), 500))
        start_after = self._decode_cursor(cursor)
        ns_prefix = f"{namespace}:"
        full_prefix = f"{namespace}:{prefix}"
        now = time.time()
        items: list[dict[str, Any]] = []
        last_key: str | None = None

        with self._lock:
            self._evict_expired(now)
            for composite, entry in self._data.items():
                if not composite.startswith(full_prefix):
                    continue
                # Strip the namespace prefix to give callers the bare key.
                bare_key = composite[len(ns_prefix):]
                if start_after and composite <= start_after:
                    continue
                items.append({
                    "key": bare_key,
                    "value": entry.value,
                    "created_at": entry.created_at,
                    "expires_at": entry.expires_at,
                })
                last_key = composite
                if len(items) >= limit:
                    break

        next_cursor = self._encode_cursor(last_key) if (last_key and len(items) >= limit) else None
        return items, next_cursor

    def delete(self, namespace: str, key: str) -> bool:
        composite = self._composite(namespace, key)
        with self._lock:
            return self._data.pop(composite, None) is not None

    def stats(self, namespace: str) -> dict[str, Any]:
        ns_prefix = f"{namespace}:"
        now = time.time()
        with self._lock:
            self._evict_expired(now)
            entries = [e for k, e in self._data.items() if k.startswith(ns_prefix)]
        count = len(entries)
        if not entries:
            return {"namespace": namespace, "count": 0, "oldest_ts": None, "newest_ts": None}
        timestamps = [e.created_at for e in entries]
        return {
            "namespace": namespace,
            "count": count,
            "oldest_ts": min(timestamps),
            "newest_ts": max(timestamps),
        }

    def clear(self) -> None:
        """Drop every namespace. Used by tests."""
        with self._lock:
            self._data.clear()


# Module-level default store. Tests can monkeypatch this with a fresh instance.
GLOBAL_MEMORY_STORE = MemoryStore()
