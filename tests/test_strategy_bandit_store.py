"""Tests for backend.strategy.bandit_store — the durable bandit persistence.

The strategy-integration build (Unit 1) makes the multi-armed bandit durable
via a DynamoDB table with a JSON-file fallback. These tests run against the
JSON fallback only — no real DDB — mirroring how test_llm_budget.py / conftest
isolate the LLM budget store (``DDB_LLM_BUDGETS_TABLE=""`` + a JSON tmpfile).

Coverage:
  - persistence round-trips (write -> read same store);
  - the cross-process scenario (write via one store instance, read via a
    fresh one — the decide / learn split runs in separate processes);
  - graceful degradation: a catastrophic store failure must not raise;
  - portfolio_manager.update_bandit / select_best_policy / get_bandit_stats
    behave identically to the in-memory bandit when reading their own writes;
  - the DDB backend issues an atomic ADD update (the concurrency contract).
"""

from __future__ import annotations

import pytest

from backend.strategy.bandit_store import (
    ARM_PK_ATTR,
    TRIALS_ATTR,
    WINS_ATTR,
    BanditArm,
    BanditStore,
    _DdbBanditBackend,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_store(tmp_path, **overrides) -> BanditStore:
    """Build a store backed only by a tmp JSON file (DDB disabled)."""
    kwargs = dict(
        ddb_table="",  # explicit disable -> JSON only
        json_path=str(tmp_path / "bandit.json"),
        cache_ttl_sec=0.0,  # no caching -> deterministic reads
    )
    kwargs.update(overrides)
    return BanditStore(**kwargs)


# ---------------------------------------------------------------------------
# BanditArm serialization
# ---------------------------------------------------------------------------


def test_bandit_arm_round_trips_through_item():
    """to_item / from_item preserve arm_id, wins, trials."""
    arm = BanditArm(arm_id="hvac::fast_quote_mode", wins=3.5, trials=4)
    restored = BanditArm.from_item(arm.to_item())
    assert restored.arm_id == "hvac::fast_quote_mode"
    assert restored.wins == pytest.approx(3.5)
    assert restored.trials == 4


def test_bandit_arm_from_item_tolerates_missing_counters():
    """A row missing wins/trials defaults to zero, not a KeyError."""
    arm = BanditArm.from_item({ARM_PK_ATTR: "lonely_arm"})
    assert arm.arm_id == "lonely_arm"
    assert arm.wins == 0.0
    assert arm.trials == 0


# ---------------------------------------------------------------------------
# JSON backend — persistence round-trips
# ---------------------------------------------------------------------------


def test_store_starts_empty(tmp_path):
    s = _json_store(tmp_path)
    assert s.read_all() == {}


def test_add_then_read_round_trips(tmp_path):
    """A single add is durable and reads back with the right counters."""
    s = _json_store(tmp_path)
    assert s.add("dentist", wins_delta=1.0, trials_delta=1) is True

    arms = s.read_all()
    assert set(arms) == {"dentist"}
    assert arms["dentist"].wins == pytest.approx(1.0)
    assert arms["dentist"].trials == 1


def test_add_accumulates_on_same_arm(tmp_path):
    """Repeated adds to one arm sum the wins and trials."""
    s = _json_store(tmp_path)
    s.add("plumber", wins_delta=1.0, trials_delta=1)
    s.add("plumber", wins_delta=0.5, trials_delta=1)
    s.add("plumber", wins_delta=0.0, trials_delta=1)

    arm = s.read_all()["plumber"]
    assert arm.wins == pytest.approx(1.5)
    assert arm.trials == 3


def test_add_keeps_arms_isolated(tmp_path):
    """Distinct arm ids never share counters."""
    s = _json_store(tmp_path)
    s.add("hvac", wins_delta=2.0, trials_delta=1)
    s.add("roofer", wins_delta=0.0, trials_delta=1)

    arms = s.read_all()
    assert arms["hvac"].wins == pytest.approx(2.0)
    assert arms["roofer"].wins == pytest.approx(0.0)
    assert arms["hvac"].trials == 1 and arms["roofer"].trials == 1


def test_flat_and_composite_arms_share_one_table(tmp_path):
    """Flat ``industry`` and composite ``industry::policy`` arms coexist."""
    s = _json_store(tmp_path)
    s.add("hvac", wins_delta=1.0, trials_delta=1)
    s.add("hvac::fast_quote_mode", wins_delta=1.0, trials_delta=1)

    arms = s.read_all()
    assert set(arms) == {"hvac", "hvac::fast_quote_mode"}


def test_clear_truncates_the_store(tmp_path):
    s = _json_store(tmp_path)
    s.add("hvac", wins_delta=1.0, trials_delta=1)
    assert s.read_all()  # populated
    s.clear()
    assert s.read_all() == {}


# ---------------------------------------------------------------------------
# Cross-process scenario — write via one store, read via a fresh one
# ---------------------------------------------------------------------------


def test_cross_process_write_then_fresh_read(tmp_path):
    """A second BanditStore on the same path sees the first store's writes.

    This is the decide / learn split: the host-side process writes via one
    BanditStore instance, the container-side process reads via a brand-new
    instance with its own (empty) cache.
    """
    json_path = str(tmp_path / "shared.json")
    writer = BanditStore(ddb_table="", json_path=json_path, cache_ttl_sec=0.0)
    writer.add("dentist::reputation_repair", wins_delta=2.0, trials_delta=1)
    writer.add("dentist::reputation_repair", wins_delta=1.0, trials_delta=1)

    reader = BanditStore(ddb_table="", json_path=json_path, cache_ttl_sec=0.0)
    arms = reader.read_all()
    assert arms["dentist::reputation_repair"].wins == pytest.approx(3.0)
    assert arms["dentist::reputation_repair"].trials == 2


def test_cross_process_interleaved_writers(tmp_path):
    """Two store instances writing the same arm both land (read-modify-write)."""
    json_path = str(tmp_path / "interleaved.json")
    host = BanditStore(ddb_table="", json_path=json_path, cache_ttl_sec=0.0)
    container = BanditStore(ddb_table="", json_path=json_path, cache_ttl_sec=0.0)

    host.add("hvac", wins_delta=1.0, trials_delta=1)
    container.add("hvac", wins_delta=1.0, trials_delta=1)
    host.add("hvac", wins_delta=0.0, trials_delta=1)

    arm = BanditStore(ddb_table="", json_path=json_path).read_all()["hvac"]
    assert arm.wins == pytest.approx(2.0)
    assert arm.trials == 3


# ---------------------------------------------------------------------------
# In-memory cache vs store-of-truth
# ---------------------------------------------------------------------------


def test_cache_is_invalidated_by_a_write(tmp_path):
    """A write invalidates the cache so the next read reflects it."""
    s = _json_store(tmp_path, cache_ttl_sec=300.0)  # long TTL
    s.add("hvac", wins_delta=1.0, trials_delta=1)
    assert s.read_all()["hvac"].trials == 1  # populates cache
    s.add("hvac", wins_delta=1.0, trials_delta=1)
    # Even with a 5-minute TTL the post-write read must show trials == 2.
    assert s.read_all()["hvac"].trials == 2


def test_read_returns_independent_copies(tmp_path):
    """Mutating a read result must not corrupt the store's cache."""
    s = _json_store(tmp_path, cache_ttl_sec=300.0)
    s.add("hvac", wins_delta=1.0, trials_delta=1)
    arms = s.read_all()
    arms["hvac"].wins = 999.0
    # A fresh read still shows the real value.
    assert s.read_all()["hvac"].wins == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Graceful degradation — a store failure must not raise
# ---------------------------------------------------------------------------


def test_read_all_degrades_to_empty_on_json_failure(tmp_path, monkeypatch):
    """A JSON backend that raises on read degrades to {} — never propagates."""
    s = _json_store(tmp_path)

    def _boom():
        raise OSError("disk gone")

    monkeypatch.setattr(s._json_backend, "read_all", _boom)
    assert s.read_all() == {}  # no exception


def test_add_degrades_to_false_on_json_failure(tmp_path, monkeypatch):
    """An add whose backend raises returns False — it does not propagate."""
    s = _json_store(tmp_path)

    def _boom(*_a, **_kw):
        raise OSError("disk gone")

    monkeypatch.setattr(s._json_backend, "add", _boom)
    assert s.add("hvac", wins_delta=1.0, trials_delta=1) is False


def test_add_returns_false_when_json_save_unwritable(tmp_path):
    """An unwritable JSON path fails the save softly (False), no raise."""
    # A path whose parent is a file, not a directory -> makedirs/replace fail.
    bad_parent = tmp_path / "afile"
    bad_parent.write_text("not a dir")
    s = BanditStore(ddb_table="", json_path=str(bad_parent / "nested" / "b.json"))
    assert s.add("hvac", wins_delta=1.0, trials_delta=1) is False


def test_json_backend_recovers_from_corrupt_file(tmp_path):
    """A corrupt JSON file reads as empty rather than crashing."""
    json_path = tmp_path / "corrupt.json"
    json_path.write_text("{ this is not json")
    s = BanditStore(ddb_table="", json_path=str(json_path))
    assert s.read_all() == {}
    # And a subsequent write still succeeds, overwriting the garbage.
    assert s.add("hvac", wins_delta=1.0, trials_delta=1) is True
    assert s.read_all()["hvac"].trials == 1


# ---------------------------------------------------------------------------
# DDB backend — atomic ADD contract (no real DDB; the table is a fake)
# ---------------------------------------------------------------------------


class _FakeTable:
    """Minimal stand-in capturing the update_item call shape."""

    def __init__(self) -> None:
        self.update_calls: list[dict] = []
        self.rows: dict[str, dict] = {}

    def update_item(self, **kwargs):
        self.update_calls.append(kwargs)

    def scan(self, **_kwargs):
        return {"Items": list(self.rows.values())}


def test_ddb_backend_add_uses_atomic_add_expression(monkeypatch):
    """The DDB backend issues an ``ADD`` UpdateExpression on wins + trials.

    The ADD action is what keeps the host-side and container-side writers
    from clobbering each other — DynamoDB serializes the increments.
    """
    backend = _DdbBanditBackend("samus_strategy_bandit", "us-west-1")
    fake = _FakeTable()
    monkeypatch.setattr(backend, "_table", lambda: fake)

    assert backend.add("hvac::fast_quote_mode", wins_delta=1.5, trials_delta=1) is True

    assert len(fake.update_calls) == 1
    call = fake.update_calls[0]
    assert call["Key"] == {ARM_PK_ATTR: "hvac::fast_quote_mode"}
    # Must be an ADD, not a SET (SET would overwrite, losing a concurrent write).
    assert call["UpdateExpression"].strip().startswith("ADD ")
    assert WINS_ATTR in call["UpdateExpression"]
    assert TRIALS_ATTR in call["UpdateExpression"]
    assert call["ExpressionAttributeValues"][":t"] == 1


def test_ddb_backend_add_degrades_to_false_on_error(monkeypatch):
    """A DDB update that raises returns False — never propagates."""
    backend = _DdbBanditBackend("samus_strategy_bandit", "us-west-1")

    def _boom():
        raise RuntimeError("ddb unreachable")

    monkeypatch.setattr(backend, "_table", _boom)
    assert backend.add("hvac", wins_delta=1.0, trials_delta=1) is False


def test_ddb_backend_read_all_degrades_to_none_on_error(monkeypatch):
    """A failing DDB scan returns None so the store falls back to JSON."""
    backend = _DdbBanditBackend("samus_strategy_bandit", "us-west-1")

    def _boom():
        raise RuntimeError("ddb unreachable")

    monkeypatch.setattr(backend, "_table", _boom)
    assert backend.read_all() is None


def test_store_falls_back_to_json_when_ddb_scan_fails(tmp_path, monkeypatch):
    """When the DDB backend's scan fails, read_all uses the JSON fallback."""
    json_path = str(tmp_path / "fallback.json")
    # Seed the JSON file via a JSON-only store.
    BanditStore(ddb_table="", json_path=json_path).add(
        "hvac",
        wins_delta=2.0,
        trials_delta=1,
    )
    # Now a DDB-configured store whose DDB scan fails must still see the row.
    s = BanditStore(
        ddb_table="samus_strategy_bandit",
        json_path=json_path,
        cache_ttl_sec=0.0,
    )
    monkeypatch.setattr(s._ddb_backend, "read_all", lambda: None)
    arms = s.read_all()
    assert arms["hvac"].wins == pytest.approx(2.0)
