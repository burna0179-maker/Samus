"""In-process idempotency store per doc §3.10.

OrderedDict-backed LRU. Thread-safe. ``first_seen`` returns True on the first
call for a key (sets a None placeholder), False on every subsequent call —
the standard "claim once" pattern used by workcell service entry points.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any


class IdempotencyStore:
    def __init__(self, max_items: int = 10_000) -> None:
        self._max = max_items
        self._data: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            return self._data.get(key)

    def set(self, key: str, result: Any) -> None:
        with self._lock:
            self._data[key] = result
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def exists(self, key: str) -> bool:
        with self._lock:
            return key in self._data

    def first_seen(self, key: str) -> bool:
        """Return True on the first call for ``key``, False thereafter."""
        with self._lock:
            if key in self._data:
                return False
            self._data[key] = None
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)
            return True


GLOBAL_IDEMPOTENCY_STORE = IdempotencyStore()
