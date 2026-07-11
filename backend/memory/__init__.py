"""Memory workcell — namespaced k/v store + graph stubs (doc §8).

Phase-A: in-process ``MemoryStore`` (see ``store.py``) exposes write/read/query/
delete/stats. The graph endpoints (``/graph/*``) in ``app.py`` are stubs that
return ``status=deferred`` until the Neo4j integration lands.
"""

from .store import GLOBAL_MEMORY_STORE, MemoryStore

__all__ = ["GLOBAL_MEMORY_STORE", "MemoryStore"]
