"""G11 persistence is fail-OPEN — broken JSON path doesn't crash record_spend.

Per Codex chapter 04: Apollo budget is a cost cap, not a safety cap. Losing
the store means bounded overspend, NOT system unavailability.
"""
from __future__ import annotations

import logging

from backend.common.apollo_budget import ApolloBudgetStore


def _store_with_unwritable_path(path: str, *, cap: float = 10.0) -> ApolloBudgetStore:
    return ApolloBudgetStore(
        ddb_table=None,
        json_path=path,
        daily_cap_usd=lambda: cap,
    )


def test_unwritable_json_path_doesnt_crash(tmp_path, caplog):
    # Path inside a file (treated as not-a-directory) -> save fails.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    broken_path = str(blocker / "child" / "apollo.json")

    s = _store_with_unwritable_path(broken_path)
    with caplog.at_level(logging.WARNING, logger="samus.common.apollo_budget"):
        # Must not raise.
        s.record_spend(0.04, endpoint="people_search")

    # Logged loudly so ops can alert.
    assert any(
        "apollo_budget" in rec.name.lower() and rec.levelno >= logging.WARNING
        for rec in caplog.records
    )


def test_corrupt_json_file_recoverable(tmp_path):
    path = tmp_path / "apollo.json"
    path.write_text("{this is not json")
    s = ApolloBudgetStore(
        ddb_table=None,
        json_path=str(path),
        daily_cap_usd=lambda: 10.0,
    )
    # Load returns None for corrupt file -> fresh budget; record_spend OK.
    s.record_spend(0.04, endpoint="people_search")
    # And future reads work.
    assert s.current_spend_usd() >= 0.04


def test_assert_allows_fails_open_on_unloadable_store(tmp_path, monkeypatch, caplog):
    """If the store can't be read, assert_allows must allow (fail-OPEN)."""
    s = ApolloBudgetStore(
        ddb_table=None,
        json_path=str(tmp_path / "apollo.json"),
        daily_cap_usd=lambda: 10.0,
    )

    def _boom() -> None:
        raise RuntimeError("simulated store outage")

    monkeypatch.setattr(s, "_load", _boom)
    with caplog.at_level(logging.WARNING, logger="samus.common.apollo_budget"):
        s.assert_allows(99999.0)  # would normally trip the cap

    assert any("store unavailable" in rec.message for rec in caplog.records)
