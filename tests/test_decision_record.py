"""Decision records (HOTL Tranche 4) — mint, persist-via-stream, list, drill-down.

DecisionRecords ride the unified business-event stream as decision.made
metadata, so isolation is the same SAMUS_BUSINESS_EVENTS_PATH tmpfile pattern.
"""

from __future__ import annotations

import pytest

from backend.common import business_events, decision_record as dr


@pytest.fixture
def stream(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_BUSINESS_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    monkeypatch.delenv("SAMUS_LEDGER_BACKEND", raising=False)
    return tmp_path


# --- minting (pure) --------------------------------------------------------


def test_make_decision_record_fills_fields():
    rec = dr.make_decision_record(
        "arbiter",
        "picked call over email",
        alternatives_considered=["email (lower EV)"],
        data_used=["ev=$500", "connect_rate=0.2"],
        expected_outcome="book a meeting",
        confidence=0.8,
        risk_level="normal",
        ev_usd=500.0,
        prospect_id="p1",
    )
    assert rec.decision_id
    assert rec.actor == "arbiter"
    assert rec.why == "picked call over email"
    assert rec.alternatives_considered == ["email (lower EV)"]
    assert rec.confidence == 0.8
    assert rec.ev_usd == 500.0
    assert rec.risk_level == "normal"
    assert rec.prospect_id == "p1"
    assert rec.ts  # timestamped


def test_make_decision_record_defaults_are_safe():
    rec = dr.make_decision_record("x", "")
    assert rec.confidence == 0.0
    assert rec.ev_usd == 0.0
    assert rec.risk_level == "normal"
    assert rec.alternatives_considered == []


# --- record + emit round-trip ----------------------------------------------


def test_record_decision_emits_to_stream(stream):
    rec = dr.record_decision(
        "planner",
        "generated plan",
        workcell="planning",
        ev_usd=1000.0,
        confidence=0.6,
        prospect_id="p1",
    )
    # a decision.made event landed
    events = business_events.read_events(event_types=["decision.made"])
    assert len(events) == 1
    ev = events[0]
    assert ev["event_type"] == "decision.made"
    assert ev["workcell"] == "planning"
    assert ev["prospect_id"] == "p1"
    # the full record rides metadata under the record key
    embedded = ev["metadata"][dr.DECISION_RECORD_KEY]
    assert embedded["decision_id"] == rec.decision_id
    assert embedded["why"] == "generated plan"
    # forecast ev is on the record, NOT the event's money fields
    assert ev["revenue_usd"] is None
    assert embedded["ev_usd"] == 1000.0


def test_record_decision_never_raises_on_bad_event(stream, monkeypatch):
    # Even if the emit path blows up, record_decision returns the record.
    def boom(*a, **k):
        raise RuntimeError("emit down")

    monkeypatch.setattr(business_events, "emit_business_event", boom)
    rec = dr.record_decision("planner", "still works")
    assert rec.actor == "planner"
    assert rec.why == "still works"


# --- listing + drill-down --------------------------------------------------


def test_list_decisions_newest_first(stream):
    dr.record_decision("planner", "first")
    dr.record_decision("arbiter", "second")
    dr.record_decision("planner", "third")
    rows = dr.list_decisions(limit=10)
    assert len(rows) == 3
    assert rows[0]["why"] == "third"  # newest first
    assert rows[-1]["why"] == "first"


def test_list_decisions_actor_filter(stream):
    dr.record_decision("planner", "p1")
    dr.record_decision("arbiter", "a1")
    dr.record_decision("planner", "p2")
    planner_rows = dr.list_decisions(actor="planner", limit=10)
    assert len(planner_rows) == 2
    assert all(r["actor"] == "planner" for r in planner_rows)


def test_list_decisions_prospect_filter(stream):
    dr.record_decision("planner", "for p1", prospect_id="p1")
    dr.record_decision("planner", "for p2", prospect_id="p2")
    rows = dr.list_decisions(prospect_id="p1", limit=10)
    assert len(rows) == 1
    assert rows[0]["prospect_id"] == "p1"


def test_get_decision_by_id(stream):
    rec = dr.record_decision(
        "arbiter",
        "picked call",
        alternatives_considered=["email"],
        data_used=["ev=$500"],
        expected_outcome="meeting booked",
    )
    found = dr.get_decision(rec.decision_id)
    assert found is not None
    assert found["decision_id"] == rec.decision_id
    assert found["why"] == "picked call"
    assert found["alternatives_considered"] == ["email"]
    assert found["data_used"] == ["ev=$500"]
    assert found["expected_outcome"] == "meeting booked"


def test_get_decision_unknown_returns_none(stream):
    assert dr.get_decision("nope") is None
    assert dr.get_decision("") is None


def test_synthesises_thin_record_from_bare_decision_event(stream):
    # A decision.made event WITHOUT an embedded record (e.g. an approval
    # lifecycle event) still appears in the decision log, synthesised.
    business_events.emit_business_event(
        "decision.made",
        workcell="governance",
        metadata={"decision": "approval.requested", "approval_id": "abc", "risk_level": "high"},
    )
    rows = dr.list_decisions(limit=10)
    assert len(rows) == 1
    assert rows[0]["actor"] == "governance"
    assert rows[0]["why"] == "approval.requested"
    assert rows[0]["risk_level"] == "high"


def test_list_decisions_empty_stream(stream):
    assert dr.list_decisions(limit=10) == []


# --- validation_performed + memories_retrieved (G3 / G5 enrichment) --------


def test_make_decision_record_accepts_validation_and_memories():
    """G3 + G5: new optional fields round-trip through the dataclass."""
    rec = dr.make_decision_record(
        "planner",
        "picked plan A",
        validation_performed=[
            "schema_validate:proposal_v2",
            "budget_gate:passed",
            "adversarial_verify:2of3",
        ],
        memories_retrieved=[
            {
                "source": "guidance_ledger",
                "id": "G-042",
                "why": "prior outreach on same ICP converted",
            },
            {
                "source": "precedent",
                "id": "opp/17",
                "why": "same objection resolved with SEO audit offer",
            },
        ],
    )
    assert rec.validation_performed == [
        "schema_validate:proposal_v2",
        "budget_gate:passed",
        "adversarial_verify:2of3",
    ]
    assert len(rec.memories_retrieved) == 2
    assert rec.memories_retrieved[0]["source"] == "guidance_ledger"
    assert rec.memories_retrieved[1]["id"] == "opp/17"
    # to_dict carries them
    d = rec.to_dict()
    assert d["validation_performed"] == rec.validation_performed
    assert d["memories_retrieved"] == rec.memories_retrieved


def test_make_decision_record_defaults_new_fields_to_empty_lists():
    rec = dr.make_decision_record("x", "no self-check")
    assert rec.validation_performed == []
    assert rec.memories_retrieved == []
    d = rec.to_dict()
    assert d["validation_performed"] == []
    assert d["memories_retrieved"] == []


def test_record_decision_emits_new_fields_on_stream(stream):
    """G3 + G5: fields survive the emit → read_events → _extract_record round-trip."""
    rec = dr.record_decision(
        "arbiter",
        "picked call over email",
        validation_performed=["budget_gate:passed", "dnc_gate:clean"],
        memories_retrieved=[
            {
                "source": "calibration",
                "id": "cal/dial_hour_of_day",
                "why": "10a PT lifts connect rate 1.6x",
            },
        ],
        prospect_id="p1",
    )
    rows = dr.list_decisions(limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["decision_id"] == rec.decision_id
    assert row["validation_performed"] == ["budget_gate:passed", "dnc_gate:clean"]
    assert row["memories_retrieved"] == [
        {
            "source": "calibration",
            "id": "cal/dial_hour_of_day",
            "why": "10a PT lifts connect rate 1.6x",
        },
    ]


def test_record_decision_without_new_fields_is_backward_compatible(stream):
    """Regression: a caller that omits the new fields must still work end-to-end."""
    rec = dr.record_decision("planner", "legacy call site")
    row = dr.get_decision(rec.decision_id)
    assert row is not None
    # Defaults land as empty lists, not missing keys.
    assert row["validation_performed"] == []
    assert row["memories_retrieved"] == []


def test_extract_record_thin_synthesis_defaults_new_fields():
    """Bare decision.made event (no embedded record) — synthesised record has
    both new fields defaulting to []."""
    event = {
        "event_type": "decision.made",
        "event_id": "evt-1",
        "workcell": "governance",
        "ts": "2026-07-08T12:00:00Z",
        "metadata": {"decision": "approval.requested", "risk_level": "high"},
    }
    rec = dr._extract_record(event)
    assert rec is not None
    assert rec["actor"] == "governance"
    assert rec["validation_performed"] == []
    assert rec["memories_retrieved"] == []


def test_extract_record_older_embedded_record_backfills_new_fields():
    """A record written by an OLDER version (no new fields in the embedded
    dict) must still parse, with the new fields defaulting to []."""
    older_embedded = {
        "decision_id": "dec-legacy-1",
        "actor": "planner",
        "why": "old writer, no new fields",
        "alternatives_considered": [],
        "data_used": [],
        "expected_outcome": "",
        "confidence": 0.0,
        "risk_level": "normal",
        "ev_usd": 0.0,
        "ts": "2026-07-08T11:00:00Z",
        "prospect_id": "",
        "opportunity_id": "",
        "campaign_id": "",
        "extra": {},
        # NOTE: validation_performed + memories_retrieved intentionally absent
    }
    event = {
        "event_type": "decision.made",
        "event_id": "evt-legacy",
        "workcell": "planner",
        "ts": "2026-07-08T11:00:00Z",
        "metadata": {dr.DECISION_RECORD_KEY: older_embedded},
    }
    rec = dr._extract_record(event)
    assert rec is not None
    assert rec["decision_id"] == "dec-legacy-1"
    assert rec["validation_performed"] == []
    assert rec["memories_retrieved"] == []
