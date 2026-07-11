"""Tests for the shared, persistent feedback store (backend.common.feedback_store)
and the unification of crm.feedback_engine + outreach.metrics onto it, plus the
determine_pitch_angle feedback loop.

Isolation: conftest points SAMUS_FEEDBACK_STORE_PATH at a per-process tmpfile
and truncates it per test; the autouse fixture here additionally resets to a
known-empty store at the start of each test.
"""
from __future__ import annotations

import json
import os

import pytest

from backend.common import feedback_store


@pytest.fixture(autouse=True)
def _reset_store():
    feedback_store.reset_metrics()
    yield
    feedback_store.reset_metrics()


def _store_file() -> str:
    return os.environ["SAMUS_FEEDBACK_STORE_PATH"]


# ---------------------------------------------------------------------------
# Persistence — survives "restart" (state lives on disk, not in a module dict)
# ---------------------------------------------------------------------------

def test_log_interaction_persists_to_disk():
    feedback_store.log_interaction("p1", "closed", "price", "seo", "pain")

    # Read the raw file — proves the counter is durable, not process-local.
    with open(_store_file(), encoding="utf-8") as fh:
        raw = json.load(fh)

    assert raw["closes"]["seo"] == 1
    assert raw["angles"]["pain"] == {"wins": 1, "losses": 0}
    assert raw["objections"]["price"] == 1


def test_reads_reflect_on_disk_state_written_out_of_band():
    # Simulate a *different process* (e.g. another workcell container that shares
    # the volume) having written the store: write the file directly, then read
    # through the public API. A module-level-dict store would not see this.
    with open(_store_file(), "w", encoding="utf-8") as fh:
        json.dump(
            {"objections": {}, "closes": {"seo": 4},
             "failures": {}, "angles": {"pain": {"wins": 3, "losses": 1}}},
            fh,
        )

    assert feedback_store.get_best_products() == [("seo", 4)]
    assert feedback_store.get_angle_performance()["pain"] == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Unification — crm.feedback_engine + outreach.metrics share ONE store
# ---------------------------------------------------------------------------

def test_crm_and_outreach_engines_accumulate_into_one_store():
    from backend.crm import feedback_engine
    from backend.outreach import metrics

    # A close logged via the CRM surface and a failure logged via the outreach
    # surface must land in the same counters.
    feedback_engine.log_interaction("p1", "closed", None, "seo", "pain")
    metrics.log_interaction("p2", "failed", "too pricey", "ads", "pain")

    # Both delegators see the combined state.
    for snap in (feedback_engine.snapshot(), metrics.snapshot()):
        assert snap["closes"]["seo"] == 1
        assert snap["failures"]["ads"] == 1
        assert snap["objections"]["too pricey"] == 1
        assert snap["angles"]["pain"] == {"wins": 1, "losses": 1}

    # And the angle win-rate folds both outcomes together (1 win / 2 total).
    assert feedback_store.get_angle_performance()["pain"] == pytest.approx(0.5)


def test_reset_metrics_via_either_engine_clears_shared_store():
    from backend.crm import feedback_engine
    from backend.outreach import metrics

    feedback_engine.log_interaction("p1", "closed", None, "seo", "pain")
    metrics.reset_metrics()  # reset via the *other* surface
    assert feedback_engine.snapshot()["closes"] == {}


# ---------------------------------------------------------------------------
# determine_pitch_angle feedback loop — applicability-bounded re-ranking
# ---------------------------------------------------------------------------

# signals/scores where ONLY time_leak + visibility_gap (+ general_growth) apply:
#   has_website True + has_cta True  -> trust_gap / conversion_leak inapplicable
#   automation 80 -> time_leak applies ; seo 80 -> visibility_gap applies
_MULTI_SIGNALS = {"has_website": True, "has_cta": True, "has_booking": True}
_MULTI_SCORES = {"reputation": 0, "automation": 80, "seo": 80, "ads": 0}


def test_pitch_angle_deterministic_when_no_learned_data():
    from backend.prospecting import intelligence

    # No learned_performance -> first-applicable in priority order (time_leak
    # before visibility_gap). Byte-identical to the prior behaviour.
    angle = intelligence.determine_pitch_angle(_MULTI_SIGNALS, _MULTI_SCORES)
    assert angle == "time_leak"


def test_pitch_angle_prefers_higher_winrate_applicable_angle():
    from backend.prospecting import intelligence

    # visibility_gap closes far better than the deterministic pick time_leak.
    learned = {"time_leak": 0.1, "visibility_gap": 0.9}
    angle = intelligence.determine_pitch_angle(
        _MULTI_SIGNALS, _MULTI_SCORES, learned_performance=learned,
    )
    assert angle == "visibility_gap"


def test_pitch_angle_never_picks_inapplicable_angle_even_with_high_winrate():
    from backend.prospecting import intelligence

    # Only general_growth applies (website + cta + booking, all scores low).
    signals = {"has_website": True, "has_cta": True, "has_booking": True}
    scores = {"reputation": 0, "automation": 0, "seo": 0, "ads": 0}
    # trust_gap has a perfect win-rate but does NOT apply here.
    learned = {"trust_gap": 1.0}
    angle = intelligence.determine_pitch_angle(
        signals, scores, learned_performance=learned,
    )
    assert angle == "general_growth"


def test_pitch_angle_keeps_deterministic_pick_when_it_is_best():
    from backend.prospecting import intelligence

    # Deterministic pick (time_leak) is also the best performer -> no change.
    learned = {"time_leak": 0.9, "visibility_gap": 0.2}
    angle = intelligence.determine_pitch_angle(
        _MULTI_SIGNALS, _MULTI_SCORES, learned_performance=learned,
    )
    assert angle == "time_leak"


def test_pitch_angle_avoids_proven_loser_for_neutral_unsampled_peer():
    from backend.prospecting import intelligence

    # time_leak proven bad (0.1); visibility_gap unsampled -> neutral 0.5 prior,
    # which beats the proven loser, so selection moves off time_leak.
    learned = {"time_leak": 0.1}
    angle = intelligence.determine_pitch_angle(
        _MULTI_SIGNALS, _MULTI_SCORES, learned_performance=learned,
    )
    assert angle == "visibility_gap"
