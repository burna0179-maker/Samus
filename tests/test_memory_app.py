"""TestClient smoke for backend.memory.app."""

from __future__ import annotations


def _fresh_store(monkeypatch):
    from backend.memory.store import MemoryStore
    import backend.memory.store as store_mod
    import backend.memory.app as app_mod

    fresh = MemoryStore()
    monkeypatch.setattr(store_mod, "GLOBAL_MEMORY_STORE", fresh)
    monkeypatch.setattr(app_mod, "GLOBAL_MEMORY_STORE", fresh)
    return fresh


def test_health(monkeypatch):
    _fresh_store(monkeypatch)
    from fastapi.testclient import TestClient
    from backend.memory.app import app

    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "memory"


def test_write_read_delete_roundtrip(monkeypatch):
    _fresh_store(monkeypatch)
    from fastapi.testclient import TestClient
    from backend.memory.app import app

    client = TestClient(app)
    r = client.post("/write", json={"namespace": "ns", "key": "k", "value": {"x": 1}})
    assert r.status_code == 200

    r = client.post("/read", json={"namespace": "ns", "key": "k"})
    assert r.status_code == 200
    assert r.json() == {"value": {"x": 1}, "found": True}

    r = client.post("/delete", json={"namespace": "ns", "key": "k"})
    assert r.status_code == 200
    assert r.json() == {"deleted": True}

    r = client.post("/read", json={"namespace": "ns", "key": "k"})
    assert r.json()["found"] is False


def test_query_endpoint(monkeypatch):
    _fresh_store(monkeypatch)
    from fastapi.testclient import TestClient
    from backend.memory.app import app

    client = TestClient(app)
    for i in range(3):
        client.post("/write", json={"namespace": "ns", "key": f"u_{i}", "value": i})
    r = client.post("/query", json={"namespace": "ns", "prefix": "u_", "limit": 50})
    assert r.status_code == 200
    body = r.json()
    keys = [it["key"] for it in body["items"]]
    assert keys == ["u_0", "u_1", "u_2"]
    assert body["next_cursor"] is None


def test_stats_endpoint(monkeypatch):
    _fresh_store(monkeypatch)
    from fastapi.testclient import TestClient
    from backend.memory.app import app

    client = TestClient(app)
    client.post("/write", json={"namespace": "ns", "key": "a", "value": 1})
    r = client.get("/stats/ns")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["oldest_ts"] is not None


# `test_graph_endpoints_deferred` removed — graph endpoints now go through the real
# GraphClient (see tests/test_memory_graph_endpoints.py for the new behavior).
