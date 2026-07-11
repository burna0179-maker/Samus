"""Pluggable in-process state backend per doc §3.8.

The doc separates per-process ephemeral state (this module) from cross-service
durable state (DynamoDB-backed via ``aws_runtime`` task_state_table).

Prior shell exported task-lifecycle helpers (``start/processing/completed/failed/get``)
that wrapped DynamoDB writes. Those move into ``aws_runtime`` / ``dynamodb`` as
direct calls; this module is now the ephemeral StateBackend ABC.
"""
from __future__ import annotations

import os
import threading
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Any


class StateBackend(ABC):
    @abstractmethod
    def get(self, key: str) -> Any | None: ...

    @abstractmethod
    def set(self, key: str, value: Any, ttl_seconds: int = 0) -> None: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def delete(self, key: str) -> bool: ...


class LocalStateBackend(StateBackend):
    """OrderedDict + Lock. LRU eviction at ``max_items`` (default 50_000)."""

    def __init__(self, max_items: int = 50_000) -> None:
        self._max = max_items
        self._data: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            return self._data.get(key)

    def set(self, key: str, value: Any, ttl_seconds: int = 0) -> None:  # noqa: ARG002
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def exists(self, key: str) -> bool:
        with self._lock:
            return key in self._data

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._data.pop(key, None) is not None


_backend: StateBackend | None = None
_BACKEND_LOCK = threading.Lock()


def get_backend() -> StateBackend:
    """Return the singleton state backend, instantiating on first call."""
    global _backend
    with _BACKEND_LOCK:
        if _backend is None:
            kind = os.getenv("SAMUS_STATE_BACKEND", "local").lower()
            if kind == "local":
                _backend = LocalStateBackend()
            else:
                raise ValueError(f"Unknown SAMUS_STATE_BACKEND: {kind}")
    return _backend


def reset_backend() -> None:
    """Test helper — force re-initialization on next get_backend()."""
    global _backend
    with _BACKEND_LOCK:
        _backend = None
