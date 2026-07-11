"""Strategic intelligence cycle — autonomous reports + the feedback seam.

Covers:
  * gather_production_state / compose_* produce non-empty reports.
  * run_pre_shift_briefing: stub LLM ingests guidance; raising LLM is fail-safe.
  * run_end_of_day_review: local-only (no OpenAI) and with-OpenAI paths.
  * active_guidance_context: the cognitive-loop feedback seam — empty, accepted,
    tier ordering, and fail-safe on ledger error.

All offline: the LLM is always injected as a stub; the ledger is redirected to
tmp via SAMUS_STATE_ROOT.
"""
from __future__ import annotations

import json

import pytest

from backend.cognitive.guidance import GuidanceLedger, ingest_guidance
from backend.cognitive import intelligence_cycle as ic


_STUB_GUIDANCE = json.dumps({
    "recommendations": [
        {"category": "revenue_acceleration", "recommendation": "Send SEO-audit cold emails",
         "action_steps": ["pull list", "send"], "feasibility": "high",
         "expected_impact": "high", "risk_level": "low", "suggested_owner": "outreach"},
        {"category": "risk_reduction", "recommendation": "Firestore graph substitute",
         "feasibility": "medium", "expected_impact": "low", "risk_level": "low"},
    ]
})


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "jsonl")
    return GuidanceLedger()


def test_gather_production_state_keys():
    state = ic.gather_production_state()
    assert "date" in state
    assert "revenue" in state
    assert state["revenue"]["target_usd"] == 40000


def test_gather_production_state_date_is_pacific_business_day(monkeypatch):
    """Regression: state['date'] must match Pacific business_today, not UTC date.

    Previously used ``date.today()`` (UTC-naive) which reads a day AHEAD all
    evening PT. That mis-stamped the briefing_id + primer path for "tomorrow"
    while the attestation (which correctly uses business_today) stamped
    "today" — downstream file lookups then missed. See
    project_samus_dpapi_store_thin_vs_fleet_2026_07_06.md tail.
    """
    from datetime import date as _date

    from backend.common import us_timezones

    # Pin business_today to a fixed date and prove state["date"] follows it,
    # not the local process date.
    fixed = _date(2026, 5, 15)
    monkeypatch.setattr(us_timezones, "business_today", lambda state="CA": fixed)
    state = ic.gather_production_state()
    assert state["date"] == "2026-05-15", (
        f"state['date']={state['date']!r} did not track business_today"
    )


def test_business_today_helper_falls_back_on_lookup_failure(monkeypatch):
    """Fail-safe: a broken timezone lookup must yield the UTC date rather than
    crashing the briefing (the whole cycle is designed to degrade)."""
    def _boom(state="CA"):
        raise RuntimeError("timezone lookup down")

    from backend.common import us_timezones
    monkeypatch.setattr(us_timezones, "business_today", _boom)
    iso = ic._business_today_iso()
    # Should be a valid ISO date string, not raise.
    from datetime import date as _date
    _date.fromisoformat(iso)  # raises ValueError if malformed


def test_compose_reports_nonempty():
    state = ic.gather_production_state()
    assert "PRE-SHIFT" in ic.compose_pre_shift_briefing(state)
    assert "OPERATIONAL REVIEW" in ic.compose_end_of_day_review(state, {"total": 0})


def test_run_briefing_ingests(ledger):
    result = ic.run_pre_shift_briefing(llm=lambda s, p: _STUB_GUIDANCE, ledger=ledger)
    assert result["ingested"] == 2
    assert result["briefing_id"].startswith("briefing-")
    assert result["summary"]["total"] == 2
    assert "error" not in result


def test_run_briefing_llm_failure_is_failsafe(ledger):
    def _boom(system, prompt):
        raise RuntimeError("openai down")

    result = ic.run_pre_shift_briefing(llm=_boom, ledger=ledger)
    assert result["ingested"] == 0
    assert "error" in result
    assert "openai down" in result["error"]
    # ledger untouched, summary still returned
    assert result["summary"]["total"] == 0


def test_run_review_local_only(ledger):
    # seed a completed recommendation so effectiveness has signal
    [rec] = ingest_guidance("b", {"recommendations": [
        {"recommendation": "x", "feasibility": "high", "expected_impact": "high", "risk_level": "low"}
    ]}, ledger=ledger)
    ledger.record_outcome(rec.recommendation_id, outcome="won", success_score=0.9)

    result = ic.run_end_of_day_review(ledger=ledger, consult_openai=False)
    assert result["ingested"] == 0
    assert result["effectiveness"]["completed_count"] == 1
    assert result["effectiveness"]["mean_success_score"] == 0.9
    assert "OPERATIONAL REVIEW" in result["review"]


def test_run_review_with_openai_ingests_tomorrow(ledger):
    result = ic.run_end_of_day_review(
        llm=lambda s, p: _STUB_GUIDANCE, ledger=ledger, consult_openai=True
    )
    assert result["ingested"] == 2
    assert result["review_id"].startswith("review-")


# ---------------------------------------------------------------------------
# The feedback seam
# ---------------------------------------------------------------------------
def test_active_guidance_context_empty(ledger):
    assert ic.active_guidance_context(ledger=ledger) == ""


def test_active_guidance_context_accepted_only(ledger):
    records = ingest_guidance("b", {"recommendations": [
        {"category": "revenue_acceleration", "recommendation": "ACCEPTED ONE",
         "feasibility": "high", "expected_impact": "high", "risk_level": "low"},
        {"category": "risk_reduction", "recommendation": "STILL PROPOSED",
         "feasibility": "medium", "expected_impact": "low", "risk_level": "low"},
    ]}, ledger=ledger)
    ledger.accept(records[0].recommendation_id)

    ctx = ic.active_guidance_context(ledger=ledger)
    assert "ACCEPTED ONE" in ctx
    assert "STILL PROPOSED" not in ctx   # proposed-but-not-accepted excluded
    assert "Active Strategic Guidance" in ctx


def test_active_guidance_context_tier_ordering(ledger):
    records = ingest_guidance("b", {"recommendations": [
        {"category": "risk_reduction", "recommendation": "LOW TIER",
         "feasibility": "high", "expected_impact": "low", "risk_level": "low"},   # tier 3
        {"category": "revenue_acceleration", "recommendation": "HIGH TIER",
         "feasibility": "high", "expected_impact": "high", "risk_level": "low"},  # tier 1
    ]}, ledger=ledger)
    for r in records:
        ledger.accept(r.recommendation_id)

    ctx = ic.active_guidance_context(ledger=ledger)
    # tier 1 must appear before tier 3
    assert ctx.index("HIGH TIER") < ctx.index("LOW TIER")


def test_active_guidance_context_failsafe():
    class _Boom:
        def all_latest(self):
            raise RuntimeError("ledger down")

    # injected ledger that raises -> "" (never crashes a cognitive tick)
    assert ic.active_guidance_context(ledger=_Boom()) == ""


# ---------------------------------------------------------------------------
# Concept 1 — precedent seam (active_precedent_context + consult_precedent)
# ---------------------------------------------------------------------------


@pytest.fixture
def belief_state(tmp_path, monkeypatch):
    """Isolate belief_ledger state for precedent tests."""
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "jsonl")
    from backend.cognitive import belief_ledger as bl
    return bl


def _ev(source, weight=1.0):
    return {"source": source, "detail": "d", "weight": weight, "ts": ""}


def test_active_precedent_context_empty_context_returns_empty(belief_state):
    assert ic.active_precedent_context("") == ""
    assert ic.active_precedent_context("   ") == ""


def test_active_precedent_context_no_precedent_returns_empty(belief_state):
    # No beliefs recorded and a query token unlikely to hit the codex corpus.
    out = ic.active_precedent_context("zzzzz-not-in-corpus-qqqqq")
    assert out == ""


def test_active_precedent_context_includes_belief_precedent(belief_state):
    belief_state.record_belief(
        "cold outreach converts best on Tuesday",
        belief_id="cotue",
        supporting=[_ev("a"), _ev("b"), _ev("c")],
    )
    out = ic.active_precedent_context("cold outreach timing")
    assert out
    assert "Prior beliefs" in out
    assert "Tuesday" in out
    assert "%" in out  # confidence prefix formatting


def test_active_precedent_context_composes_beliefs_and_decisions(belief_state):
    """When both sources hit, both sections appear."""
    belief_state.record_belief(
        "outreach converts best on Tuesday",
        belief_id="cotue",
        supporting=[_ev("a"), _ev("b")],
    )
    # "outreach" also hits the codex corpus (real ADRs present).
    out = ic.active_precedent_context("outreach cadence")
    assert "Prior beliefs" in out
    # Decisions may or may not hit depending on token overlap; if they do,
    # the section header appears.
    if "Prior decisions" in out:
        assert "adr" in out.lower() or "resolved" in out.lower()


def test_consult_precedent_proceed_novel_on_empty(belief_state):
    r = ic.consult_precedent("")
    assert r["mode"] == "proceed_novel"
    assert r["leading_belief"] is None
    assert r["beliefs"] == []


def test_consult_precedent_proceed_novel_when_no_precedent(belief_state):
    r = ic.consult_precedent("zzzz-nothing-here-qqqq")
    assert r["mode"] == "proceed_novel"


def test_consult_precedent_short_circuits_on_strong_belief(belief_state):
    """A high-confidence, keyword-matched active belief triggers short-circuit."""
    # Enough support to push confidence above 0.7, plus situation_key for the
    # +8.0 boost that exceeds the 4.0 score threshold.
    belief_state.record_belief(
        "cold outreach converts best on Tuesday",
        belief_id="cotue",
        supporting=[_ev("a"), _ev("b"), _ev("c"), _ev("d"), _ev("e"), _ev("f")],
        situation_key=belief_state.situation_key_for("cold outreach first touch"),
    )
    r = ic.consult_precedent("cold outreach first touch")
    assert r["mode"] == "short_circuit"
    assert r["leading_belief"] is not None
    assert r["leading_belief"].belief_id == "cotue"
    assert "cotue" in r["rationale"]


def test_consult_precedent_does_not_short_circuit_on_contradicted(belief_state):
    """A CONTRADICTED belief must not short-circuit even at high keyword match."""
    belief_state.record_belief(
        "cold outreach converts best on Tuesday",
        belief_id="flip",
        supporting=[_ev("a")],
        counter=[_ev("x"), _ev("y"), _ev("z")],  # flips to contradicted
        situation_key=belief_state.situation_key_for("cold outreach first touch"),
    )
    assert belief_state.get_belief("flip").status == belief_state.STATUS_CONTRADICTED
    r = ic.consult_precedent("cold outreach first touch")
    assert r["mode"] == "proceed_novel"
    assert r["leading_belief"] is None


def test_consult_precedent_proceed_novel_on_weak_belief(belief_state):
    """A single-support keyword-only belief has confidence 0.667, below 0.7."""
    belief_state.record_belief(
        "cold outreach might work sometime",
        belief_id="weak",
        supporting=[_ev("a")],  # confidence = 2/3 = 0.667, below 0.7 threshold
    )
    r = ic.consult_precedent("cold outreach timing")
    # Belief surfaces but does not clear short-circuit thresholds.
    assert r["mode"] == "proceed_novel"


# ---------------------------------------------------------------------------
# Wiring — compose_pre_shift_briefing appends precedent block when it matches
# ---------------------------------------------------------------------------


def test_compose_pre_shift_briefing_appends_precedent_block(belief_state):
    """When a belief precedent matches today's runway/phase/burn shape, the
    briefing gains a "Precedent" block after the cost breakdown. Absent a
    matching belief the briefing is unchanged.
    """
    state = {
        "date": "2026-07-06",
        "timestamp_utc": "2026-07-06T00:00:00Z",
        "codb": {"total_monthly_burn": 500.0, "by_category": []},
        "runway": {"days_remaining": 15, "alert_triggered": True},
        "revenue": {"mrr_usd": 0, "phase": "pre-revenue",
                    "target_usd": 50000, "days_to_target": 90},
    }
    baseline = ic.compose_pre_shift_briefing(state)
    # Baseline is expected to have NO Prior beliefs section (no seeded belief),
    # though ADR/codex precedent may already surface on shared vocabulary.
    assert "Prior beliefs on this situation" not in baseline

    belief_state.record_belief(
        "at 15d runway and pre-revenue phase, focus on collection",
        belief_id="preshift_precedent_A",
        supporting=[_ev("s1"), _ev("s2"), _ev("s3")],
    )
    briefed = ic.compose_pre_shift_briefing(state)
    assert "Precedent (recall before synthesizing anew)" in briefed
    assert "Prior beliefs on this situation" in briefed
    assert "focus on collection" in briefed
