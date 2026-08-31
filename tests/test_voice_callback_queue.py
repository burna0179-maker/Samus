"""Date-anchored callback queue + closure detection (deferred-lead capability)."""

from __future__ import annotations

import pytest

import backend.voice.callback_queue as cbq
import backend.voice.closure_detector as cd


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    return tmp_path


# ── callback_queue ────────────────────────────────────────────────────────


def test_schedule_and_due(isolated):
    cbq.schedule_callback(
        prospect_id="p1",
        callback_date="2026-07-06",
        company="Kattuah DDS",
        phone="(530) 555-0117",
        reason="closed for vacation",
    )
    # Not due before the date.
    assert cbq.get_due_callbacks(today="2026-07-01") == []
    # Due on/after the date.
    due = cbq.get_due_callbacks(today="2026-07-06")
    assert len(due) == 1 and due[0]["prospect_id"] == "p1"
    due = cbq.get_due_callbacks(today="2026-07-10")
    assert len(due) == 1


def test_reschedule_is_idempotent(isolated):
    cbq.schedule_callback(prospect_id="p1", callback_date="2026-07-06")
    out = cbq.schedule_callback(prospect_id="p1", callback_date="2026-07-08")
    assert out["updated"] is True
    due = cbq.get_due_callbacks(today="2026-07-08")
    assert len(due) == 1 and due[0]["callback_date"] == "2026-07-08"


def test_mark_done_removes_from_due(isolated):
    cbq.schedule_callback(prospect_id="p1", callback_date="2026-07-06")
    assert cbq.mark_done("p1") == 1
    assert cbq.get_due_callbacks(today="2026-07-10") == []


def test_bad_date_rejected(isolated):
    out = cbq.schedule_callback(prospect_id="p1", callback_date="not-a-date")
    assert out["scheduled"] is False and out["reason"] == "bad_date"


def test_durable_path_is_under_artifacts(isolated):
    """The queue must live under the host-bound artifacts dir (storage.root),
    so a deferred lead survives a host crash."""
    cbq.schedule_callback(prospect_id="p1", callback_date="2026-07-06")
    p = cbq._queue_path()
    assert "artifacts" in str(p) or str(isolated) in str(p)
    assert p.exists()


# ── closure_detector ──────────────────────────────────────────────────────


def test_no_closure_keywords_skips_llm():
    """No closure language → return immediately, never call the LLM."""
    called = []

    def _llm(**kw):
        called.append(kw)
        return "2026-07-06"

    out, reason = cd.detect_closure_callback(
        "Hi you've reached Acme, leave a message after the beep.",
        today="2026-06-30",
        llm=_llm,
    )
    assert out is None
    assert called == []  # LLM never invoked on a non-closure voicemail


def test_closure_with_reopen_date_extracted():
    def _llm(**kw):
        return "2026-07-06"

    out, reason = cd.detect_closure_callback(
        "Our office will be closed from June twenty ninth through July fifth, "
        "reopening July sixth.",
        today="2026-06-30",
        llm=_llm,
    )
    assert out == "2026-07-06"
    assert "reopen" in reason.lower()


def test_llm_none_response_yields_no_callback():
    out, _ = cd.detect_closure_callback(
        "We are closed today for the holiday.",
        today="2026-06-30",
        llm=lambda **kw: "NONE",
    )
    assert out is None


def test_past_date_rejected():
    """A reopen date in the past is nonsensical → no callback."""
    out, _ = cd.detect_closure_callback(
        "We were closed, reopened last week.",
        today="2026-06-30",
        llm=lambda **kw: "2026-06-01",
    )
    assert out is None


def test_garbage_llm_output_safe():
    out, _ = cd.detect_closure_callback(
        "We will be closed for a while.",
        today="2026-06-30",
        llm=lambda **kw: "sometime soon maybe",
    )
    assert out is None


def test_pending_future_ids_for_dialer_skip_gate(isolated):
    cbq.schedule_callback(prospect_id="p_future", callback_date="2026-07-06")
    cbq.schedule_callback(prospect_id="p_due", callback_date="2026-06-15")
    fut = cbq.pending_future_ids(today="2026-06-30")
    assert fut == {"p_future": "2026-07-06"}  # only the future one (skip it)
    # past/due one is NOT in the skip set (it should be dialed, not deferred)
    assert "p_due" not in fut
