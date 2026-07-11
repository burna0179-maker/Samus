"""Scoring v2 — urgency decay, full cost, confidence, priority formula.

All fixtures hand-computed so a formula regression is caught exactly.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from backend.crm.scoring import (
    CONFIDENCE_PRIOR_TRIALS,
    DEFAULT_TIME_ESTIMATE_HRS,
    EMAIL_SEND_COST_USD,
    PRIORITY_COST_FLOOR_USD,
    STAGE_TIME_ESTIMATE_HRS,
    STAGE_URGENCY_HALF_LIFE_DAYS,
    URGENCY_FLOOR,
    PriorityScore,
    bandit_trials_for,
    classify_tier,
    compute_priority,
    confidence_from_trials,
    estimate_voice_cost_usd,
    full_cost_usd,
    priority_score,
    score_opportunity_from_lead,
    urgency_multiplier,
)

_NOW = _dt.datetime(2026, 7, 5, 12, 0, 0, tzinfo=_dt.timezone.utc)


# ---------------------------------------------------------------------------
# v1 functions still work (extension must not regress the existing API)
# ---------------------------------------------------------------------------


def test_v1_surface_unchanged():
    assert classify_tier(95) == "priority"
    tier, prob, size = score_opportunity_from_lead(
        {"intent_score": 75, "monthly_budget": "$500-$2000"},
    )
    assert (tier, prob, size) == ("hot", 0.35, 15000.0)


# ---------------------------------------------------------------------------
# urgency
# ---------------------------------------------------------------------------


def test_urgency_fresh_is_one():
    assert urgency_multiplier(0.0, "new") == 1.0


def test_urgency_half_life_hits_exactly_half():
    # 14 days in stage "new" (half-life 14d) -> 0.5 exactly.
    assert urgency_multiplier(14.0, "new") == pytest.approx(0.5)
    # negotiation half-life is 7d.
    assert urgency_multiplier(7.0, "negotiation") == pytest.approx(0.5)


def test_urgency_override_half_life():
    assert urgency_multiplier(10.0, "new", half_life_days=10.0) == pytest.approx(0.5)


def test_urgency_floor_and_negative_age():
    assert urgency_multiplier(10_000.0, "new") == URGENCY_FLOOR
    assert urgency_multiplier(-3.0, "new") == 1.0


def test_urgency_unknown_stage_uses_default_half_life():
    assert urgency_multiplier(14.0, "no_such_stage") == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# confidence
# ---------------------------------------------------------------------------


def test_confidence_zero_trials_is_zero():
    assert confidence_from_trials(0) == 0.0


def test_confidence_prior_trials_is_half():
    assert confidence_from_trials(int(CONFIDENCE_PRIOR_TRIALS)) == pytest.approx(0.5)


def test_confidence_monotonic_and_bounded():
    values = [confidence_from_trials(t) for t in (0, 1, 5, 20, 500)]
    assert values == sorted(values)
    assert all(0.0 <= v < 1.0 for v in values)


def test_bandit_trials_for_empty_industry_and_missing_store():
    assert bandit_trials_for("") == 0
    # Bandit JSON is truncated per-test by conftest -> no arms -> 0 trials.
    assert bandit_trials_for("plumbing") == 0


# ---------------------------------------------------------------------------
# full cost
# ---------------------------------------------------------------------------


def test_full_cost_sums_all_components():
    # 0.30 tokens + 0.45 voice + 3 * 0.002 email = 0.756
    got = full_cost_usd(
        {"token_cost_usd": 0.30},
        voice_cost_usd=0.45,
        email_sends=3,
    )
    assert got == pytest.approx(0.30 + 0.45 + 3 * EMAIL_SEND_COST_USD)


def test_full_cost_missing_fields_are_zero():
    assert full_cost_usd({}) == 0.0


def test_estimate_voice_cost_no_data_degrades_to_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH", str(tmp_path / "absent.jsonl"))
    assert estimate_voice_cost_usd(3) == 0.0
    assert estimate_voice_cost_usd(0) == 0.0


# ---------------------------------------------------------------------------
# priority formula (hand-computed)
# ---------------------------------------------------------------------------


def test_compute_priority_hand_computed():
    # (1000 * 0.5 * 0.8) / (2.0 * 0.5) = 400 / 1.0 = 400
    got = compute_priority(
        ev_usd=1000.0,
        probability=0.5,
        urgency=0.8,
        time_estimate_hrs=2.0,
        cost_usd=0.5,
    )
    assert got == pytest.approx(400.0)


def test_compute_priority_cost_floor_prevents_divide_blowup():
    # cost 0 -> floored at PRIORITY_COST_FLOOR_USD (0.01):
    # (100 * 1 * 1) / (1 * 0.01) = 10000
    got = compute_priority(
        ev_usd=100.0,
        probability=1.0,
        urgency=1.0,
        time_estimate_hrs=1.0,
        cost_usd=0.0,
    )
    assert got == pytest.approx(100.0 / (1.0 * PRIORITY_COST_FLOOR_USD))


def test_compute_priority_clamps_probability_and_urgency():
    a = compute_priority(
        ev_usd=100, probability=5.0, urgency=3.0, time_estimate_hrs=1.0, cost_usd=1.0
    )
    b = compute_priority(
        ev_usd=100, probability=1.0, urgency=1.0, time_estimate_hrs=1.0, cost_usd=1.0
    )
    assert a == b


# ---------------------------------------------------------------------------
# priority_score (integration of the parts, injected inputs — pure)
# ---------------------------------------------------------------------------


def _opp(**over):
    base = {
        "opportunity_id": "op_1",
        "stage": "qualified",
        "deal_size_usd": 12000.0,
        "close_probability": 0.25,
        "token_cost_usd": 0.50,
        "updated_at": (_NOW - _dt.timedelta(days=14)).isoformat(),
        "industry": "plumbing",
        "policy_family": "",
    }
    base.update(over)
    return base


def test_priority_score_hand_computed_fixture():
    score = priority_score(
        _opp(),
        now=_NOW,
        voice_cost_usd=0.0,
        email_sends=0,
        bandit_trials=15,
    )
    assert isinstance(score, PriorityScore)
    # 14d in "qualified" (half-life 14) -> urgency 0.5
    assert score.urgency == pytest.approx(0.5)
    assert score.ev_usd == 12000.0
    assert score.probability == 0.25
    assert score.cost_usd == pytest.approx(0.50)
    assert score.time_estimate_hrs == STAGE_TIME_ESTIMATE_HRS["qualified"]
    # confidence = 15 / (15 + 5) = 0.75
    assert score.confidence == pytest.approx(0.75)
    # priority = (12000 * 0.25 * 0.5) / (1.0 * 0.5) = 3000
    assert score.priority == pytest.approx(3000.0)


def test_priority_score_prefers_stage_entered_at_over_updated_at():
    opp = _opp(stage_entered_at=(_NOW - _dt.timedelta(days=28)).isoformat())
    score = priority_score(opp, now=_NOW, voice_cost_usd=0.0, bandit_trials=0)
    # 28d at half-life 14 -> 0.25
    assert score.urgency == pytest.approx(0.25)


def test_priority_score_no_timestamps_means_full_urgency():
    opp = _opp(updated_at="", created_at="")
    score = priority_score(opp, now=_NOW, voice_cost_usd=0.0, bandit_trials=0)
    assert score.urgency == 1.0


def test_priority_score_email_and_voice_costs_flow_into_denominator():
    score = priority_score(
        _opp(token_cost_usd=0.0),
        now=_NOW,
        voice_cost_usd=0.98,
        email_sends=1,
        bandit_trials=0,
    )
    assert score.cost_usd == pytest.approx(0.98 + EMAIL_SEND_COST_USD)


def test_priority_score_unknown_stage_uses_default_time_estimate():
    score = priority_score(
        _opp(stage="weird"),
        now=_NOW,
        voice_cost_usd=0.0,
        bandit_trials=0,
    )
    assert score.time_estimate_hrs == DEFAULT_TIME_ESTIMATE_HRS


def test_priority_score_to_dict_round_trip():
    d = priority_score(_opp(), now=_NOW, voice_cost_usd=0.0, bandit_trials=0).to_dict()
    assert set(d) == {
        "ev_usd",
        "probability",
        "urgency",
        "time_estimate_hrs",
        "cost_usd",
        "confidence",
        "priority",
    }


def test_stage_tables_cover_pipeline_stages():
    from backend.crm.pipeline import STAGE_PROBABILITIES

    for stage in STAGE_PROBABILITIES:
        assert stage in STAGE_URGENCY_HALF_LIFE_DAYS
        assert stage in STAGE_TIME_ESTIMATE_HRS
