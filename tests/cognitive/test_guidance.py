"""Guidance ingestion + effectiveness pipeline tests.

Covers:
  * Models — GuidanceRecord round-trip, category coercion, level coercion.
  * Parsing — structured list, {recommendations:[…]} envelope, JSON string,
    markdown-fenced JSON, and free-form prose fallback.
  * Classification — tier derivation from (impact, risk), acceptance hint,
    default owner routing.
  * Ingestion — ingest_guidance creates + persists records (status PROPOSED).
  * Ledger — latest-wins reduction, status transitions, record_outcome
    effectiveness write, effectiveness_summary aggregate.
  * Fail-safe — empty/malformed payloads never raise.

Isolation: the JSONL ledger is redirected under tmp_path via SAMUS_STATE_ROOT.
No network, no LLM, no Firestore.
"""
from __future__ import annotations

import json

import pytest

from backend.cognitive.guidance import (
    GuidanceLedger,
    _coerce_items,
    _derive_tier,
    _acceptance_hint,
    ingest_guidance,
)
from backend.cognitive.guidance_models import (
    GuidanceCategory,
    GuidanceRecord,
    GuidanceStatus,
    GuidanceTier,
    coerce_level,
)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """A GuidanceLedger whose JSONL file lands under tmp_path."""
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "jsonl")
    return GuidanceLedger()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
def test_record_roundtrip():
    rec = GuidanceRecord(
        recommendation_id="abc",
        briefing_id="b1",
        ts="2026-06-29T00:00:00Z",
        updated_ts="2026-06-29T00:00:00Z",
        recommendation="Send 50 cold emails",
        category=GuidanceCategory.REVENUE.value,
        tier=1,
        feasibility="high",
        expected_impact="high",
        risk_level="low",
        action_plan=["pull list", "send batch"],
        owner="outreach",
    )
    row = rec.to_dict()
    back = GuidanceRecord.from_dict(row)
    assert back.recommendation_id == "abc"
    assert back.action_plan == ["pull list", "send batch"]
    assert back.category == GuidanceCategory.REVENUE.value
    assert back.tier == 1


def test_category_coerce_variants():
    assert GuidanceCategory.coerce("daily_goal") is GuidanceCategory.DAILY_GOAL
    assert GuidanceCategory.coerce("Revenue Acceleration") is GuidanceCategory.REVENUE
    assert GuidanceCategory.coerce("operational optimization") is GuidanceCategory.OPERATIONAL
    assert GuidanceCategory.coerce("risk") is GuidanceCategory.RISK
    assert GuidanceCategory.coerce("") is GuidanceCategory.OTHER
    assert GuidanceCategory.coerce("something weird") is GuidanceCategory.OTHER


def test_level_coerce():
    assert coerce_level("HIGH") == "high"
    assert coerce_level("moderate") == "medium"
    assert coerce_level("critical") == "high"
    assert coerce_level("minimal") == "low"
    assert coerce_level(None) == "medium"
    assert coerce_level("garbage", default="low") == "low"


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "impact,risk,expected_tier",
    [
        ("high", "low", GuidanceTier.CRITICAL.value),
        ("high", "high", GuidanceTier.CRITICAL.value),
        ("medium", "low", GuidanceTier.HIGH_VALUE.value),
        ("medium", "high", GuidanceTier.CRITICAL.value),   # high risk escalates
        ("low", "low", GuidanceTier.INFORMATIONAL.value),
        ("low", "high", GuidanceTier.HIGH_VALUE.value),    # high risk escalates
    ],
)
def test_tier_derivation(impact, risk, expected_tier):
    assert _derive_tier(impact, risk) == expected_tier


def test_acceptance_hint():
    assert _acceptance_hint("high", "low") == "accept"
    assert _acceptance_hint("low", "low") == "reject"      # infeasible
    assert _acceptance_hint("medium", "high") == "review"  # risky, not surely feasible
    assert _acceptance_hint("high", "high") == "accept"    # high feasibility overrides


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def test_parse_structured_list():
    payload = [{"recommendation": "do x"}, {"recommendation": "do y"}]
    items = _coerce_items(payload)
    assert len(items) == 2


def test_parse_recommendations_envelope():
    payload = {"recommendations": [{"recommendation": "a"}, {"recommendation": "b"}]}
    assert len(_coerce_items(payload)) == 2


def test_parse_json_string():
    payload = json.dumps({"recommendations": [{"recommendation": "a"}]})
    assert len(_coerce_items(payload)) == 1


def test_parse_markdown_fenced_json():
    payload = "```json\n" + json.dumps({"recommendations": [{"recommendation": "a"}]}) + "\n```"
    assert len(_coerce_items(payload)) == 1


def test_parse_prose_numbered_fallback():
    prose = (
        "1. DAILY GOAL: Send 50 cold emails to top prospects.\n"
        "2. WEEKLY GOAL: Close 3 contracts.\n"
        "3. Reduce utility spend."
    )
    items = _coerce_items(prose)
    assert len(items) == 3
    # the ALL-CAPS label is lifted as a category hint
    assert items[0]["category"] == "DAILY GOAL"
    assert "Send 50 cold emails" in items[0]["recommendation"]


def test_parse_prose_single_blob():
    items = _coerce_items("Just one unstructured suggestion with no markers.")
    assert len(items) == 1
    assert "unstructured suggestion" in items[0]["recommendation"]


def test_parse_empty():
    assert _coerce_items(None) == []
    assert _coerce_items("") == []
    assert _coerce_items([]) == []


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------
def test_ingest_structured(ledger):
    payload = {
        "recommendations": [
            {
                "category": "revenue_acceleration",
                "recommendation": "Send SEO-audit cold emails to top 50 prospects",
                "rationale": "fastest path to first dollar",
                "action_steps": ["pull list", "generate audits", "send batch"],
                "feasibility": "high",
                "expected_impact": "high",
                "risk_level": "low",
                "suggested_owner": "outreach",
                "source_question": "3",
            },
            {
                "category": "risk_reduction",
                "recommendation": "Use Firestore as graph substitute",
                "feasibility": "medium",
                "expected_impact": "low",
                "risk_level": "low",
            },
        ]
    }
    records = ingest_guidance("b-test", payload, ledger=ledger)
    assert len(records) == 2

    rev = records[0]
    assert rev.category == GuidanceCategory.REVENUE.value
    assert rev.tier == GuidanceTier.CRITICAL.value           # high impact
    assert rev.owner == "outreach"
    assert rev.acceptance_hint == "accept"
    assert rev.action_plan == ["pull list", "generate audits", "send batch"]
    assert rev.status == GuidanceStatus.PROPOSED.value
    assert rev.source_question == "3"

    # persisted + reloadable
    reloaded = ledger.get(rev.recommendation_id)
    assert reloaded is not None
    assert reloaded.recommendation == rev.recommendation


def test_ingest_owner_pipe_delimited_takes_first(ledger):
    payload = {"recommendations": [
        {"category": "revenue_acceleration", "recommendation": "bundle SEO + proposal",
         "suggested_owner": "seo|outreach|strategy",
         "feasibility": "high", "expected_impact": "high", "risk_level": "low"},
    ]}
    [rec] = ingest_guidance("b", payload, ledger=ledger)
    assert rec.owner == "seo"   # first concrete workcell, not the pipe list


def test_ingest_default_owner_routing(ledger):
    payload = {"recommendations": [
        {"category": "autonomy_advancement", "recommendation": "gate autonomy on accuracy",
         "feasibility": "medium", "expected_impact": "medium", "risk_level": "medium"},
    ]}
    [rec] = ingest_guidance("b", payload, ledger=ledger)
    assert rec.owner == "cognition"   # default routing for autonomy


def test_ingest_skips_empty_recommendation(ledger):
    payload = {"recommendations": [{"recommendation": ""}, {"recommendation": "real one"}]}
    records = ingest_guidance("b", payload, ledger=ledger)
    assert len(records) == 1
    assert records[0].recommendation == "real one"


def test_ingest_malformed_never_raises(ledger):
    # Not a dict/list/str → empty, no raise.
    assert ingest_guidance("b", 12345, ledger=ledger) == []
    assert ingest_guidance("b", None, ledger=ledger) == []


# ---------------------------------------------------------------------------
# Ledger — latest-wins + status lifecycle
# ---------------------------------------------------------------------------
def test_latest_wins_after_transition(ledger):
    [rec] = ingest_guidance("b", {"recommendations": [
        {"recommendation": "do the thing", "feasibility": "high",
         "expected_impact": "high", "risk_level": "low"}
    ]}, ledger=ledger)
    rid = rec.recommendation_id

    ledger.accept(rid)
    ledger.start(rid)

    current = ledger.get(rid)
    assert current.status == GuidanceStatus.IN_PROGRESS.value

    # only ONE logical record despite multiple appended rows
    assert len(ledger.all_latest()) == 1


def test_reject_and_open_items(ledger):
    records = ingest_guidance("b", {"recommendations": [
        {"recommendation": "good", "feasibility": "high", "expected_impact": "high", "risk_level": "low"},
        {"recommendation": "bad", "feasibility": "low", "expected_impact": "low", "risk_level": "high"},
    ]}, ledger=ledger)
    ledger.reject(records[1].recommendation_id, reason="infeasible")

    open_items = ledger.open_items()
    assert len(open_items) == 1
    assert open_items[0].recommendation == "good"

    rejected = ledger.get(records[1].recommendation_id)
    assert rejected.status == GuidanceStatus.REJECTED.value
    assert rejected.is_terminal()


def test_record_outcome_effectiveness(ledger):
    [rec] = ingest_guidance("b", {"recommendations": [
        {"recommendation": "send emails", "feasibility": "high",
         "expected_impact": "high", "risk_level": "low"}
    ]}, ledger=ledger)
    rid = rec.recommendation_id
    ledger.accept(rid)
    ledger.start(rid)
    updated = ledger.record_outcome(
        rid,
        outcome="2 replies, 1 booked call",
        impact_actual="high",
        success_score=0.8,
        lessons_learned="personalized subject lines doubled opens",
        implemented_actions=["sent 50 emails"],
    )
    assert updated.status == GuidanceStatus.COMPLETED.value
    assert updated.success_score == 0.8
    assert updated.impact_actual == "high"
    assert updated.lessons_learned.startswith("personalized")
    assert updated.implemented_actions == ["sent 50 emails"]


def test_success_score_clamped(ledger):
    [rec] = ingest_guidance("b", {"recommendations": [
        {"recommendation": "x", "feasibility": "high", "expected_impact": "high", "risk_level": "low"}
    ]}, ledger=ledger)
    out = ledger.record_outcome(rec.recommendation_id, outcome="done", success_score=5.0)
    assert out.success_score == 1.0
    # and a negative clamps to 0
    [rec2] = ingest_guidance("b2", {"recommendations": [
        {"recommendation": "y", "feasibility": "high", "expected_impact": "high", "risk_level": "low"}
    ]}, ledger=ledger)
    out2 = ledger.record_outcome(rec2.recommendation_id, outcome="done", success_score=-3)
    assert out2.success_score == 0.0


def test_transition_unknown_id_returns_none(ledger):
    assert ledger.accept("does-not-exist") is None


def test_effectiveness_summary(ledger):
    records = ingest_guidance("b", {"recommendations": [
        {"recommendation": "a", "feasibility": "high", "expected_impact": "high", "risk_level": "low"},
        {"recommendation": "b", "feasibility": "high", "expected_impact": "medium", "risk_level": "low"},
        {"recommendation": "c", "feasibility": "low", "expected_impact": "low", "risk_level": "low"},
    ]}, ledger=ledger)
    # complete one with a score, reject one
    ledger.record_outcome(records[0].recommendation_id, outcome="won", success_score=0.9)
    ledger.reject(records[2].recommendation_id)

    summary = ledger.effectiveness_summary()
    assert summary["total"] == 3
    assert summary["by_tier"][GuidanceTier.CRITICAL.value] == 1   # 'a' high impact
    assert summary["by_status"][GuidanceStatus.COMPLETED.value] == 1
    assert summary["by_status"][GuidanceStatus.REJECTED.value] == 1
    assert summary["completed_count"] == 1
    assert summary["mean_success_score"] == 0.9
    assert summary["open_count"] == 1   # only 'b' still open


def test_empty_ledger_summary(ledger):
    summary = ledger.effectiveness_summary()
    assert summary["total"] == 0
    assert summary["mean_success_score"] is None
    assert summary["open_count"] == 0


# ---------------------------------------------------------------------------
# Revision-based latest-wins (finding 1) — must NOT depend on scan order
# ---------------------------------------------------------------------------
def test_revision_increments_per_transition(ledger):
    [rec] = ingest_guidance("b", {"recommendations": [
        {"recommendation": "x", "feasibility": "high", "expected_impact": "high", "risk_level": "low"}
    ]}, ledger=ledger)
    assert rec.revision == 0
    ledger.accept(rec.recommendation_id)
    ledger.start(rec.recommendation_id)
    current = ledger.get(rec.recommendation_id)
    assert current.revision == 2
    assert current.status == GuidanceStatus.IN_PROGRESS.value


def test_latest_wins_ignores_scan_order():
    """Even if scan() returns rows out of append order, MAX(revision) wins.

    Simulates the Firestore failure mode where order_by(time.time()) returns an
    earlier-revision row last. The reducer must still pick the highest revision.
    """
    from backend.cognitive.guidance import GuidanceLedger
    from backend.cognitive.guidance_models import GuidanceRecord

    rid = "fixed-id"
    base = dict(
        recommendation_id=rid, briefing_id="b", ts="2026-06-29T00:00:00Z",
        recommendation="x",
    )
    proposed = GuidanceRecord.from_dict({**base, "updated_ts": "2026-06-29T00:00:00Z",
                                         "status": "proposed", "revision": 0}).to_dict()
    completed = GuidanceRecord.from_dict({**base, "updated_ts": "2026-06-29T00:00:01Z",
                                          "status": "completed", "revision": 3}).to_dict()

    class _OutOfOrderLedger:
        # scan() returns the COMPLETED (rev 3) FIRST, PROPOSED (rev 0) LAST —
        # the worst case for a last-seen-wins reducer.
        def scan(self):
            return [completed, proposed]
        def append(self, row):  # unused here
            pass

    led = GuidanceLedger(ledger=_OutOfOrderLedger())
    [current] = led.all_latest()
    assert current.status == "completed"   # rev 3 wins despite being scanned first
    assert current.revision == 3


def test_from_dict_poison_row_skipped():
    """A malformed row must not crash all_latest() — it's skipped + logged."""
    from backend.cognitive.guidance import GuidanceLedger

    good = {"kind": "guidance_record", "recommendation_id": "g1", "ts": "t",
            "updated_ts": "t", "recommendation": "ok", "revision": 0}
    # tier as a non-numeric string is now tolerated by _safe_int, so craft a row
    # that genuinely breaks from_dict: recommendation_id present but a nested
    # un-stringifiable structure is fine — instead force a None row.
    poison = None

    class _Led:
        def scan(self):
            return [good, poison]
        def append(self, row):
            pass

    led = GuidanceLedger(ledger=_Led())
    recs = led.all_latest()
    assert len(recs) == 1
    assert recs[0].recommendation_id == "g1"


def test_from_dict_tolerant_tier_and_score():
    from backend.cognitive.guidance_models import GuidanceRecord
    rec = GuidanceRecord.from_dict({
        "recommendation_id": "x", "briefing_id": "b", "ts": "t", "updated_ts": "t",
        "recommendation": "y", "tier": "high", "success_score": "not-a-number",
    })
    assert rec.tier == 3            # non-numeric tier -> default
    assert rec.success_score is None  # non-numeric score -> None


# ---------------------------------------------------------------------------
# Hardened parsing (findings 6, 7, 10)
# ---------------------------------------------------------------------------
def test_build_record_non_string_recommendation():
    payload = {"recommendations": [
        {"recommendation": ["step a", "step b"], "feasibility": "high",
         "expected_impact": "high", "risk_level": "low"},
    ]}
    items = _coerce_items(payload)
    from backend.cognitive.guidance import _build_record
    rec = _build_record("b", items[0])
    # list joined, NOT str()'d into "['step a', 'step b']"
    assert rec.recommendation == "step a; step b"


def test_envelope_mixed_dict_and_string():
    payload = {"recommendations": ["do x", {"recommendation": "do y"}]}
    items = _coerce_items(payload)
    assert len(items) == 2   # the bare string "do x" is NOT dropped


def test_envelope_non_list_value_recurses():
    payload = {"recommendations": json.dumps({"recommendation": "single"})}
    items = _coerce_items(payload)
    assert len(items) == 1
    assert items[0]["recommendation"] == "single"


def test_strip_fence_hyphen_lang_and_trailing_ws():
    payload = "```json-5\n" + json.dumps({"recommendations": [{"recommendation": "a"}]}) + "\n```  "
    assert len(_coerce_items(payload)) == 1


def test_owner_leading_delimiter_takes_first_nonempty(ledger):
    payload = {"recommendations": [
        {"category": "revenue_acceleration", "recommendation": "x", "suggested_owner": "|outreach|seo",
         "feasibility": "high", "expected_impact": "high", "risk_level": "low"},
    ]}
    [rec] = ingest_guidance("b", payload, ledger=ledger)
    assert rec.owner == "outreach"   # not "" -> not category default


# ---------------------------------------------------------------------------
# Lifecycle coverage gaps (finding 8)
# ---------------------------------------------------------------------------
def test_abandon(ledger):
    [rec] = ingest_guidance("b", {"recommendations": [
        {"recommendation": "x", "feasibility": "high", "expected_impact": "high", "risk_level": "low"}
    ]}, ledger=ledger)
    ledger.accept(rec.recommendation_id)
    ledger.start(rec.recommendation_id)
    updated = ledger.abandon(rec.recommendation_id, reason="deprioritized")
    assert updated.status == GuidanceStatus.ABANDONED.value
    assert updated.is_terminal()
    assert "deprioritized" in updated.outcome


def test_record_outcome_unknown_id(ledger):
    assert ledger.record_outcome("nope", outcome="x") is None


def test_record_outcome_none_score(ledger):
    [rec] = ingest_guidance("b", {"recommendations": [
        {"recommendation": "x", "feasibility": "high", "expected_impact": "high", "risk_level": "low"}
    ]}, ledger=ledger)
    out = ledger.record_outcome(rec.recommendation_id, outcome="done", success_score=None)
    assert out.status == GuidanceStatus.COMPLETED.value
    assert out.success_score is None


# ---------------------------------------------------------------------------
# Compaction (finding 2)
# ---------------------------------------------------------------------------
def test_compact_drops_superseded_rows(ledger, tmp_path):
    [rec] = ingest_guidance("b", {"recommendations": [
        {"recommendation": "x", "feasibility": "high", "expected_impact": "high", "risk_level": "low"}
    ]}, ledger=ledger)
    rid = rec.recommendation_id
    ledger.accept(rid)
    ledger.start(rid)
    ledger.record_outcome(rid, outcome="won", success_score=0.9)
    # 4 rows on disk (proposed + accepted + in_progress + completed)
    dropped = ledger.compact()
    assert dropped == 3   # only the latest (completed) survives
    # state preserved after compaction
    current = ledger.get(rid)
    assert current.status == GuidanceStatus.COMPLETED.value
    assert current.success_score == 0.9
    assert len(ledger.all_latest()) == 1
