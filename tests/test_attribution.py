"""Attribution engine — arm_id, reward shaping, store, UCB1 select, learn."""

from __future__ import annotations

import pytest

from backend.attribution import engine
from backend.attribution import store as attr_store


@pytest.fixture
def attr_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DDB_ATTRIBUTION_TABLE", "")
    monkeypatch.setenv("SAMUS_ATTRIBUTION_PATH", str(tmp_path / "attr.json"))
    attr_store.reset_store()
    yield
    attr_store.reset_store()


def _set_flag(monkeypatch, enabled: bool):
    monkeypatch.setenv("SAMUS_ATTRIBUTION_ENABLED", "true" if enabled else "false")
    from backend.common.settings import reload_settings

    reload_settings()


# ---------------------------------------------------------------------------
# arm_id + reward shaping
# ---------------------------------------------------------------------------


def test_build_arm_id_normalizes_blanks():
    assert engine.build_arm_id("cold_v1") == "cold_v1::default::default"
    assert engine.build_arm_id("cold_v1", "subjA", "ctaB") == "cold_v1::subjA::ctaB"


def test_reward_bounds():
    assert engine.reward_from_signals() == 0.0
    assert engine.reward_from_signals(won=True) == 1.0
    assert engine.reward_from_signals(opened=True) == pytest.approx(0.1)
    full = engine.reward_from_signals(opened=True, clicked=True, replied=True, won=True)
    assert full == 1.0  # clamped


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


def test_unknown_arm_loads_none(attr_env):
    assert attr_store.get_store().load("nope::default::default") is None


def test_record_outcome_accrues(attr_env):
    arm = "cold_v1::default::default"
    engine.record_outcome(arm, 0.6, now=1.0)
    engine.record_outcome(arm, 1.0, won=True, now=2.0)
    stats = attr_store.get_store().load(arm)
    assert stats.trials == 2
    assert stats.wins == 1
    assert stats.mean_reward == pytest.approx(0.8)


def test_record_outcome_accrues_even_when_disabled(attr_env, monkeypatch):
    _set_flag(monkeypatch, False)
    engine.record_outcome("a::default::default", 0.5, now=1.0)
    assert attr_store.get_store().load("a::default::default").trials == 1


# ---------------------------------------------------------------------------
# select_variant
# ---------------------------------------------------------------------------


def test_select_passthrough_when_disabled(attr_env, monkeypatch):
    _set_flag(monkeypatch, False)
    choice = engine.select_variant(["a", "b", "c"])
    assert choice.arm_id == "a"  # deterministic first = current behavior
    assert choice.reason == "passthrough"


def test_select_single_candidate(attr_env, monkeypatch):
    _set_flag(monkeypatch, True)
    choice = engine.select_variant(["only"])
    assert choice.arm_id == "only"


def test_select_explores_cold_arm_first(attr_env, monkeypatch):
    _set_flag(monkeypatch, True)
    # "a" has data, "b" is cold -> cold explored first.
    engine.record_outcome("a", 0.9, now=1.0)
    choice = engine.select_variant(["a", "b"])
    assert choice.arm_id == "b"
    assert choice.reason == "explore_cold"


def test_select_prefers_higher_mean_when_all_warm(attr_env, monkeypatch):
    _set_flag(monkeypatch, True)
    # Give both arms equal trials so the UCB exploration term is equal; the
    # higher mean reward then wins.
    for _ in range(10):
        engine.record_outcome("good", 0.9, now=1.0)
        engine.record_outcome("bad", 0.1, now=1.0)
    choice = engine.select_variant(["good", "bad"])
    assert choice.arm_id == "good"
    assert choice.reason == "ucb1"


def test_select_no_candidates(attr_env, monkeypatch):
    _set_flag(monkeypatch, True)
    assert engine.select_variant([]).arm_id == ""


def test_snapshot_shape(attr_env):
    engine.record_outcome("x::y::z", 1.0, won=True, now=1.0)
    snap = engine.snapshot("x::y::z")
    assert snap["trials"] == 1 and snap["wins"] == 1
    assert snap["mean_reward"] == 1.0
