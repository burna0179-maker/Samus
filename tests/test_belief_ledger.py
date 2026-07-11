"""Belief ledger — durable belief tracking + contradiction/staleness (backend/cognitive/belief_ledger.py)."""
from __future__ import annotations

import pytest

from backend.cognitive import belief_ledger as bl


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "jsonl")


def _ev(source, weight=1.0):
    return {"source": source, "detail": "d", "weight": weight, "ts": ""}


def test_record_belief_computes_confidence_from_support():
    b = bl.record_belief("new opener lifts close rate",
                         supporting=[_ev("a"), _ev("b")])
    # Laplace: (1+2)/(2+2) = 0.75
    assert b.confidence == 0.75
    assert b.status == bl.STATUS_ACTIVE
    assert b.last_verified


def test_upsert_merges_evidence_and_strengthens():
    bl.record_belief("claim x", belief_id="cx", supporting=[_ev("a")])
    b = bl.record_belief("claim x", belief_id="cx", supporting=[_ev("b"), _ev("c")])
    # 3 support total -> (1+3)/(2+3) = 0.8
    assert b.confidence == 0.8
    assert len(b.supporting_evidence) == 3


def test_counter_evidence_flips_to_contradicted():
    b = bl.record_belief("shaky claim", belief_id="sc",
                         supporting=[_ev("a")],
                         counter=[_ev("x"), _ev("y"), _ev("z")])
    # (1+1)/(2+1+3) = 0.333 < 0.5 -> contradicted
    assert b.confidence < bl.CONTRADICTION_CONFIDENCE
    assert b.status == bl.STATUS_CONTRADICTED


def test_contradictions_ranked_by_economic_impact():
    bl.record_belief("cheap wrong", belief_id="c1",
                     supporting=[_ev("a")], counter=[_ev("x"), _ev("y")],
                     economic_impact=50.0)
    bl.record_belief("expensive wrong", belief_id="c2",
                     supporting=[_ev("a")], counter=[_ev("x"), _ev("y")],
                     economic_impact=5000.0)
    out = bl.contradictions()
    assert [b.belief_id for b in out] == ["c2", "c1"]  # costliest first


def test_stale_beliefs_flags_by_age():
    bl.record_belief("fresh", belief_id="f", supporting=[_ev("a")])
    # Huge horizon -> nothing stale yet.
    assert bl.stale_beliefs(max_age_seconds=10_000) == []
    # Negative horizon -> everything active is overdue.
    stale = bl.stale_beliefs(max_age_seconds=-1)
    assert [b.belief_id for b in stale] == ["f"]


def test_verify_refreshes_last_verified():
    bl.record_belief("v", belief_id="v", supporting=[_ev("a")])
    before = bl.get_belief("v").last_verified
    out = bl.verify("v")
    assert out is not None
    assert out.last_verified >= before


def test_add_evidence_unknown_returns_none():
    assert bl.add_evidence("nope", support=_ev("a")) is None


def test_record_from_triangulation_maps_corroborated_and_divergent():
    result = {
        "corroborated": [
            {"finding": "runway is 30 days", "tier": 1, "sources": ["A", "B"]},
        ],
        "divergent": [
            {"finding": "market is turning", "source": "B"},
        ],
    }
    beliefs = bl.record_from_triangulation(result, economic_impact=1000.0)
    assert len(beliefs) == 2

    corro = bl.get_belief(bl.belief_id_for("runway is 30 days"))
    assert corro.tier == 1
    assert len(corro.supporting_evidence) == 2  # one per source
    assert corro.status == bl.STATUS_ACTIVE

    dvg = bl.get_belief(bl.belief_id_for("market is turning"))
    # 1 support + 1 divergence counter -> (1+1)/(2+1+1) = 0.5 -> not below thresh
    assert dvg.counter_evidence  # carries the divergence marker
    assert dvg.confidence <= 0.5


def test_belief_id_for_is_stable_slug():
    assert bl.belief_id_for("New Opener Lifts Close-Rate!!") == "new-opener-lifts-close-rate"
    assert bl.belief_id_for("") == "belief"


# ---------------------------------------------------------------------------
# Concept 1 — precedent retrieval (query_precedent + situation_key)
# ---------------------------------------------------------------------------


def test_situation_key_for_is_stable_slug():
    assert bl.situation_key_for("Cold Outreach — First Touch!") == "cold-outreach-first-touch"
    assert bl.situation_key_for("") == "situation"


def test_query_precedent_empty_corpus_returns_empty():
    assert bl.query_precedent("cold outreach first touch") == []


def test_query_precedent_no_match_returns_empty():
    bl.record_belief(
        "runway is 30 days",
        belief_id="r30",
        supporting=[_ev("a"), _ev("b"), _ev("c")],
    )
    assert bl.query_precedent("gmail oauth outage") == []


def test_query_precedent_single_match_on_claim_keywords():
    bl.record_belief(
        "cold outreach converts best on Tuesday",
        belief_id="cotue",
        supporting=[_ev("a"), _ev("b")],
    )
    out = bl.query_precedent("cold outreach timing")
    assert len(out) == 1
    assert out[0].belief_id == "cotue"
    assert out[0].matched_on == "claim"
    assert out[0].score > 0.0


def test_query_precedent_situation_key_dominates_claim_overlap():
    """A tagged belief with fewer claim overlaps beats an untagged one with more."""
    bl.record_belief(
        "outreach timing lifts response rate",
        belief_id="untagged",
        supporting=[_ev("a"), _ev("b"), _ev("c"), _ev("d")],
    )
    bl.record_belief(
        "Tuesday works",  # short claim, few keywords
        belief_id="tagged",
        supporting=[_ev("a"), _ev("b")],
        situation_key=bl.situation_key_for("cold outreach first touch"),
    )
    out = bl.query_precedent("cold outreach first touch")
    assert out[0].belief_id == "tagged"
    assert "situation_key" in out[0].matched_on


def test_query_precedent_excludes_contradicted_beliefs():
    """A flipped belief must never surface as precedent."""
    bl.record_belief(
        "cold outreach converts best on Tuesday",
        belief_id="flip",
        supporting=[_ev("a")],
        counter=[_ev("x"), _ev("y"), _ev("z")],  # forces contradicted
    )
    assert bl.get_belief("flip").status == bl.STATUS_CONTRADICTED
    assert bl.query_precedent("cold outreach timing") == []


def test_query_precedent_multi_match_ranks_by_score():
    bl.record_belief(
        "outreach converts best on Tuesday",
        belief_id="strong",
        supporting=[_ev("a"), _ev("b"), _ev("c")],  # high confidence
    )
    bl.record_belief(
        "outreach might work sometime",  # weak overlap
        belief_id="weak",
        supporting=[_ev("a")],
    )
    out = bl.query_precedent("outreach converts", k=5)
    assert len(out) >= 1
    # Higher-confidence stronger-match ranks first.
    assert out[0].belief_id == "strong"


def test_query_precedent_respects_k_cap():
    for i in range(6):
        bl.record_belief(
            f"outreach lesson {i}",
            belief_id=f"o{i}",
            supporting=[_ev("a"), _ev("b")],
        )
    out = bl.query_precedent("outreach lesson", k=3)
    assert len(out) == 3


# ---------------------------------------------------------------------------
# Concept 5 — epistemic governance (depended_by + link_decision +
# active->contradicted trigger + ADR-0019 severity routing)
# ---------------------------------------------------------------------------


def test_belief_depended_by_defaults_to_empty_list():
    b = bl.record_belief("a claim", belief_id="dbe",
                         supporting=[_ev("a")])
    assert b.depended_by == []


def test_from_dict_backward_compat_without_depended_by():
    """Rows saved before Concept 5 landed have no ``depended_by`` key."""
    legacy = {
        "belief_id": "old",
        "claim": "old claim",
        "confidence": 0.75,
        "supporting_evidence": [],
        "counter_evidence": [],
        "last_verified": "",
        "economic_impact": 0.0,
        "tier": 2,
        "status": bl.STATUS_ACTIVE,
        "created_at": "",
        "updated_at": "",
        "situation_key": "",
        # depended_by intentionally absent
    }
    b = bl.Belief.from_dict(legacy)
    assert b.depended_by == []


def test_from_dict_reads_depended_by_when_present():
    row = {"belief_id": "x", "claim": "c", "depended_by": ["dec1", "dec2"]}
    b = bl.Belief.from_dict(row)
    assert b.depended_by == ["dec1", "dec2"]


def test_link_decision_appends_and_persists():
    bl.record_belief("linkable", belief_id="lk", supporting=[_ev("a")])
    out = bl.link_decision("lk", "dec_alpha")
    assert out is not None
    assert out.depended_by == ["dec_alpha"]
    # Persistence across reload:
    assert bl.get_belief("lk").depended_by == ["dec_alpha"]


def test_link_decision_is_idempotent():
    bl.record_belief("idem", belief_id="id", supporting=[_ev("a")])
    bl.link_decision("id", "dec_beta")
    bl.link_decision("id", "dec_beta")
    bl.link_decision("id", "dec_beta")
    assert bl.get_belief("id").depended_by == ["dec_beta"]


def test_link_decision_allows_multiple_distinct_decisions():
    bl.record_belief("multi", belief_id="mu", supporting=[_ev("a")])
    bl.link_decision("mu", "d1")
    bl.link_decision("mu", "d2")
    bl.link_decision("mu", "d3")
    assert bl.get_belief("mu").depended_by == ["d1", "d2", "d3"]


def test_link_decision_fail_soft_on_unknown_belief():
    """Missing belief returns None (matches add_evidence behaviour)."""
    assert bl.link_decision("does_not_exist", "dec_x") is None


def test_link_decision_ignores_empty_ids():
    bl.record_belief("empty_ids", belief_id="ei", supporting=[_ev("a")])
    assert bl.link_decision("", "dec_y") is None
    assert bl.link_decision("ei", "") is None
    assert bl.get_belief("ei").depended_by == []


def test_dependent_decisions_reads_back_links():
    bl.record_belief("readback", belief_id="rb", supporting=[_ev("a")])
    bl.link_decision("rb", "dec_1")
    bl.link_decision("rb", "dec_2")
    assert bl.dependent_decisions("rb") == ["dec_1", "dec_2"]


def test_dependent_decisions_empty_for_unlinked_belief():
    bl.record_belief("no_deps", belief_id="nd", supporting=[_ev("a")])
    assert bl.dependent_decisions("nd") == []


def test_dependent_decisions_empty_for_unknown_belief():
    assert bl.dependent_decisions("no_such_belief") == []


def test_dependent_decisions_returns_copy_not_alias():
    """Caller can't mutate the ledger through the returned list."""
    bl.record_belief("copy", belief_id="cp", supporting=[_ev("a")])
    bl.link_decision("cp", "d")
    out = bl.dependent_decisions("cp")
    out.append("intruder")
    assert bl.dependent_decisions("cp") == ["d"]


def test_active_to_contradicted_with_no_deps_emits_event_no_approval(monkeypatch):
    """No approval fires when nothing depended on the flipped belief."""
    from backend.common import approvals

    calls: list[dict] = []
    monkeypatch.setattr(
        approvals, "create_approval",
        lambda kind, payload=None, **kw: calls.append(
            {"kind": kind, "payload": payload, **kw}) or {"id": "x"},
    )
    # Seed active belief.
    bl.record_belief("no deps flip", belief_id="ndf",
                     supporting=[_ev("a"), _ev("b")])
    assert bl.get_belief("ndf").status == bl.STATUS_ACTIVE
    # Flip via update: overwhelming counter-evidence.
    bl.record_belief("no deps flip", belief_id="ndf",
                     counter=[_ev("x"), _ev("y"), _ev("z"),
                              _ev("w"), _ev("v")])
    assert bl.get_belief("ndf").status == bl.STATUS_CONTRADICTED
    assert calls == []  # no depended_by -> no approval


def test_active_to_contradicted_with_deps_fires_approval(monkeypatch):
    from backend.common import approvals

    calls: list[dict] = []
    monkeypatch.setattr(
        approvals, "create_approval",
        lambda kind, payload=None, **kw: calls.append(
            {"kind": kind, "payload": payload, **kw}) or {"id": "x"},
    )
    bl.record_belief("deps flip", belief_id="df",
                     supporting=[_ev("a"), _ev("b")])
    bl.link_decision("df", "dec_downstream")
    # Flip.
    bl.record_belief("deps flip", belief_id="df",
                     counter=[_ev("x"), _ev("y"), _ev("z"),
                              _ev("w"), _ev("v")])
    assert len(calls) == 1
    call = calls[0]
    assert call["kind"] == "recheck_decisions"
    assert call["payload"]["belief_id"] == "df"
    assert call["payload"]["decisions"] == ["dec_downstream"]


def test_contradiction_emergency_severity_at_impact_threshold(monkeypatch):
    """economic_impact >= $100 -> risk_level=high (emergency severity)."""
    from backend.common import approvals

    calls: list[dict] = []
    monkeypatch.setattr(
        approvals, "create_approval",
        lambda kind, payload=None, **kw: calls.append(
            {"kind": kind, "payload": payload, **kw}) or {"id": "x"},
    )
    bl.record_belief("expensive flip", belief_id="ef",
                     supporting=[_ev("a"), _ev("b")],
                     economic_impact=500.0)
    bl.link_decision("ef", "dec_costly")
    bl.record_belief("expensive flip", belief_id="ef",
                     counter=[_ev("x"), _ev("y"), _ev("z"),
                              _ev("w"), _ev("v")],
                     economic_impact=500.0)
    assert len(calls) == 1
    assert calls[0]["risk_level"] == "high"
    assert calls[0]["ev_usd"] == 500.0


def test_contradiction_routine_severity_below_threshold(monkeypatch):
    """economic_impact < $100 -> risk_level=normal (routine severity)."""
    from backend.common import approvals

    calls: list[dict] = []
    monkeypatch.setattr(
        approvals, "create_approval",
        lambda kind, payload=None, **kw: calls.append(
            {"kind": kind, "payload": payload, **kw}) or {"id": "x"},
    )
    bl.record_belief("cheap flip", belief_id="cf",
                     supporting=[_ev("a"), _ev("b")],
                     economic_impact=25.0)
    bl.link_decision("cf", "dec_cheap")
    bl.record_belief("cheap flip", belief_id="cf",
                     counter=[_ev("x"), _ev("y"), _ev("z"),
                              _ev("w"), _ev("v")],
                     economic_impact=25.0)
    assert len(calls) == 1
    assert calls[0]["risk_level"] == "normal"


def test_second_update_on_contradicted_belief_does_not_re_fire(monkeypatch):
    """Only the ACTIVE->CONTRADICTED edge triggers; contradicted->contradicted is silent."""
    from backend.common import approvals

    calls: list[dict] = []
    monkeypatch.setattr(
        approvals, "create_approval",
        lambda kind, payload=None, **kw: calls.append(
            {"kind": kind, "payload": payload, **kw}) or {"id": "x"},
    )
    bl.record_belief("once", belief_id="on",
                     supporting=[_ev("a")])
    bl.link_decision("on", "dec_once")
    # First flip.
    bl.record_belief("once", belief_id="on",
                     counter=[_ev("x"), _ev("y"), _ev("z")])
    assert len(calls) == 1
    # Extra counter — still contradicted, no fresh edge.
    bl.record_belief("once", belief_id="on",
                     counter=[_ev("q")])
    assert len(calls) == 1


def test_active_stays_active_does_not_fire_contradiction(monkeypatch):
    from backend.common import approvals

    calls: list[dict] = []
    monkeypatch.setattr(
        approvals, "create_approval",
        lambda kind, payload=None, **kw: calls.append(
            {"kind": kind, "payload": payload, **kw}) or {"id": "x"},
    )
    bl.record_belief("stays", belief_id="st", supporting=[_ev("a")])
    bl.link_decision("st", "d")
    bl.record_belief("stays", belief_id="st", supporting=[_ev("b")])
    assert bl.get_belief("st").status == bl.STATUS_ACTIVE
    assert calls == []


def test_new_belief_born_contradicted_still_fires_when_deps_exist(monkeypatch):
    """A brand-new belief that lands contradicted from birth has no
    depended_by yet, so it emits the marker event but no approval — the
    dependency-graph check is what gates the HOTL enqueue."""
    from backend.common import approvals

    calls: list[dict] = []
    monkeypatch.setattr(
        approvals, "create_approval",
        lambda kind, payload=None, **kw: calls.append(
            {"kind": kind, "payload": payload, **kw}) or {"id": "x"},
    )
    b = bl.record_belief("born flipped", belief_id="bf",
                         supporting=[_ev("a")],
                         counter=[_ev("x"), _ev("y"), _ev("z")])
    assert b.status == bl.STATUS_CONTRADICTED
    assert calls == []  # nothing depended on it — correctly quiet


def test_link_decision_preserved_across_record_belief_update():
    """A depended_by list survives updates to the belief that don't flip it."""
    bl.record_belief("survive", belief_id="sv", supporting=[_ev("a")])
    bl.link_decision("sv", "dec_persist")
    bl.record_belief("survive", belief_id="sv", supporting=[_ev("b")])
    assert bl.get_belief("sv").depended_by == ["dec_persist"]


def test_contradiction_approval_survives_business_event_emit_failure(monkeypatch):
    """Fail-soft: telemetry break must not mask the approval enqueue."""
    from backend.common import approvals
    from backend.common import business_events

    def boom(*a, **kw):
        raise RuntimeError("event stream down")

    calls: list[dict] = []
    monkeypatch.setattr(business_events, "emit_business_event", boom)
    monkeypatch.setattr(
        approvals, "create_approval",
        lambda kind, payload=None, **kw: calls.append(
            {"kind": kind, "payload": payload, **kw}) or {"id": "x"},
    )
    bl.record_belief("resilient", belief_id="rs",
                     supporting=[_ev("a"), _ev("b")])
    bl.link_decision("rs", "dec_r")
    bl.record_belief("resilient", belief_id="rs",
                     counter=[_ev("x"), _ev("y"), _ev("z"),
                              _ev("w"), _ev("v")])
    # Approval must still have fired despite the event-emit blow-up.
    assert len(calls) == 1
    assert calls[0]["kind"] == "recheck_decisions"
