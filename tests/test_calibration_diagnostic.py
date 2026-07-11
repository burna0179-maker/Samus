"""Unit tests for the confidence-calibration + loop-completion diagnostic.

Uses monkey-patched ``read_events`` so the reconcile is exercised over hand-
built decision/outcome events -- no touch of the real telemetry ledger.
Pins:
  * per-bucket hit-rate math (G1 calibration),
  * per-actor breakdown + overall summary,
  * loop-completion rate (G2 proxy),
  * unscoreable-fraction accounting,
  * empty-stream degradation to a valid empty scaffold,
  * ``write_calibration_report`` lands JSON at the storage-rooted path.
"""

from __future__ import annotations

import json

import pytest

from backend.cognitive import calibration_diagnostic as cd


# --- Event builders --------------------------------------------------------


def _decision_event(
    *,
    actor: str,
    confidence: float,
    expected_outcome: str,
    prospect_id: str = "",
    opportunity_id: str = "",
    campaign_id: str = "",
    ts: str = "2026-07-05T12:00:00Z",
    decision_id: str = "",
) -> dict:
    rec = {
        "decision_id": decision_id or f"dec-{actor}-{prospect_id or campaign_id}",
        "actor": actor,
        "why": "test",
        "alternatives_considered": [],
        "data_used": [],
        "expected_outcome": expected_outcome,
        "confidence": confidence,
        "risk_level": "normal",
        "ev_usd": 0.0,
        "ts": ts,
        "prospect_id": prospect_id,
        "opportunity_id": opportunity_id,
        "campaign_id": campaign_id,
        "extra": {},
    }
    return {
        "ts": ts,
        "event_id": f"ev-{rec['decision_id']}",
        "event_type": "decision.made",
        "workcell": actor,
        "prospect_id": prospect_id,
        "opportunity_id": opportunity_id,
        "campaign_id": campaign_id,
        "metadata": {"actor": actor, "decision_record": rec},
    }


def _outcome_event(
    *,
    event_type: str,
    prospect_id: str = "",
    opportunity_id: str = "",
    campaign_id: str = "",
    ts: str = "2026-07-06T12:00:00Z",
) -> dict:
    return {
        "ts": ts,
        "event_id": f"out-{event_type}-{prospect_id or campaign_id}",
        "event_type": event_type,
        "workcell": "test",
        "prospect_id": prospect_id,
        "opportunity_id": opportunity_id,
        "campaign_id": campaign_id,
        "metadata": {},
    }


def _install_stream(monkeypatch, decisions, outcomes):
    """Stub business_events.read_events with our hand-built stream."""
    from backend.common import business_events as be

    def _fake_read_events(**kwargs):
        wanted = kwargs.get("event_types")
        if wanted is None:
            return list(decisions) + list(outcomes)
        wanted = list(wanted)
        if "decision.made" in wanted:
            return list(decisions)
        return list(outcomes)

    monkeypatch.setattr(be, "read_events", _fake_read_events)


# --- reconcile_decisions ---------------------------------------------------


def test_reconcile_empty_stream_returns_scaffold(monkeypatch):
    _install_stream(monkeypatch, [], [])
    out = cd.reconcile_decisions(window_days=7, until_iso="2026-07-08T00:00:00Z")
    assert out["window"]["days"] == 7
    assert out["overall"] == {
        "decisions": 0,
        "unscoreable": 0,
        "loop_completion_rate": 0.0,
    }
    assert out["per_actor"] == {}


def test_reconcile_per_bucket_hit_rate_math(monkeypatch):
    # Three high-confidence decisions from "planner", all intending "booked".
    # Two land a meeting.booked outcome, one doesn't -> hit_rate = 2/3.
    decisions = [
        _decision_event(
            actor="planner",
            confidence=0.9,
            expected_outcome="prospect will be booked",
            prospect_id="p1",
        ),
        _decision_event(
            actor="planner",
            confidence=0.85,
            expected_outcome="meeting booked this week",
            prospect_id="p2",
        ),
        _decision_event(
            actor="planner", confidence=0.95, expected_outcome="booked", prospect_id="p3"
        ),
    ]
    outcomes = [
        _outcome_event(event_type="meeting.booked", prospect_id="p1"),
        _outcome_event(event_type="meeting.booked", prospect_id="p2"),
        # p3 -- no outcome at all
    ]
    _install_stream(monkeypatch, decisions, outcomes)

    out = cd.reconcile_decisions(window_days=7, until_iso="2026-07-08T00:00:00Z")

    planner = out["per_actor"]["planner"]
    assert planner["decisions"] == 3
    top = [b for b in planner["calibration"] if b["bucket"] == "0.8-1.0"][0]
    assert top["count"] == 3
    assert top["hits"] == 2
    assert top["hit_rate"] == pytest.approx(2 / 3, abs=1e-4)


def test_reconcile_per_actor_breakdown(monkeypatch):
    decisions = [
        _decision_event(
            actor="planner", confidence=0.9, expected_outcome="booked", prospect_id="p1"
        ),
        _decision_event(
            actor="arbiter", confidence=0.5, expected_outcome="paid", opportunity_id="o1"
        ),
    ]
    outcomes = [
        _outcome_event(event_type="meeting.booked", prospect_id="p1"),
        _outcome_event(event_type="payment.received", opportunity_id="o1"),
    ]
    _install_stream(monkeypatch, decisions, outcomes)

    out = cd.reconcile_decisions(window_days=7, until_iso="2026-07-08T00:00:00Z")
    assert set(out["per_actor"].keys()) == {"planner", "arbiter"}
    assert out["per_actor"]["planner"]["decisions"] == 1
    assert out["per_actor"]["arbiter"]["decisions"] == 1


def test_loop_completion_rate_counts_any_outcome(monkeypatch):
    # Two decisions on p1 (any outcome type counts as loop-completed) and one
    # decision on p_dark with NO downstream outcomes at all -> 2/3 completion.
    decisions = [
        _decision_event(
            actor="planner",
            confidence=0.7,
            expected_outcome="booked",
            prospect_id="p1",
            decision_id="d1",
        ),
        _decision_event(
            actor="planner",
            confidence=0.7,
            expected_outcome="reply",
            prospect_id="p1",
            decision_id="d2",
        ),
        _decision_event(
            actor="planner",
            confidence=0.7,
            expected_outcome="booked",
            prospect_id="p_dark",
            decision_id="d3",
        ),
    ]
    outcomes = [
        # Not a target-match for "reply" (payment) -- but still proves the loop
        # reached reality, so it counts for G2 loop-completion.
        _outcome_event(event_type="payment.received", prospect_id="p1"),
    ]
    _install_stream(monkeypatch, decisions, outcomes)

    out = cd.reconcile_decisions(window_days=7, until_iso="2026-07-08T00:00:00Z")
    assert out["overall"]["decisions"] == 3
    assert out["overall"]["loop_completion_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert out["per_actor"]["planner"]["loop_completion_rate"] == pytest.approx(2 / 3, abs=1e-4)


def test_unscoreable_fraction_reported(monkeypatch):
    # One decision with a keyword the intent map covers, one with junk text
    # that matches NOTHING -> unscoreable should be 1.
    decisions = [
        _decision_event(
            actor="planner", confidence=0.9, expected_outcome="prospect booked", prospect_id="p1"
        ),
        _decision_event(actor="planner", confidence=0.9, expected_outcome="???", prospect_id="p2"),
    ]
    outcomes = [
        _outcome_event(event_type="meeting.booked", prospect_id="p1"),
    ]
    _install_stream(monkeypatch, decisions, outcomes)

    out = cd.reconcile_decisions(window_days=7, until_iso="2026-07-08T00:00:00Z")
    assert out["overall"]["decisions"] == 2
    assert out["overall"]["unscoreable"] == 1
    # The unscoreable decision still contributes to bucket count (volume) but
    # not to hits.
    top = [b for b in out["per_actor"]["planner"]["calibration"] if b["bucket"] == "0.8-1.0"][0]
    assert top["count"] == 2
    assert top["hits"] == 1


def test_bucket_boundaries():
    # Direct-check the private mapper -- pins the [lo, hi) convention.
    assert cd._bucket_for(0.0) == "0.0-0.2"
    assert cd._bucket_for(0.19) == "0.0-0.2"
    assert cd._bucket_for(0.2) == "0.2-0.4"
    assert cd._bucket_for(0.6) == "0.6-0.8"
    assert cd._bucket_for(0.8) == "0.8-1.0"
    assert cd._bucket_for(1.0) == "0.8-1.0"  # top bucket inclusive
    assert cd._bucket_for(-1.0) == "0.0-0.2"  # clamps low
    assert cd._bucket_for(2.0) == "0.8-1.0"  # clamps high


def test_return_shape_is_stable(monkeypatch):
    _install_stream(monkeypatch, [], [])
    out = cd.reconcile_decisions()
    assert set(out.keys()) == {"window", "overall", "per_actor"}
    assert set(out["window"].keys()) == {"start", "end", "days"}
    assert set(out["overall"].keys()) == {
        "decisions",
        "unscoreable",
        "loop_completion_rate",
    }


# --- write_calibration_report ----------------------------------------------


def test_write_calibration_report_lands_json(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    from backend.common import storage

    monkeypatch.setattr(storage, "_ROOT", None, raising=False)
    _install_stream(monkeypatch, [], [])

    path = cd.write_calibration_report("2026-07-07")
    assert path.is_file()
    assert path.name == "calibration_report_2026-07-07.json"
    assert path.parent == tmp_path / "cognition"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["day"] == "2026-07-07"
    assert "written_at" in doc
    assert doc["overall"]["decisions"] == 0


def test_write_calibration_report_never_raises_on_empty_day(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    from backend.common import storage

    monkeypatch.setattr(storage, "_ROOT", None, raising=False)
    _install_stream(monkeypatch, [], [])
    # Empty/whitespace day -> today's ISO date is filled in silently.
    path = cd.write_calibration_report("")
    assert path.is_file()
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["day"]  # non-empty


def test_write_calibration_report_swallows_storage_fault(tmp_path, monkeypatch):
    _install_stream(monkeypatch, [], [])
    # Force storage.root() to explode -- the diagnostic must not raise.
    from backend.common import storage

    def _boom():
        raise RuntimeError("no storage today")

    monkeypatch.setattr(storage, "root", _boom)
    path = cd.write_calibration_report("2026-07-07")
    # Returned path is the fallback location; nothing was written but nothing
    # raised either.
    assert path.name == "calibration_report_2026-07-07.json"
