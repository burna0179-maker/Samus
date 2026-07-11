"""Tests for backend.governance.elegance_scorer."""
from __future__ import annotations

import pytest

from backend.governance.elegance_scorer import ElegancePlan, score_elegance


def test_minimal_high_impact_plan_scores_A():
    plan = {
        "steps": [{"kind": "send_email"}],
        "branch_count": 0,
        "llm_calls_required": 0,
        "external_api_calls": 1,
        "novel_capabilities_required": 0,
        "projected_impact": 0.9,
        "reversibility": 1.0,
    }
    r = score_elegance(plan)
    assert isinstance(r, ElegancePlan)
    assert r.grade == "A"
    assert r.score >= 0.8


def test_zero_impact_zeros_score():
    r = score_elegance({"projected_impact": 0.0})
    assert r.score == 0.0
    assert r.grade == "F"
    assert any("projected_impact is 0" in line for line in r.rationale)


def test_complexity_saturation_floors_score():
    plan = {
        "steps": [{} for _ in range(50)],
        "branch_count": 20,
        "llm_calls_required": 10,
        "external_api_calls": 10,
        "novel_capabilities_required": 10,
        "projected_impact": 1.0,
        "reversibility": 1.0,
    }
    r = score_elegance(plan)
    assert r.normalized_complexity == 1.0
    assert r.score == 0.0


def test_multi_llm_calls_flagged_in_rationale():
    plan = {
        "steps": [{} for _ in range(2)],
        "llm_calls_required": 3,
        "projected_impact": 0.5,
        "reversibility": 0.5,
    }
    r = score_elegance(plan)
    assert any("LLM calls" in line for line in r.rationale)


def test_reversibility_floor_at_half_bonus():
    """A fully irreversible high-impact low-complexity plan still scores
    something — reversibility never zeros the result."""
    plan = {
        "steps": [{"kind": "atom"}],
        "projected_impact": 1.0,
        "reversibility": 0.0,
    }
    r = score_elegance(plan)
    # complexity = 1.0; normalized = 1/30 ≈ 0.033
    # score = 1.0 * (1 - 0.033) * (0.5 + 0) = 0.4835...
    assert 0.45 < r.score < 0.50


def test_novel_capability_weighted_3x():
    plan_a = {
        "steps": [{}],
        "branch_count": 3,
        "projected_impact": 1.0,
        "reversibility": 1.0,
    }
    plan_b = dict(plan_a)
    plan_b["novel_capabilities_required"] = 1
    plan_b["branch_count"] = 0
    a = score_elegance(plan_a)
    b = score_elegance(plan_b)
    # plan_a: 1.0 + 4.5 = 5.5
    # plan_b: 1.0 + 3.0 = 4.0    novel beats branches in raw count but
    # at the *same* raw count plan_b would lose. Validate the weight by
    # constructing equal counts:
    p_branch = dict(plan_a, branch_count=2, novel_capabilities_required=0)   # 1.0 + 3.0 = 4.0
    p_novel = dict(plan_a, branch_count=0, novel_capabilities_required=1)    # 1.0 + 3.0 = 4.0
    s_branch = score_elegance(p_branch)
    s_novel = score_elegance(p_novel)
    assert s_branch.complexity == pytest.approx(s_novel.complexity)


def test_nan_impact_defaults_to_zero():
    r = score_elegance({"projected_impact": float("nan")})
    assert r.projected_impact == 0.0
    assert r.score == 0.0


def test_missing_steps_treated_as_empty():
    r = score_elegance({"projected_impact": 0.8, "reversibility": 1.0})
    assert r.components["step_count"] == 0
    assert r.score > 0.0
