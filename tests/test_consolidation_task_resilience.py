"""Restart-resilience for the nightly consolidation loop (2026-07-07 fix).

Pins the last-run-day marker + boot catch-up + strict one-run-per-day
idempotency added to :mod:`backend.cognitive.consolidation_task`. The original
loop was a ``sleep(seconds_until_next_fire); run`` one-shot that silently never
fired across the gateway's frequent container recreates (it re-slept to the
*next* 02:00 on every boot); it went dark ~7.6 days. These tests lock in the new
behavior without touching ``run_consolidation`` itself.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # Marker + every consolidator ledger lands under tmp; never touch host state.
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "jsonl")
    monkeypatch.setenv("SAMUS_CONVERSION_FUNNEL_PATH", str(tmp_path / "funnel.jsonl"))
    monkeypatch.setenv("SAMUS_REWARD_PERSIST_PATH", str(tmp_path / "rewards.jsonl"))
    monkeypatch.setenv("SAMUS_ATTRIBUTION_PATH", str(tmp_path / "attr.json"))
    monkeypatch.setenv("SAMUS_FEEDBACK_STORE_PATH", str(tmp_path / "metrics.json"))
    monkeypatch.setenv("SAMUS_CONSOLIDATION_LLM_ENABLED", "0")
    monkeypatch.setenv("SAMUS_CONSOLIDATION_HOUR", "2")
    monkeypatch.delenv("SAMUS_CONSOLIDATION_LOOP_ENABLED", raising=False)
    monkeypatch.delenv("DDB_ATTRIBUTION_TABLE", raising=False)
    from backend.attribution import store as attr_store

    attr_store.reset_store()
    yield
    attr_store.reset_store()


def _spy():
    """A fake run_consolidation that records the days it was asked to run."""
    calls: list[str] = []

    def runner(day):
        calls.append(day)
        return {"day": day, "ok": True}

    return calls, runner


# ---------------------------------------------------------------------------
# _scheduled_fire_day / is_consolidation_due (pure)
# ---------------------------------------------------------------------------
class TestScheduledFireDay:
    def test_at_or_after_hour_is_today(self):
        from backend.cognitive import consolidation_task as ct

        assert ct._scheduled_fire_day(datetime(2026, 7, 7, 2, 0), 2) == datetime(2026, 7, 7).date()
        assert (
            ct._scheduled_fire_day(datetime(2026, 7, 7, 18, 30), 2) == datetime(2026, 7, 7).date()
        )

    def test_before_hour_is_yesterday(self):
        from backend.cognitive import consolidation_task as ct

        now = datetime(2026, 7, 7, 1, 30)
        assert ct._scheduled_fire_day(now, 2) == now.date() - timedelta(days=1)


class TestIsConsolidationDue:
    def test_never_run_is_due(self):
        from backend.cognitive import consolidation_task as ct

        due, reason = ct.is_consolidation_due(datetime(2026, 7, 7, 18, 30), None, fire_hour=2)
        assert due is True
        assert "no consolidation on record" in reason

    def test_stale_marker_is_due(self):
        from backend.cognitive import consolidation_task as ct

        due, _ = ct.is_consolidation_due(datetime(2026, 7, 7, 18, 30), "2026-07-05", fire_hour=2)
        assert due is True

    def test_today_marker_not_due(self):
        from backend.cognitive import consolidation_task as ct

        due, reason = ct.is_consolidation_due(
            datetime(2026, 7, 7, 18, 30), "2026-07-07", fire_hour=2
        )
        assert due is False
        assert "already consolidated" in reason

    def test_future_marker_not_due(self):
        # clock-skew / future marker must never trigger a re-run
        from backend.cognitive import consolidation_task as ct

        due, _ = ct.is_consolidation_due(datetime(2026, 7, 7, 18, 30), "2026-07-09", fire_hour=2)
        assert due is False

    def test_before_fire_hour_owes_yesterday(self):
        from backend.cognitive import consolidation_task as ct

        now = datetime(2026, 7, 7, 1, 30)  # before 02:00 -> owed day is 07-06
        assert ct.is_consolidation_due(now, "2026-07-06", fire_hour=2)[0] is False
        assert ct.is_consolidation_due(now, "2026-07-05", fire_hour=2)[0] is True


# ---------------------------------------------------------------------------
# last-run-day marker
# ---------------------------------------------------------------------------
class TestMarker:
    def test_missing_marker_reads_none(self):
        from backend.cognitive import consolidation_task as ct

        assert ct._read_last_run_day() is None

    def test_write_then_read_roundtrip(self):
        from backend.cognitive import consolidation_task as ct

        ct._write_last_run_day("2026-07-07", ok=True)
        assert ct._read_last_run_day() == "2026-07-07"
        payload = json.loads(ct._marker_path().read_text(encoding="utf-8"))
        assert payload["last_run_day"] == "2026-07-07"
        assert payload["ok"] is True
        assert payload["kind"] == "consolidation_last_run"

    def test_corrupt_marker_fails_open_to_none(self):
        from backend.cognitive import consolidation_task as ct

        p = ct._marker_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json", encoding="utf-8")
        assert ct._read_last_run_day() is None

    def test_invalid_day_string_reads_none(self):
        from backend.cognitive import consolidation_task as ct

        p = ct._marker_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"last_run_day": "not-a-date"}), encoding="utf-8")
        assert ct._read_last_run_day() is None


# ---------------------------------------------------------------------------
# run_if_due — the acceptance behavior
# ---------------------------------------------------------------------------
class TestRunIfDue:
    def test_boot_catchup_runs_immediately_and_writes_marker(self):
        # ACCEPTANCE 1: container started at 18:30, no marker for today, now past
        # the fire hour -> consolidation runs immediately (catch-up) + marks.
        from backend.cognitive import consolidation_task as ct

        calls, runner = _spy()
        out = ct.run_if_due(datetime(2026, 7, 7, 18, 30), runner=runner)
        assert out["ran"] is True
        assert calls == ["2026-07-07"]
        assert ct._read_last_run_day() == "2026-07-07"

    def test_marker_set_today_is_noop_and_sleeps_to_next_fire(self):
        # ACCEPTANCE 2: marker already set for today -> does NOT re-run; the loop
        # then sleeps to the NEXT fire (tomorrow 02:00), not today's.
        from backend.cognitive import consolidation_task as ct

        ct._write_last_run_day("2026-07-07", ok=True)
        calls, runner = _spy()
        now = datetime(2026, 7, 7, 18, 30)
        out = ct.run_if_due(now, runner=runner)
        assert out["ran"] is False
        assert calls == []
        assert "already consolidated" in out["reason"]
        # 18:30 -> next 02:00 is 7.5h away (proves it isn't waiting for today's).
        assert ct.seconds_until_next_fire(now) == pytest.approx(7.5 * 3600)

    def test_catchup_then_same_day_tick_is_a_single_run(self):
        # Idempotency (item 3): boot catch-up + a later same-day scheduled tick
        # must consolidate the day exactly once.
        from backend.cognitive import consolidation_task as ct

        calls, runner = _spy()
        ct.run_if_due(datetime(2026, 7, 7, 18, 30), runner=runner)  # boot catch-up
        ct.run_if_due(datetime(2026, 7, 7, 23, 0), runner=runner)  # later same-day tick
        assert calls == ["2026-07-07"]

    def test_before_fire_hour_boot_catches_up_yesterday(self):
        # Boot at 01:30 (before 02:00) with yesterday unconsolidated -> catch up
        # YESTERDAY's missed fire and mark it (not today).
        from backend.cognitive import consolidation_task as ct

        calls, runner = _spy()
        out = ct.run_if_due(datetime(2026, 7, 7, 1, 30), runner=runner)
        assert out["ran"] is True
        assert calls == ["2026-07-06"]
        assert ct._read_last_run_day() == "2026-07-06"

    def test_marker_uses_runner_returned_day(self):
        # The marker is sourced from run_consolidation()'s own "day" field.
        from backend.cognitive import consolidation_task as ct

        def runner(day):
            return {"day": "2026-07-07", "ok": False}

        out = ct.run_if_due(datetime(2026, 7, 7, 18, 30), runner=runner)
        assert out["ran"] is True
        assert out["ok"] is False
        assert ct._read_last_run_day() == "2026-07-07"

    def test_runner_fault_is_fail_soft_and_leaves_marker_unset(self):
        # A run fault must not raise (would kill the loop) and must NOT mark the
        # day -> the next cycle retries.
        from backend.cognitive import consolidation_task as ct

        def boom(day):
            raise RuntimeError("consolidation exploded")

        out = ct.run_if_due(datetime(2026, 7, 7, 18, 30), runner=boom)
        assert out["ran"] is False
        assert "run_if_due-error" in out["reason"]
        assert ct._read_last_run_day() is None


class TestRunIfDueRealConsolidation:
    def test_real_run_writes_marker_then_second_call_noops(self):
        # No runner injected -> exercises the real run_consolidation wiring
        # (fail-soft; empty ledgers make every stage a clean no-op). Proves
        # result["day"] flows into the marker and same-day re-entry is idempotent.
        from backend.cognitive import consolidation_task as ct

        out = ct.run_if_due(datetime(2026, 7, 7, 18, 30))
        assert out["ran"] is True
        assert out["day"] == "2026-07-07"
        assert ct._read_last_run_day() == "2026-07-07"
        out2 = ct.run_if_due(datetime(2026, 7, 7, 23, 59))
        assert out2["ran"] is False
