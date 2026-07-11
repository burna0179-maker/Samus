"""Tests for backend.growth.scheduler.

Covers:
  - Flag OFF => schedule() raises GrowthSchedulerDisabledError.
  - Flag ON + valid schema + enabled action => job enqueued, job_id returned.
  - Unknown action => raises GrowthSchedulerError.
  - Disabled action (group flag OFF) => raises GrowthSchedulerError.
  - Payload missing required field => raises GrowthSchedulerError.
  - list_pending() returns enqueued, non-completed jobs.
  - cancel() removes a job; returns False for unknown id.
  - _tick() processes due jobs; one-shot jobs move to completed.
  - Recurring jobs stay pending after _tick().
  - Disabled (enabled=False) jobs are not ticked.
  - Future run_at jobs are not ticked until due.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from backend.growth.scheduler import (
    GrowthJobSpec,
    GrowthScheduler,
    GrowthSchedulerDisabledError,
    GrowthSchedulerError,
)
import backend.growth.dispatch_policy as _dispatch_policy_mod
import backend.growth.schema_registry as _schema_registry_mod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_ACTION = "geo_format"
_VALID_PAYLOAD = {"query": "dentist near me", "location": "Seattle WA"}

_FLAG = "SAMUS_GROWTH_SCHEDULER_ENABLED"
_SEO_FLAG = "SAMUS_GROWTH_SEO_ENABLED"


def _make_scheduler() -> GrowthScheduler:
    """Return a scheduler wired to the real policy + registry modules."""
    return GrowthScheduler(_dispatch_policy_mod, _schema_registry_mod)


def _make_spec(**kwargs) -> GrowthJobSpec:
    """Return a GrowthJobSpec with sensible defaults."""
    defaults = {
        "action": _VALID_ACTION,
        "payload": dict(_VALID_PAYLOAD),
        "enabled": True,
    }
    defaults.update(kwargs)
    return GrowthJobSpec(**defaults)


# ---------------------------------------------------------------------------
# Flag OFF: schedule() raises GrowthSchedulerDisabledError
# ---------------------------------------------------------------------------


def test_schedule_raises_when_scheduler_flag_off():
    scheduler = _make_scheduler()
    env = {_FLAG: "false", _SEO_FLAG: "true"}
    with patch.dict(os.environ, env, clear=False):
        with pytest.raises(GrowthSchedulerDisabledError):
            scheduler.schedule(_make_spec())


def test_schedule_raises_disabled_error_is_subclass_of_scheduler_error():
    """GrowthSchedulerDisabledError must be a GrowthSchedulerError."""
    assert issubclass(GrowthSchedulerDisabledError, GrowthSchedulerError)


def test_schedule_disabled_error_message_mentions_flag():
    scheduler = _make_scheduler()
    with patch.dict(os.environ, {_FLAG: ""}, clear=False):
        with pytest.raises(GrowthSchedulerDisabledError, match=_FLAG):
            scheduler.schedule(_make_spec())


# ---------------------------------------------------------------------------
# Flag ON + valid payload => job enqueued
# ---------------------------------------------------------------------------


def test_schedule_enqueues_valid_job():
    scheduler = _make_scheduler()
    env = {_FLAG: "true", _SEO_FLAG: "true"}
    with patch.dict(os.environ, env, clear=False):
        job_id = scheduler.schedule(_make_spec())
    assert job_id is not None
    assert isinstance(job_id, str)
    assert len(job_id) > 0


def test_schedule_returns_spec_job_id():
    scheduler = _make_scheduler()
    spec = _make_spec()
    env = {_FLAG: "true", _SEO_FLAG: "true"}
    with patch.dict(os.environ, env, clear=False):
        returned_id = scheduler.schedule(spec)
    assert returned_id == spec.job_id


def test_schedule_job_appears_in_list_pending():
    scheduler = _make_scheduler()
    env = {_FLAG: "true", _SEO_FLAG: "true"}
    with patch.dict(os.environ, env, clear=False):
        job_id = scheduler.schedule(_make_spec())
    pending_ids = {j.job_id for j in scheduler.list_pending()}
    assert job_id in pending_ids


# ---------------------------------------------------------------------------
# Unknown action
# ---------------------------------------------------------------------------


def test_schedule_raises_for_unknown_action():
    scheduler = _make_scheduler()
    env = {_FLAG: "true"}
    with patch.dict(os.environ, env, clear=False):
        with pytest.raises(GrowthSchedulerError, match="Unknown growth action"):
            scheduler.schedule(_make_spec(action="totally_fake_action"))


# ---------------------------------------------------------------------------
# Disabled action (group flag OFF)
# ---------------------------------------------------------------------------


def test_schedule_raises_when_action_group_flag_off():
    scheduler = _make_scheduler()
    # Scheduler enabled but SEO group flag off
    env = {_FLAG: "true", _SEO_FLAG: "false"}
    with patch.dict(os.environ, env, clear=False):
        with pytest.raises(GrowthSchedulerError, match="disabled"):
            scheduler.schedule(_make_spec(action="geo_format"))


# ---------------------------------------------------------------------------
# Payload validation failure
# ---------------------------------------------------------------------------


def test_schedule_raises_on_missing_required_field():
    scheduler = _make_scheduler()
    env = {_FLAG: "true", _SEO_FLAG: "true"}
    # geo_format requires "query" and "location"
    bad_payload = {"query": "coffee shops"}  # missing location
    with patch.dict(os.environ, env, clear=False):
        with pytest.raises(GrowthSchedulerError, match="missing required"):
            scheduler.schedule(_make_spec(payload=bad_payload))


def test_schedule_raises_on_empty_payload():
    scheduler = _make_scheduler()
    env = {_FLAG: "true", _SEO_FLAG: "true"}
    with patch.dict(os.environ, env, clear=False):
        with pytest.raises(GrowthSchedulerError):
            scheduler.schedule(_make_spec(payload={}))


# ---------------------------------------------------------------------------
# list_pending
# ---------------------------------------------------------------------------


def test_list_pending_empty_initially():
    scheduler = _make_scheduler()
    assert scheduler.list_pending() == []


def test_list_pending_returns_multiple_jobs():
    scheduler = _make_scheduler()
    env = {_FLAG: "true", _SEO_FLAG: "true"}
    with patch.dict(os.environ, env, clear=False):
        id1 = scheduler.schedule(_make_spec())
        id2 = scheduler.schedule(_make_spec())
    ids = {j.job_id for j in scheduler.list_pending()}
    assert id1 in ids
    assert id2 in ids


def test_list_pending_returns_snapshot_not_reference():
    scheduler = _make_scheduler()
    env = {_FLAG: "true", _SEO_FLAG: "true"}
    with patch.dict(os.environ, env, clear=False):
        scheduler.schedule(_make_spec())
    snapshot1 = scheduler.list_pending()
    with patch.dict(os.environ, env, clear=False):
        scheduler.schedule(_make_spec())
    snapshot2 = scheduler.list_pending()
    assert len(snapshot2) == len(snapshot1) + 1


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


def test_cancel_removes_job():
    scheduler = _make_scheduler()
    env = {_FLAG: "true", _SEO_FLAG: "true"}
    with patch.dict(os.environ, env, clear=False):
        job_id = scheduler.schedule(_make_spec())
    assert scheduler.cancel(job_id) is True
    ids = {j.job_id for j in scheduler.list_pending()}
    assert job_id not in ids


def test_cancel_returns_false_for_unknown_id():
    scheduler = _make_scheduler()
    assert scheduler.cancel("nonexistent-id-xyz") is False


def test_cancel_twice_returns_false_second_time():
    scheduler = _make_scheduler()
    env = {_FLAG: "true", _SEO_FLAG: "true"}
    with patch.dict(os.environ, env, clear=False):
        job_id = scheduler.schedule(_make_spec())
    assert scheduler.cancel(job_id) is True
    assert scheduler.cancel(job_id) is False


# ---------------------------------------------------------------------------
# _tick: processes due jobs
# ---------------------------------------------------------------------------


def test_tick_processes_due_one_shot_job():
    """A one-shot job with run_at=None (immediate) is processed by _tick."""
    scheduler = _make_scheduler()
    env = {_FLAG: "true", _SEO_FLAG: "true"}
    with patch.dict(os.environ, env, clear=False):
        job_id = scheduler.schedule(_make_spec(enabled=True, run_at=None))

    processed = scheduler._tick()
    assert job_id in processed


def test_tick_one_shot_job_removed_from_pending_after_tick():
    scheduler = _make_scheduler()
    env = {_FLAG: "true", _SEO_FLAG: "true"}
    with patch.dict(os.environ, env, clear=False):
        job_id = scheduler.schedule(_make_spec(enabled=True, run_at=None))
    scheduler._tick()
    ids = {j.job_id for j in scheduler.list_pending()}
    assert job_id not in ids


def test_tick_recurring_job_stays_pending_after_tick():
    """Recurring jobs must remain in pending after being ticked."""
    scheduler = _make_scheduler()
    env = {_FLAG: "true", _SEO_FLAG: "true"}
    with patch.dict(os.environ, env, clear=False):
        job_id = scheduler.schedule(_make_spec(enabled=True, run_at=None, recurrence="0 9 * * 1"))
    scheduler._tick()
    ids = {j.job_id for j in scheduler.list_pending()}
    assert job_id in ids


def test_tick_disabled_job_not_processed():
    """Jobs with enabled=False must not be ticked."""
    scheduler = _make_scheduler()
    env = {_FLAG: "true", _SEO_FLAG: "true"}
    with patch.dict(os.environ, env, clear=False):
        job_id = scheduler.schedule(_make_spec(enabled=False, run_at=None))
    processed = scheduler._tick()
    assert job_id not in processed
    # Still pending
    ids = {j.job_id for j in scheduler.list_pending()}
    assert job_id in ids


def test_tick_future_job_not_processed():
    """Jobs with run_at in the future must not be ticked."""
    scheduler = _make_scheduler()
    env = {_FLAG: "true", _SEO_FLAG: "true"}
    future = datetime.now(tz=timezone.utc) + timedelta(hours=1)
    with patch.dict(os.environ, env, clear=False):
        job_id = scheduler.schedule(_make_spec(enabled=True, run_at=future))
    processed = scheduler._tick()
    assert job_id not in processed
    ids = {j.job_id for j in scheduler.list_pending()}
    assert job_id in ids


def test_tick_past_run_at_is_processed():
    """Jobs with run_at in the past must be ticked."""
    scheduler = _make_scheduler()
    env = {_FLAG: "true", _SEO_FLAG: "true"}
    past = datetime.now(tz=timezone.utc) - timedelta(seconds=1)
    with patch.dict(os.environ, env, clear=False):
        job_id = scheduler.schedule(_make_spec(enabled=True, run_at=past))
    processed = scheduler._tick()
    assert job_id in processed


def test_tick_returns_list_of_processed_ids():
    """_tick() must return a list even when nothing is due."""
    scheduler = _make_scheduler()
    result = scheduler._tick()
    assert isinstance(result, list)


def test_tick_invokes_route_growth_action_when_available():
    """_tick() must call policy.route_growth_action with action + payload."""
    mock_policy = MagicMock()
    mock_policy.is_enabled.return_value = True
    mock_policy.get_entry.return_value = MagicMock(flag="SAMUS_GROWTH_SEO_ENABLED")
    mock_policy.route_growth_action = MagicMock(return_value={"ok": True})

    scheduler = GrowthScheduler(mock_policy, _schema_registry_mod)

    env = {_FLAG: "true", _SEO_FLAG: "true"}
    with patch.dict(os.environ, env, clear=False):
        job_id = scheduler.schedule(_make_spec(enabled=True, run_at=None))

    scheduler._tick()
    mock_policy.route_growth_action.assert_called_once_with(_VALID_ACTION, _VALID_PAYLOAD)


def test_tick_handler_exception_does_not_propagate():
    """A handler that raises must not crash _tick()."""
    mock_policy = MagicMock()
    mock_policy.is_enabled.return_value = True
    mock_policy.get_entry.return_value = MagicMock(flag="SAMUS_GROWTH_SEO_ENABLED")
    mock_policy.route_growth_action = MagicMock(side_effect=RuntimeError("boom"))

    scheduler = GrowthScheduler(mock_policy, _schema_registry_mod)

    env = {_FLAG: "true", _SEO_FLAG: "true"}
    with patch.dict(os.environ, env, clear=False):
        job_id = scheduler.schedule(_make_spec(enabled=True, run_at=None))

    # Must not raise
    processed = scheduler._tick()
    assert job_id in processed


# ---------------------------------------------------------------------------
# dispatch_policy.validate_payload integration
# ---------------------------------------------------------------------------


def test_dispatch_policy_validate_payload_known_action():
    from backend.growth.dispatch_policy import validate_payload

    missing = validate_payload("geo_format", {"query": "x", "location": "y"})
    assert missing == []


def test_dispatch_policy_validate_payload_missing_field():
    from backend.growth.dispatch_policy import validate_payload

    missing = validate_payload("geo_format", {"query": "x"})
    assert "location" in missing


def test_dispatch_policy_validate_payload_unknown_action():
    from backend.growth.dispatch_policy import validate_payload

    result = validate_payload("no_such_action", {"x": 1})
    assert result == ["<unknown action>"]


# ---------------------------------------------------------------------------
# dispatch_policy.GrowthDispatchEntry.schema property
# ---------------------------------------------------------------------------


def test_dispatch_entry_schema_property_returns_correct_schema():
    from backend.growth.dispatch_policy import get_entry

    entry = get_entry("geo_format")
    assert entry is not None
    schema = entry.schema
    assert schema is not None
    assert schema.action == "geo_format"


def test_dispatch_entry_schema_property_all_12_actions():
    from backend.growth.dispatch_policy import GROWTH_DISPATCH_TABLE

    for entry in GROWTH_DISPATCH_TABLE:
        schema = entry.schema
        assert schema is not None, f"entry.schema is None for action={entry.action!r}"
        assert schema.action == entry.action
