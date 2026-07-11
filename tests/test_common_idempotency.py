"""Doc §3.10 — in-process IdempotencyStore + GLOBAL_IDEMPOTENCY_STORE."""

from __future__ import annotations

import threading

from backend.common.idempotency import GLOBAL_IDEMPOTENCY_STORE, IdempotencyStore


def test_first_seen_returns_true_then_false():
    store = IdempotencyStore()
    assert store.first_seen("alpha") is True
    assert store.first_seen("alpha") is False
    assert store.first_seen("alpha") is False


def test_get_returns_none_for_absent():
    store = IdempotencyStore()
    assert store.get("missing") is None


def test_set_and_get_round_trip():
    store = IdempotencyStore()
    store.set("k1", {"value": 7})
    assert store.get("k1") == {"value": 7}


def test_exists_after_set():
    store = IdempotencyStore()
    assert store.exists("nope") is False
    store.set("yep", "hello")
    assert store.exists("yep") is True


def test_lru_eviction_when_over_capacity():
    store = IdempotencyStore(max_items=3)
    store.set("a", 1)
    store.set("b", 2)
    store.set("c", 3)
    store.set("d", 4)  # should evict "a"

    assert store.exists("a") is False
    assert store.exists("b") is True
    assert store.exists("c") is True
    assert store.exists("d") is True
    assert store.get("a") is None


def test_first_seen_does_not_overwrite_existing_value():
    store = IdempotencyStore()
    store.set("key", "the-value")
    assert store.first_seen("key") is False
    assert store.get("key") == "the-value"


def test_global_store_singleton_exists_and_is_idempotencystore():
    assert isinstance(GLOBAL_IDEMPOTENCY_STORE, IdempotencyStore)


def test_thread_safety_basic():
    """Twenty concurrent first_seen calls — exactly one returns True."""
    store = IdempotencyStore()
    results: list[bool] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(20)

    def worker() -> None:
        barrier.wait()
        outcome = store.first_seen("same_key")
        with results_lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for r in results if r is True) == 1
    assert sum(1 for r in results if r is False) == 19
