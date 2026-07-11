"""Tests for backend.outreach.sequences — the nurture engine (pure, no network)."""
from __future__ import annotations

from backend.outreach.sequences import (
    SEQUENCES,
    WELCOME_SEQUENCE,
    Enrollment,
    days_elapsed,
    dispatch_due,
    evaluate_branch,
    due_touches,
    get_sequence,
    mark_sent,
    plan_next,
    record_event,
)


def _enroll(started="2026-06-01T00:00:00+00:00") -> Enrollment:
    return Enrollment(prospect_id="p1", sequence_id="welcome", started_at=started)


def test_registry_has_expected_sequences():
    assert set(SEQUENCES) == {
        "welcome", "onboarding", "reengagement", "buying_signal",
    }
    assert get_sequence("welcome") is WELCOME_SEQUENCE
    assert get_sequence("nope") is None


def test_days_elapsed():
    e = _enroll()
    assert days_elapsed(e, "2026-06-01T06:00:00+00:00") == 0
    assert days_elapsed(e, "2026-06-04T00:00:00+00:00") == 3
    # never negative
    assert days_elapsed(e, "2026-05-30T00:00:00+00:00") == 0


def test_days_elapsed_handles_z_suffix():
    e = Enrollment(prospect_id="p", sequence_id="welcome", started_at="2026-06-01T00:00:00Z")
    assert days_elapsed(e, "2026-06-08T00:00:00Z") == 7


def test_due_touches_by_cadence():
    e = _enroll()
    # Day 0: only step 1 (day 0) is due.
    due = due_touches(WELCOME_SEQUENCE, e, "2026-06-01T01:00:00+00:00")
    assert [t.step for t in due] == [1]
    # Day 5: steps 1 (d0), 2 (d2), 3 (d4) are due; 4 (d7) not yet.
    due = due_touches(WELCOME_SEQUENCE, e, "2026-06-06T00:00:00+00:00")
    assert [t.step for t in due] == [1, 2, 3]


def test_completed_steps_excluded():
    e = _enroll()
    e.completed_steps = [1, 2]
    due = due_touches(WELCOME_SEQUENCE, e, "2026-06-06T00:00:00+00:00")
    assert [t.step for t in due] == [3]


def test_plan_next_sends_first_due():
    e = _enroll()
    plan = plan_next(WELCOME_SEQUENCE, e, "2026-06-01T01:00:00+00:00")
    assert plan["action"] == "send"
    assert plan["touch"].step == 1


def test_plan_next_wait_when_nothing_due():
    e = _enroll()
    e.completed_steps = [1]
    # Day 1: step 2 is day 2, not due yet.
    plan = plan_next(WELCOME_SEQUENCE, e, "2026-06-02T00:00:00+00:00")
    assert plan["action"] == "wait"


def test_branch_unsubscribe_stops():
    e = _enroll()
    record_event(e, "unsubscribed", "2026-06-01T02:00:00+00:00")
    plan = plan_next(WELCOME_SEQUENCE, e, "2026-06-03T00:00:00+00:00")
    assert plan["action"] == "stop"
    assert plan["reason"] == "unsubscribed"


def test_branch_trial_started_switches_to_onboarding():
    e = _enroll()
    record_event(e, "trial_started", "2026-06-02T00:00:00+00:00")
    plan = plan_next(WELCOME_SEQUENCE, e, "2026-06-03T00:00:00+00:00")
    assert plan["action"] == "switch"
    assert plan["target"] == "onboarding"


def test_branch_clicked_fast_tracks_but_continues():
    e = _enroll()
    record_event(e, "clicked", "2026-06-01T02:00:00+00:00")
    plan = plan_next(WELCOME_SEQUENCE, e, "2026-06-01T03:00:00+00:00")
    assert plan["action"] == "send"
    assert plan.get("fast_track") is True


def test_branch_no_open_by_day_7_switches_to_reengagement():
    e = _enroll()
    # No "opened" event, 8 days elapsed -> re-engagement.
    plan = plan_next(WELCOME_SEQUENCE, e, "2026-06-09T00:00:00+00:00")
    assert plan["action"] == "switch"
    assert plan["target"] == "reengagement"


def test_no_open_branch_not_triggered_if_opened():
    e = _enroll()
    record_event(e, "opened", "2026-06-02T00:00:00+00:00")
    # opened present -> no_open branch must not fire; first unsent touch sends.
    plan = plan_next(WELCOME_SEQUENCE, e, "2026-06-09T00:00:00+00:00")
    assert plan["action"] == "send"


def test_dispatch_due_dry_run_does_not_mark():
    e = _enroll()
    out = dispatch_due(WELCOME_SEQUENCE, e, "2026-06-01T01:00:00+00:00", dry_run=True)
    assert out["action"] == "send"
    assert out["message"]["dry_run"] is True
    assert out["message"]["step"] == 1
    assert e.completed_steps == []  # not mutated in dry-run


def test_dispatch_due_live_marks_sent():
    e = _enroll()
    out = dispatch_due(WELCOME_SEQUENCE, e, "2026-06-01T01:00:00+00:00", dry_run=False)
    assert out["action"] == "send"
    assert e.completed_steps == [1]
    # next call advances (step 1 now complete; nothing else due on day 0)
    plan = plan_next(WELCOME_SEQUENCE, e, "2026-06-01T02:00:00+00:00")
    assert plan["action"] == "wait"


def test_complete_when_all_sent():
    e = _enroll()
    for t in WELCOME_SEQUENCE.touches:
        mark_sent(e, t, "2026-06-20T00:00:00+00:00")
    plan = plan_next(WELCOME_SEQUENCE, e, "2026-06-20T00:00:00+00:00")
    assert plan["action"] == "complete"


def test_evaluate_branch_returns_none_when_no_match():
    e = _enroll()
    assert evaluate_branch(WELCOME_SEQUENCE, e, "2026-06-02T00:00:00+00:00") is None
