"""Daily-cap tests for stake_sentence budget — fail-closed semantics."""

from __future__ import annotations

import pytest

from backend.common import stake_sentence_budget as b


def _fresh_store(tmp_path, monkeypatch, cap=3, now=None):
    json_path = tmp_path / "budget.json"
    monkeypatch.setenv("SAMUS_STAKE_SENTENCE_BUDGET_PATH", str(json_path))
    monkeypatch.setenv("SAMUS_STAKE_SENTENCE_DAILY_CAP", str(cap))
    monkeypatch.delenv("DDB_STAKE_SENTENCE_BUDGETS_TABLE", raising=False)
    b.reset_store()
    store = b.StakeSentenceBudgetStore(
        daily_cap=cap,
        ddb_table=None,
        json_path=str(json_path),
        now_func=now,
    )
    return store, json_path


def test_record_use_increments_and_persists(tmp_path, monkeypatch):
    store, _ = _fresh_store(tmp_path, monkeypatch, cap=3)
    assert store.count_today() == 0
    assert store.remaining_today() == 3
    store.record_use("op_a")
    store.record_use("op_b")
    assert store.count_today() == 2
    assert store.remaining_today() == 1


def test_exhaustion_raises(tmp_path, monkeypatch):
    store, _ = _fresh_store(tmp_path, monkeypatch, cap=2)
    store.record_use("op_a")
    store.record_use("op_b")
    with pytest.raises(b.StakeSentenceBudgetUnavailable) as exc:
        store.record_use("op_c")
    assert "daily_cap_exhausted" in str(exc.value)


def test_daily_reset(tmp_path, monkeypatch):
    day1_clock = [1_700_000_000.0]  # ~2023-11-14 22:13Z

    def clock1():
        return day1_clock[0]

    store, json_path = _fresh_store(tmp_path, monkeypatch, cap=3, now=clock1)
    store.record_use("op_day1_a")
    store.record_use("op_day1_b")
    assert store.count_today() == 2

    # Same JSON file, new store with a clock 48 hours later.
    day2_clock = [1_700_000_000.0 + 48 * 3600]

    def clock2():
        return day2_clock[0]

    store2 = b.StakeSentenceBudgetStore(
        daily_cap=3,
        ddb_table=None,
        json_path=str(json_path),
        now_func=clock2,
    )
    assert store2.count_today() == 0
    assert store2.remaining_today() == 3


def test_reset_today_zeroes_counter(tmp_path, monkeypatch):
    store, _ = _fresh_store(tmp_path, monkeypatch, cap=5)
    store.record_use("op_a")
    store.record_use("op_b")
    store.reset_today()
    assert store.count_today() == 0


def test_record_use_requires_opportunity_id(tmp_path, monkeypatch):
    store, _ = _fresh_store(tmp_path, monkeypatch, cap=5)
    with pytest.raises(ValueError):
        store.record_use("   ")


def test_fail_closed_on_unwritable_dir(tmp_path, monkeypatch):
    # Point at a path whose parent doesn't exist AND can't be created. We
    # simulate that by pointing into a regular file's name as a directory.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    bad_path = blocker / "child" / "budget.json"
    monkeypatch.setenv("SAMUS_STAKE_SENTENCE_BUDGET_PATH", str(bad_path))
    monkeypatch.setenv("SAMUS_STAKE_SENTENCE_DAILY_CAP", "3")
    monkeypatch.delenv("DDB_STAKE_SENTENCE_BUDGETS_TABLE", raising=False)
    b.reset_store()
    store = b.StakeSentenceBudgetStore(
        daily_cap=3,
        ddb_table=None,
        json_path=str(bad_path),
    )
    with pytest.raises(b.StakeSentenceBudgetUnavailable):
        store.record_use("op_a")


def test_get_store_reads_env_cap(tmp_path, monkeypatch):
    json_path = tmp_path / "env_budget.json"
    monkeypatch.setenv("SAMUS_STAKE_SENTENCE_BUDGET_PATH", str(json_path))
    monkeypatch.setenv("SAMUS_STAKE_SENTENCE_DAILY_CAP", "7")
    monkeypatch.delenv("DDB_STAKE_SENTENCE_BUDGETS_TABLE", raising=False)
    b.reset_store()
    store = b.get_store()
    assert store.daily_cap == 7
