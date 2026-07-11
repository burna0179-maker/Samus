"""Tests for `backend.strategy.capability_marketplace_store`.

Covers append-on-publish/withdraw, replay-on-startup, the
:class:`PersistentCapabilityMarketplace` mutation wrapper, and the env
override.
"""

from __future__ import annotations

import json

import pytest

from backend.strategy.capability_marketplace import (
    CapabilityListing,
    CapabilityMarketplace,
)
from backend.strategy.capability_marketplace_store import (
    ENV_MARKETPLACE_PATH,
    OP_PUBLISH,
    OP_WITHDRAW,
    PersistentCapabilityMarketplace,
    append_publish,
    append_withdraw,
    load_marketplace,
    replay_into,
)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Route the ledger to a per-test path and force jsonl backend."""
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "jsonl")
    # Explicit override so we never accidentally read a shared file.
    monkeypatch.setenv(
        ENV_MARKETPLACE_PATH,
        str(tmp_path / "state" / "strategy" / "capability_marketplace.jsonl"),
    )


def _listing(
    capability_id: str = "trend_forecasting",
    provider_agent: str = "research_agent_7",
    cost: int = 10,
    performance_score: float = 0.91,
    latency_ms: int = 800,
    tags: tuple[str, ...] = (),
) -> CapabilityListing:
    return CapabilityListing(
        capability_id=capability_id,
        provider_agent=provider_agent,
        cost=cost,
        performance_score=performance_score,
        latency_ms=latency_ms,
        tags=tags,
    )


def _ledger_rows(tmp_path) -> list[dict]:
    path = tmp_path / "state" / "strategy" / "capability_marketplace.jsonl"
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# Env override
# ---------------------------------------------------------------------------
def test_env_override_directs_writes_to_custom_path(tmp_path, monkeypatch):
    custom = tmp_path / "custom" / "market.jsonl"
    monkeypatch.setenv(ENV_MARKETPLACE_PATH, str(custom))
    append_publish(_listing())
    assert custom.exists(), "publish did not honour SAMUS_CAPABILITY_MARKETPLACE_PATH"
    with custom.open("r", encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    assert rows and rows[0]["op"] == OP_PUBLISH


# ---------------------------------------------------------------------------
# append_publish / append_withdraw shape
# ---------------------------------------------------------------------------
def test_append_publish_writes_expected_row(tmp_path):
    listing = _listing(tags=("realtime", "vetted"))
    assert append_publish(listing) is True
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["op"] == OP_PUBLISH
    assert row["capability_id"] == "trend_forecasting"
    assert row["provider_agent"] == "research_agent_7"
    assert row["cost"] == 10
    assert row["performance_score"] == pytest.approx(0.91)
    assert row["latency_ms"] == 800
    assert row["tags"] == ["realtime", "vetted"]
    assert "ts" in row


def test_append_withdraw_writes_expected_row(tmp_path):
    assert append_withdraw("trend_forecasting", "research_agent_7") is True
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["op"] == OP_WITHDRAW
    assert row["capability_id"] == "trend_forecasting"
    assert row["provider_agent"] == "research_agent_7"


# ---------------------------------------------------------------------------
# Replay-on-startup
# ---------------------------------------------------------------------------
def test_replay_rebuilds_from_ledger_after_restart():
    # Session 1: write via the persistent wrapper.
    mp1 = PersistentCapabilityMarketplace()
    mp1.publish(_listing(provider_agent="p_a", cost=10, performance_score=0.5))
    mp1.publish(_listing(provider_agent="p_b", cost=20, performance_score=0.9))
    del mp1

    # Session 2: fresh in-memory marketplace picks up prior state.
    mp2 = load_marketplace()
    providers = {l.provider_agent for l in mp2.list_providers("trend_forecasting")}
    assert providers == {"p_a", "p_b"}
    assert mp2.all_capabilities() == ["trend_forecasting"]


def test_replay_honours_republish_overwrite_order():
    # Later publish wins for the same (provider, capability).
    append_publish(_listing(provider_agent="p_a", cost=10, performance_score=0.4))
    append_publish(_listing(provider_agent="p_a", cost=99, performance_score=0.95))

    mp = load_marketplace()
    out = mp.list_providers("trend_forecasting")
    assert len(out) == 1
    assert out[0].cost == 99
    assert out[0].performance_score == pytest.approx(0.95)


def test_replay_applies_withdraw_after_publish():
    append_publish(_listing(provider_agent="p_gone"))
    append_withdraw("trend_forecasting", "p_gone")

    mp = load_marketplace()
    assert mp.list_providers("trend_forecasting") == []


def test_replay_skips_malformed_rows_and_reports_applied_count(tmp_path):
    # Seed one valid row and two malformed rows directly.
    path = tmp_path / "state" / "strategy" / "capability_marketplace.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "ts": "2026-07-06T00:00:00Z",
                    "op": OP_PUBLISH,
                    "capability_id": "ok_cap",
                    "provider_agent": "p_ok",
                    "cost": 5,
                    "performance_score": 0.8,
                    "latency_ms": 100,
                    "tags": [],
                }
            )
            + "\n"
        )
        # Missing required fields:
        fh.write(json.dumps({"op": OP_PUBLISH, "capability_id": "broken"}) + "\n")
        # Unknown op:
        fh.write(json.dumps({"op": "explode", "capability_id": "x", "provider_agent": "y"}) + "\n")

    mp = CapabilityMarketplace()
    applied = replay_into(mp)
    assert applied == 1
    assert mp.all_capabilities() == ["ok_cap"]


# ---------------------------------------------------------------------------
# PersistentCapabilityMarketplace — mutation wrapper
# ---------------------------------------------------------------------------
def test_persistent_publish_reaches_ledger_and_memory(tmp_path):
    mp = PersistentCapabilityMarketplace()
    listing = _listing()
    mp.publish(listing)

    assert mp.list_providers("trend_forecasting") == [listing]
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["op"] == OP_PUBLISH


def test_persistent_withdraw_only_appends_on_hit(tmp_path):
    mp = PersistentCapabilityMarketplace()
    mp.publish(_listing())
    # Real hit → publish row + withdraw row.
    assert mp.withdraw("trend_forecasting", "research_agent_7") is True
    # Miss → no additional withdraw row appended.
    assert mp.withdraw("trend_forecasting", "research_agent_7") is False

    rows = _ledger_rows(tmp_path)
    ops = [r["op"] for r in rows]
    assert ops == [OP_PUBLISH, OP_WITHDRAW]  # exactly one publish + one withdraw


def test_persistent_replay_off_starts_empty():
    """``replay=False`` gives a fresh instance without touching the ledger."""
    append_publish(_listing(provider_agent="p_prior"))
    mp = PersistentCapabilityMarketplace(replay=False)
    assert mp.list_providers("trend_forecasting") == []


def test_publish_survives_ledger_write_failure(monkeypatch, tmp_path):
    """A ledger append error must not raise into the caller."""
    from backend.strategy import capability_marketplace_store as store_mod

    class _ExplodingLedger:
        def append(self, record):  # noqa: ARG002
            raise OSError("disk on fire")

    monkeypatch.setattr(store_mod, "_ledger", lambda: _ExplodingLedger())

    # Both helpers must return False, never raise.
    assert append_publish(_listing()) is False
    assert append_withdraw("trend_forecasting", "research_agent_7") is False
