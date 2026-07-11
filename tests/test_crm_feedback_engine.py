"""Tests for backend.crm.feedback_engine (in-memory v1).

Each test calls reset_metrics() first for isolation, since the engine stores
state at module level.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset():
    """Ensure a clean slate before every test in this module."""
    from backend.crm import feedback_engine
    feedback_engine.reset_metrics()
    yield
    feedback_engine.reset_metrics()


# ---------------------------------------------------------------------------
# reset_metrics
# ---------------------------------------------------------------------------

def test_reset_metrics_yields_empty_state():
    from backend.crm import feedback_engine

    # Populate some state first.
    feedback_engine.log_interaction("p1", "closed", "price", "seo", "pain")
    # Now reset and confirm everything is empty.
    feedback_engine.reset_metrics()

    snap = feedback_engine.snapshot()
    assert snap["objections"] == {}
    assert snap["closes"] == {}
    assert snap["failures"] == {}
    assert snap["angles"] == {}


# ---------------------------------------------------------------------------
# log_interaction — closed outcome
# ---------------------------------------------------------------------------

def test_log_interaction_closed_increments_closes_and_angle_wins():
    from backend.crm import feedback_engine

    feedback_engine.log_interaction("p1", "closed", None, "seo", "pain")
    snap = feedback_engine.snapshot()

    assert snap["closes"]["seo"] == 1
    assert snap["angles"]["pain"]["wins"] == 1
    assert snap["angles"]["pain"]["losses"] == 0
    assert snap["failures"] == {}


# ---------------------------------------------------------------------------
# log_interaction — non-closed outcome
# ---------------------------------------------------------------------------

def test_log_interaction_not_closed_increments_failures_and_angle_losses():
    from backend.crm import feedback_engine

    feedback_engine.log_interaction("p2", "failed", None, "ads", "value")
    snap = feedback_engine.snapshot()

    assert snap["failures"]["ads"] == 1
    assert snap["angles"]["value"]["losses"] == 1
    assert snap["angles"]["value"]["wins"] == 0
    assert snap["closes"] == {}


# ---------------------------------------------------------------------------
# log_interaction — objection handling
# ---------------------------------------------------------------------------

def test_log_interaction_with_objection_increments_objection_count():
    from backend.crm import feedback_engine

    feedback_engine.log_interaction("p3", "failed", "too expensive", "seo", "pain")
    snap = feedback_engine.snapshot()

    assert snap["objections"]["too expensive"] == 1


def test_log_interaction_without_objection_does_not_touch_objections():
    from backend.crm import feedback_engine

    feedback_engine.log_interaction("p4", "closed", None, "seo", "pain")
    snap = feedback_engine.snapshot()

    assert snap["objections"] == {}


def test_log_interaction_accumulates_objection_count_across_calls():
    from backend.crm import feedback_engine

    feedback_engine.log_interaction("p5", "failed", "too expensive", "seo", "pain")
    feedback_engine.log_interaction("p6", "failed", "too expensive", "ads", "value")
    feedback_engine.log_interaction("p7", "failed", "not interested", "seo", "pain")

    snap = feedback_engine.snapshot()
    assert snap["objections"]["too expensive"] == 2
    assert snap["objections"]["not interested"] == 1


# ---------------------------------------------------------------------------
# get_top_objections
# ---------------------------------------------------------------------------

def test_get_top_objections_sorted_desc():
    from backend.crm import feedback_engine

    feedback_engine.log_interaction("p1", "failed", "price", "seo", "pain")
    feedback_engine.log_interaction("p2", "failed", "price", "seo", "pain")
    feedback_engine.log_interaction("p3", "failed", "timing", "seo", "pain")

    top = feedback_engine.get_top_objections()
    assert top[0] == ("price", 2)
    assert top[1] == ("timing", 1)


def test_get_top_objections_empty_when_no_interactions():
    from backend.crm import feedback_engine

    assert feedback_engine.get_top_objections() == []


# ---------------------------------------------------------------------------
# get_best_products
# ---------------------------------------------------------------------------

def test_get_best_products_sorted_desc():
    from backend.crm import feedback_engine

    feedback_engine.log_interaction("p1", "closed", None, "seo", "pain")
    feedback_engine.log_interaction("p2", "closed", None, "seo", "value")
    feedback_engine.log_interaction("p3", "closed", None, "ads", "pain")

    best = feedback_engine.get_best_products()
    assert best[0] == ("seo", 2)
    assert best[1] == ("ads", 1)


def test_get_best_products_empty_when_no_closes():
    from backend.crm import feedback_engine

    feedback_engine.log_interaction("p1", "failed", None, "seo", "pain")
    assert feedback_engine.get_best_products() == []


# ---------------------------------------------------------------------------
# get_angle_performance
# ---------------------------------------------------------------------------

def test_get_angle_performance_yields_win_rate():
    from backend.crm import feedback_engine

    feedback_engine.log_interaction("p1", "closed", None, "seo", "pain")
    feedback_engine.log_interaction("p2", "failed", None, "seo", "pain")
    feedback_engine.log_interaction("p3", "closed", None, "seo", "value")

    perf = feedback_engine.get_angle_performance()
    assert perf["pain"] == pytest.approx(0.5)
    assert perf["value"] == pytest.approx(1.0)


def test_get_angle_performance_skips_zero_total_angles():
    import json
    import os

    from backend.crm import feedback_engine

    # A zero-total angle can only arise from a persisted 0/0 record (the public
    # API always adds a win or a loss). Write one directly to the store file
    # and confirm get_angle_performance skips it.
    path = os.environ["SAMUS_FEEDBACK_STORE_PATH"]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {"objections": {}, "closes": {}, "failures": {},
             "angles": {"ghost": {"wins": 0, "losses": 0}}},
            fh,
        )

    perf = feedback_engine.get_angle_performance()
    assert "ghost" not in perf


# ---------------------------------------------------------------------------
# optimize_weights
# ---------------------------------------------------------------------------

def test_optimize_weights_sets_best_angle_in_strategy():
    from backend.crm import feedback_engine

    feedback_engine.log_interaction("p1", "closed", None, "seo", "value")
    feedback_engine.log_interaction("p2", "closed", None, "seo", "value")
    feedback_engine.log_interaction("p3", "failed", None, "seo", "pain")

    intel: dict = {}
    result = feedback_engine.optimize_weights(intel)

    assert result["strategy"]["angle_bias"] == "value"


def test_optimize_weights_merges_into_existing_strategy():
    from backend.crm import feedback_engine

    feedback_engine.log_interaction("p1", "closed", None, "seo", "pain")

    intel: dict = {"strategy": {"existing_key": "existing_value"}}
    result = feedback_engine.optimize_weights(intel)

    assert result["strategy"]["angle_bias"] == "pain"
    assert result["strategy"]["existing_key"] == "existing_value"


def test_optimize_weights_no_data_leaves_intel_strategy_absent():
    from backend.crm import feedback_engine

    intel: dict = {}
    result = feedback_engine.optimize_weights(intel)

    assert "strategy" not in result


# ---------------------------------------------------------------------------
# snapshot — type safety
# ---------------------------------------------------------------------------

def test_snapshot_returns_plain_dicts_not_defaultdicts():
    from collections import defaultdict
    from backend.crm import feedback_engine

    feedback_engine.log_interaction("p1", "closed", "objA", "seo", "pain")
    snap = feedback_engine.snapshot()

    assert type(snap["objections"]) is dict
    assert type(snap["closes"]) is dict
    assert type(snap["failures"]) is dict
    assert type(snap["angles"]) is dict
    for v in snap["angles"].values():
        assert type(v) is dict
    # Confirm none are defaultdicts
    assert not isinstance(snap["objections"], defaultdict)
    assert not isinstance(snap["closes"], defaultdict)
    assert not isinstance(snap["failures"], defaultdict)
    assert not isinstance(snap["angles"], defaultdict)
