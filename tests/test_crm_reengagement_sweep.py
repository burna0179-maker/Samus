"""Re-engagement sweep — soft-no resurfacing logic.

Covers ``backend.crm.reengagement_sweep.scan_for_reengagement_triggers``:
the flag-dormant default, the cooldown gate, the in-flight + hard-no skip
passes, the per-run cap, and the daily counter ledger that drives the
forge-ui HUD ``reengagement_queued_today`` field. Mirrors the in-memory
table-shim pattern from ``test_crm_follow_ups.py`` so the sweep is testable
in-process with no AWS and no real cash_engine queue.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any

import pytest

import backend.crm.persistence as p
from backend.cash_engine.models import RevenueTriggerResult


# ---------------------------------------------------------------------------
# Fixtures + shims
# ---------------------------------------------------------------------------


class _FakeTable:
    """In-memory DDB shim — single-attr equality FilterExpression."""

    def __init__(self) -> None:
        self.items: dict[tuple, dict[str, Any]] = {}

    def put_item(self, Item: dict[str, Any]) -> None:
        key_attr = next(iter(Item.keys()))
        self.items[(key_attr, Item[key_attr])] = dict(Item)

    def get_item(self, Key: dict[str, Any]) -> dict[str, Any]:
        key_attr, key_val = next(iter(Key.items()))
        item = self.items.get((key_attr, key_val))
        return {"Item": item} if item else {}

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        fe = kwargs.get("FilterExpression", "")
        vals = kwargs.get("ExpressionAttributeValues", {}) or {}
        names = kwargs.get("ExpressionAttributeNames", {}) or {}
        out = list(self.items.values())
        if fe and isinstance(fe, str) and "=" in fe:
            attr = names.get("#f")
            target = vals.get(":v")
            if attr is not None:
                out = [it for it in out if str(it.get(attr, "")) == str(target or "")]
        return {"Items": out[: kwargs.get("Limit", 50)]}


@dataclass
class _FakeOpp:
    """Duck-typed Opportunity stand-in — only the id field matters here."""

    opportunity_id: str
    prospect_id: str = ""
    stage: str = "qualified"


class _FakeCrm:
    """Stub of backend.crm.service.

    Only the two helpers the sweep calls are needed:
      * ``get_opportunity_for_prospect`` — used to find a deal per prospect.
      * (the persistence-layer table accessor is monkeypatched directly on
        ``backend.crm.persistence`` so the sweep's safe_scan still works.)
    """

    def __init__(self) -> None:
        self.by_prospect: dict[str, _FakeOpp] = {}
        self.lookup_calls: list[str] = []

    def add_opportunity_for(
        self, prospect_id: str, *, opp_id: str = None, stage: str = "qualified"
    ) -> None:
        opp = _FakeOpp(
            opportunity_id=opp_id or f"op_{prospect_id}",
            prospect_id=prospect_id,
            stage=stage,
        )
        self.by_prospect[prospect_id] = opp

    def get_opportunity_for_prospect(self, prospect_id: str) -> _FakeOpp | None:
        self.lookup_calls.append(prospect_id)
        return self.by_prospect.get(prospect_id)


@pytest.fixture
def fake_state_root(monkeypatch, tmp_path):
    """Point SAMUS_STATE_ROOT at a tempdir so ledger writes are scoped."""
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_call_state_table(monkeypatch):
    """Swap the DDB call-state table accessor for an in-memory shim."""
    table = _FakeTable()
    monkeypatch.setattr(p, "_call_state_table", lambda: table)
    return table


@pytest.fixture
def fake_crm():
    return _FakeCrm()


@pytest.fixture
def armed(monkeypatch):
    """Arm the re-engagement flag for the duration of the test."""
    monkeypatch.setenv("SAMUS_REENGAGEMENT_ENABLED", "true")
    monkeypatch.setenv("SAMUS_CASH_ENGINE_ENABLED", "true")
    monkeypatch.setenv("SAMUS_REENGAGEMENT_COOLDOWN_DAYS", "30")
    monkeypatch.setenv("SAMUS_REENGAGEMENT_MAX_PER_RUN", "25")
    # Drop the Settings cache so the env-bind picks up the new vars.
    from backend.common.config import reload_settings

    reload_settings()
    yield
    reload_settings()


def _accepting_review_stub(captured: list):
    """A ``review_opportunity`` stand-in that always enqueues.

    Captures every RevenueTriggerRequest it sees so we can assert
    trigger_source and per-call detail.
    """

    def _review(req, *, crm=None, decay_risk=None):  # noqa: ARG001 - match sig
        captured.append(req)
        return RevenueTriggerResult(
            accepted=True,
            status="enqueued",
            prospect_id=req.prospect_id,
            opportunity_id=f"op_{req.prospect_id}",
            task_id=f"ce-{req.prospect_id}",
            queue="mock:jsonl",
        )

    return _review


_NOW = _dt.datetime(2026, 7, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _stamp(days_ago: float) -> str:
    return (_NOW - _dt.timedelta(days=days_ago)).strftime(_FMT)


def _put_call_state(
    table: _FakeTable,
    prospect_id: str,
    *,
    last_outcome: str = "not_interested",
    state: str = "completed",
    days_ago: float = 60,
) -> None:
    table.put_item(
        {
            "prospect_id": prospect_id,
            "state": state,
            "last_outcome": last_outcome,
            "updated_at": _stamp(days_ago),
            "attempt_count": 1,
        }
    )


# ---------------------------------------------------------------------------
# Dormancy
# ---------------------------------------------------------------------------


def test_sweep_is_dormant_by_default(
    monkeypatch,
    fake_state_root,
    fake_call_state_table,
    fake_crm,
):
    """SAMUS_REENGAGEMENT_ENABLED unset → sweep returns an empty result and
    never touches the table scan."""
    # Ensure the env-var is NOT set (the conftest doesn't set it).
    monkeypatch.delenv("SAMUS_REENGAGEMENT_ENABLED", raising=False)
    from backend.common.config import reload_settings

    reload_settings()

    _put_call_state(fake_call_state_table, "pr_dormant", days_ago=120)
    fake_crm.add_opportunity_for("pr_dormant")

    from backend.crm.reengagement_sweep import scan_for_reengagement_triggers

    captured = []
    result = scan_for_reengagement_triggers(
        crm=fake_crm,
        review=_accepting_review_stub(captured),
    )

    assert result.scanned == 0
    assert result.enqueued == 0
    assert captured == []
    reload_settings()


def test_sweep_short_circuits_when_cash_engine_disabled(
    armed,
    monkeypatch,
    fake_state_root,
    fake_call_state_table,
    fake_crm,
):
    """cash_engine_enabled=False → sweep is also a no-op (the front door
    would refuse anyway; saving the scan + ledger pressure is the right call).
    """
    monkeypatch.setenv("SAMUS_CASH_ENGINE_ENABLED", "false")
    from backend.common.config import reload_settings

    reload_settings()

    _put_call_state(fake_call_state_table, "pr_offline", days_ago=120)
    fake_crm.add_opportunity_for("pr_offline")

    from backend.crm.reengagement_sweep import scan_for_reengagement_triggers

    captured = []
    result = scan_for_reengagement_triggers(
        crm=fake_crm,
        review=_accepting_review_stub(captured),
    )

    assert result.scanned == 0
    assert captured == []


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_sweep_resurfaces_aged_not_interested_prospect(
    armed,
    fake_state_root,
    fake_call_state_table,
    fake_crm,
):
    """A not_interested CallState past the cooldown with an Opportunity
    fires exactly one review with trigger_source='reengagement'."""
    _put_call_state(fake_call_state_table, "pr_kelly", days_ago=60)
    fake_crm.add_opportunity_for("pr_kelly", opp_id="op_kelly")

    from backend.crm.reengagement_sweep import scan_for_reengagement_triggers

    captured = []
    result = scan_for_reengagement_triggers(
        now=_NOW,
        crm=fake_crm,
        review=_accepting_review_stub(captured),
    )

    assert result.scanned == 1
    assert result.eligible == 1
    assert result.enqueued == 1
    assert result.escalated == 0
    assert result.cooldown_days == 30
    assert len(captured) == 1
    req = captured[0]
    assert req.prospect_id == "pr_kelly"
    assert req.trigger_source == "reengagement"
    assert "soft-no cooldown 30d" in req.trigger_reason
    assert "days_quiet=60" in req.current_samus_state


def test_sweep_writes_counter_ledger_and_count_queued_today_ticks(
    armed,
    fake_state_root,
    fake_call_state_table,
    fake_crm,
):
    """A successful enqueue appends to the daily-counter ledger so the
    forge-ui HUD ``reengagement_queued_today`` field is non-zero."""
    _put_call_state(fake_call_state_table, "pr_ledger", days_ago=45)
    fake_crm.add_opportunity_for("pr_ledger")

    from backend.crm.reengagement_sweep import (
        count_queued_today,
        scan_for_reengagement_triggers,
    )

    captured = []
    scan_for_reengagement_triggers(
        now=_NOW,
        crm=fake_crm,
        review=_accepting_review_stub(captured),
    )

    # Counter for *today* (today is the test process's UTC today — the ledger
    # writes iso_now() server-side, NOT the sweep's `now` argument, because
    # the ledger is wall-clock telemetry not historical replay).
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    assert count_queued_today(today=today) == 1

    # Yesterday's bucket is empty (records belong to today only).
    yesterday = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    assert count_queued_today(today=yesterday) == 0


def test_count_queued_today_returns_zero_on_missing_ledger(fake_state_root):
    """No ledger file -> 0, not an exception (HUD must never 500 on dormant)."""
    from backend.crm.reengagement_sweep import count_queued_today

    assert count_queued_today() == 0


# ---------------------------------------------------------------------------
# Skip passes
# ---------------------------------------------------------------------------


def test_sweep_skips_within_cooldown(
    armed,
    fake_state_root,
    fake_call_state_table,
    fake_crm,
):
    """A not_interested row whose updated_at is INSIDE the cooldown is
    counted as ``skipped_within_cooldown`` and NOT enqueued."""
    # 10 days < 30 day default cooldown.
    _put_call_state(fake_call_state_table, "pr_fresh", days_ago=10)
    fake_crm.add_opportunity_for("pr_fresh")

    from backend.crm.reengagement_sweep import scan_for_reengagement_triggers

    captured = []
    result = scan_for_reengagement_triggers(
        now=_NOW,
        crm=fake_crm,
        review=_accepting_review_stub(captured),
    )

    assert result.scanned == 1
    assert result.eligible == 0
    assert result.enqueued == 0
    assert result.skipped_within_cooldown == 1
    assert captured == []


@pytest.mark.parametrize(
    "in_flight_state",
    [
        "outreach_sent",
        "queued",
        "dialing",
        "in_progress",
    ],
)
def test_sweep_skips_in_flight_prospects(
    armed,
    fake_state_root,
    fake_call_state_table,
    fake_crm,
    in_flight_state,
):
    """A not_interested row whose CURRENT state is mid-flight (e.g. Kelly's
    parked outreach_sent) is skipped so we don't double-message."""
    _put_call_state(
        fake_call_state_table,
        "pr_inflight",
        days_ago=60,
        state=in_flight_state,
    )
    fake_crm.add_opportunity_for("pr_inflight")

    from backend.crm.reengagement_sweep import scan_for_reengagement_triggers

    captured = []
    result = scan_for_reengagement_triggers(
        now=_NOW,
        crm=fake_crm,
        review=_accepting_review_stub(captured),
    )

    assert result.scanned == 1
    assert result.eligible == 0
    assert result.enqueued == 0
    assert result.skipped_in_flight == 1
    assert captured == []


@pytest.mark.parametrize("hard_no_outcome", ["do_not_call", "disqualified"])
def test_sweep_skips_hard_no_outcomes_defense_in_depth(
    armed,
    fake_state_root,
    fake_call_state_table,
    fake_crm,
    hard_no_outcome,
):
    """The scan filter is `last_outcome=="not_interested"`, but the
    defence-in-depth pass MUST also catch a leaked do_not_call /
    disqualified row (e.g. from a malformed scan-shim or future schema
    drift). The hard nos can never be resurfaced.
    """
    # Even though our shim filters before the defence pass, force a hard-no
    # row to leak by tagging last_outcome="not_interested" on the table but
    # overwriting last_outcome inside the row to a hard-no value... actually
    # the shim's filter compares the stored row, so simulate the malformed
    # leak by inserting a row whose stored last_outcome is the hard-no and
    # bypass the filter by stamping the right value at scan time. Simplest:
    # put a row tagged with the hard-no AND a row tagged with the soft-no,
    # then confirm the hard-no never enqueues regardless of how filter works.
    fake_call_state_table.put_item(
        {
            "prospect_id": "pr_hard_no",
            "state": "completed",
            "last_outcome": hard_no_outcome,
            "updated_at": _stamp(60),
            "attempt_count": 1,
        }
    )
    # ALSO seed the eligibility check: a hard-no row that somehow tags itself
    # with last_outcome="not_interested" up top would leak into the loop;
    # simulate THAT by directly invoking the defence skip:
    fake_call_state_table.put_item(
        {
            "prospect_id": "pr_soft_no",
            "state": "completed",
            # The scan filter sees "not_interested" and lets this row in...
            "last_outcome": "not_interested",
            "updated_at": _stamp(60),
            "attempt_count": 1,
        }
    )
    fake_crm.add_opportunity_for("pr_hard_no")
    fake_crm.add_opportunity_for("pr_soft_no")

    from backend.crm.reengagement_sweep import scan_for_reengagement_triggers

    captured = []
    result = scan_for_reengagement_triggers(
        now=_NOW,
        crm=fake_crm,
        review=_accepting_review_stub(captured),
    )

    # The hard-no row's stored last_outcome doesn't match the scan filter,
    # so the shim's FilterExpression already excludes it -> the scanned
    # count reflects only the soft-no row, which is enqueued normally. This
    # confirms the FILTER LAYER excludes hard nos as designed. The defence
    # pass inside the sweep is exercised by the next test (which spoofs the
    # scan to return the hard-no row anyway).
    assert all(req.prospect_id != "pr_hard_no" for req in captured)
    assert any(req.prospect_id == "pr_soft_no" for req in captured)
    assert result.enqueued == 1


def test_sweep_defence_in_depth_skips_hard_no_even_if_filter_leaks(
    armed,
    fake_state_root,
    monkeypatch,
    fake_crm,
):
    """Simulate a malformed scan that leaks a hard-no row anyway (schema
    drift; bad filter expression). The in-loop defence pass must still
    refuse to resurface it.
    """
    leaked_hard_no = {
        "prospect_id": "pr_leaked",
        "state": "completed",
        "last_outcome": "do_not_call",  # hard no
        "updated_at": _stamp(60),
        "attempt_count": 1,
    }

    class _LeakyTable:
        def scan(self, **kwargs):
            return {"Items": [leaked_hard_no]}

    monkeypatch.setattr(p, "_call_state_table", _LeakyTable)
    fake_crm.add_opportunity_for("pr_leaked")

    from backend.crm.reengagement_sweep import scan_for_reengagement_triggers

    captured = []
    result = scan_for_reengagement_triggers(
        now=_NOW,
        crm=fake_crm,
        review=_accepting_review_stub(captured),
    )

    assert result.scanned == 1
    assert result.skipped_hard_no == 1
    assert result.enqueued == 0
    assert captured == []


def test_sweep_skips_no_opportunity_prospects(
    armed,
    fake_state_root,
    fake_call_state_table,
    fake_crm,
):
    """A qualifying CallState whose prospect has no Opportunity is counted
    as ``skipped_no_opportunity`` — nothing to resurface."""
    _put_call_state(fake_call_state_table, "pr_no_opp", days_ago=60)
    # NB: fake_crm.add_opportunity_for(...) NOT called for pr_no_opp.

    from backend.crm.reengagement_sweep import scan_for_reengagement_triggers

    captured = []
    result = scan_for_reengagement_triggers(
        now=_NOW,
        crm=fake_crm,
        review=_accepting_review_stub(captured),
    )

    assert result.scanned == 1
    assert result.eligible == 0
    assert result.enqueued == 0
    assert result.skipped_no_opportunity == 1
    assert captured == []


# ---------------------------------------------------------------------------
# Cap + cooldown override
# ---------------------------------------------------------------------------


def test_sweep_honors_per_run_cap(
    armed,
    fake_state_root,
    fake_call_state_table,
    fake_crm,
):
    """When eligible candidates exceed max_per_run, the surplus is counted
    as ``capped`` and not enqueued."""
    for i in range(5):
        pid = f"pr_capped_{i}"
        _put_call_state(fake_call_state_table, pid, days_ago=60)
        fake_crm.add_opportunity_for(pid)

    from backend.crm.reengagement_sweep import scan_for_reengagement_triggers

    captured = []
    result = scan_for_reengagement_triggers(
        now=_NOW,
        crm=fake_crm,
        max_per_run=2,
        review=_accepting_review_stub(captured),
    )

    assert result.scanned == 5
    assert result.eligible == 5
    assert result.enqueued == 2
    assert result.capped == 3
    assert len(captured) == 2


def test_sweep_cooldown_override_lets_fresher_rows_through(
    armed,
    fake_state_root,
    fake_call_state_table,
    fake_crm,
):
    """An explicit shorter cooldown_days argument bypasses the default
    30-day window — useful for tests + operator-triggered ad-hoc runs."""
    # 10 days < 30 day default but >= 7 day override -> eligible.
    _put_call_state(fake_call_state_table, "pr_short_cd", days_ago=10)
    fake_crm.add_opportunity_for("pr_short_cd")

    from backend.crm.reengagement_sweep import scan_for_reengagement_triggers

    captured = []
    result = scan_for_reengagement_triggers(
        now=_NOW,
        crm=fake_crm,
        cooldown_days=7,
        review=_accepting_review_stub(captured),
    )

    assert result.scanned == 1
    assert result.eligible == 1
    assert result.enqueued == 1
    assert result.cooldown_days == 7
    assert len(captured) == 1


# ---------------------------------------------------------------------------
# Integration — log not_interested, fast-forward, run sweep, assert enqueue
# ---------------------------------------------------------------------------


def test_integration_log_not_interested_then_sweep_enqueues(
    armed,
    fake_state_root,
    fake_call_state_table,
    fake_crm,
):
    """End-to-end: an outcome upsert that lands ``last_outcome='not_interested'``
    into the (faked) call-state table, then a sweep run with the clock
    fast-forwarded past the cooldown, ends up enqueued to the cash_engine.

    This is the closest in-process integration we can run without spinning up
    DDB + SQS — the persistence layer's scan + the sweep's review hand-off
    are both exercised on real production code paths.
    """
    # Step 1 — emulate ``backend.crm.service.upsert_call_state``: the only
    # field the sweep cares about is last_outcome + updated_at + state, so
    # we put the row directly through the persistence table.
    fake_call_state_table.put_item(
        {
            "prospect_id": "pr_integration",
            "state": "completed",
            "last_outcome": "not_interested",
            # Initially: 1 day ago (well inside the 30-day cooldown).
            "updated_at": _stamp(1),
            "attempt_count": 1,
        }
    )
    fake_crm.add_opportunity_for("pr_integration")

    from backend.crm.reengagement_sweep import scan_for_reengagement_triggers

    captured: list = []

    # Step 2 — run the sweep AT THE LOG TIME: row is fresh -> skipped.
    result_now = scan_for_reengagement_triggers(
        now=_NOW,
        crm=fake_crm,
        review=_accepting_review_stub(captured),
    )
    assert result_now.skipped_within_cooldown == 1
    assert result_now.enqueued == 0
    assert captured == []

    # Step 3 — fast-forward 45 days past the cooldown (re-write updated_at
    # would be a real-world clock change; in-test we simply pass a later
    # ``now`` to the sweep).
    future = _NOW + _dt.timedelta(days=45)
    result_later = scan_for_reengagement_triggers(
        now=future,
        crm=fake_crm,
        review=_accepting_review_stub(captured),
    )

    assert result_later.eligible == 1
    assert result_later.enqueued == 1
    assert len(captured) == 1
    assert captured[0].prospect_id == "pr_integration"
    assert captured[0].trigger_source == "reengagement"
