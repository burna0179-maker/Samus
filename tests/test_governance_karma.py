"""Karma-gated permissions — store, decay, reward/penalty, check, gate wiring."""
from __future__ import annotations

from typing import Any

import pytest

from backend.governance.karma import engine
from backend.governance.karma import store as karma_store
from backend.governance.karma.store import DEFAULT_BASELINE, KarmaVector


@pytest.fixture
def karma_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DDB_KARMA_TABLE", "")
    monkeypatch.setenv("SAMUS_KARMA_PATH", str(tmp_path / "karma.json"))
    karma_store.reset_store()
    yield
    karma_store.reset_store()


def _set_flag(monkeypatch, enabled: bool):
    monkeypatch.setenv("SAMUS_KARMA_GATE_ENABLED", "true" if enabled else "false")
    from backend.common.settings import reload_settings
    reload_settings()


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

def test_unknown_actor_loads_innocent_baseline(karma_env):
    vec = karma_store.get_store().load("nobody")
    assert vec.success_rate == DEFAULT_BASELINE
    assert vec.policy_compliance == DEFAULT_BASELINE


def test_save_and_reload_roundtrip(karma_env):
    store = karma_store.get_store()
    store.save(KarmaVector(actor="samus", success_rate=0.5, policy_compliance=0.6,
                           resource_efficiency=0.7, stability_score=0.8, updated_ts=123.0))
    vec = store.load("samus")
    assert vec.policy_compliance == 0.6
    assert vec.stability_score == 0.8


def test_from_item_clamps_and_defaults():
    vec = KarmaVector.from_item({"actor": "x", "success_rate": 2.0, "policy_compliance": "bad"})
    assert vec.success_rate == 1.0           # clamped
    assert vec.policy_compliance == DEFAULT_BASELINE  # non-numeric -> baseline


# ---------------------------------------------------------------------------
# decay
# ---------------------------------------------------------------------------

def test_deviation_halves_after_one_half_life(karma_env):
    now = 1_700_000_000.0
    store = karma_store.get_store()
    # success_rate 0.45 (deviation -0.40 from 0.85 baseline), stamped one
    # half-life ago.
    store.save(KarmaVector(actor="samus", success_rate=0.45,
                           policy_compliance=DEFAULT_BASELINE,
                           resource_efficiency=DEFAULT_BASELINE,
                           stability_score=DEFAULT_BASELINE,
                           updated_ts=now - engine.HALF_LIFE_DAYS * 86400.0))
    snap = engine.snapshot(now=now)
    # 0.85 + (0.45-0.85)*0.5 = 0.65
    assert snap["dimensions"]["success_rate"] == pytest.approx(0.65, abs=1e-3)


def test_no_decay_without_timestamp(karma_env):
    store = karma_store.get_store()
    store.save(KarmaVector(actor="samus", success_rate=0.45, updated_ts=0.0))
    snap = engine.snapshot(now=1_700_000_000.0)
    assert snap["dimensions"]["success_rate"] == pytest.approx(0.45)  # untouched


# ---------------------------------------------------------------------------
# reward / penalty
# ---------------------------------------------------------------------------

def test_penalty_lowers_dimension_and_audits(karma_env, monkeypatch):
    _set_flag(monkeypatch, False)  # accrues even while gate is dormant
    audited: list = []
    monkeypatch.setattr("backend.common.audit.record",
                        lambda et, **kw: audited.append((et, kw)))
    engine.apply_outcome("complaint", now=1_700_000_000.0)
    vec = karma_store.get_store().load("samus")
    assert vec.policy_compliance < DEFAULT_BASELINE   # penalized
    assert vec.success_rate < DEFAULT_BASELINE        # complaint hits both
    assert audited and audited[0][0] == "karma.delta"


def test_reward_raises_then_caps_at_one(karma_env):
    store = karma_store.get_store()
    store.save(KarmaVector(actor="samus", success_rate=0.98, updated_ts=1.0))
    for _ in range(20):
        engine.apply_outcome("deal_won", now=2.0)
    assert store.load("samus").success_rate <= 1.0


def test_unknown_outcome_is_noop(karma_env):
    engine.apply_outcome("nonsense_event", now=1.0)
    assert karma_store.get_store().load("samus").success_rate == DEFAULT_BASELINE


# ---------------------------------------------------------------------------
# check — the gate decision
# ---------------------------------------------------------------------------

def test_check_disabled_is_noop_allow(karma_env, monkeypatch):
    _set_flag(monkeypatch, False)
    # Even a floored actor is allowed when the gate is off.
    karma_store.get_store().save(KarmaVector(actor="samus", success_rate=0.0,
                                             policy_compliance=0.0, resource_efficiency=0.0,
                                             stability_score=0.0, updated_ts=1.0))
    d = engine.check("outreach_send", now=2.0)
    assert d.allowed is True
    assert d.reason == "karma_disabled"


def test_check_enabled_innocent_allows(karma_env, monkeypatch):
    _set_flag(monkeypatch, True)
    d = engine.check("outreach_send")  # unknown actor -> baseline 0.85 -> autonomous
    assert d.allowed is True
    assert d.tier == "autonomous"


def test_check_enabled_penalized_denies(karma_env, monkeypatch):
    _set_flag(monkeypatch, True)
    karma_store.get_store().save(KarmaVector(actor="samus", success_rate=0.3,
                                             policy_compliance=0.3, resource_efficiency=0.3,
                                             stability_score=0.3, updated_ts=1.0))
    d = engine.check("outreach_send", now=1.0)
    assert d.allowed is False
    assert "insufficient_karma" in d.reason


def test_required_tier_map():
    from backend.strategy.trust_scorer import AccessTier
    assert engine.required_tier("outreach_send") == AccessTier.AUTONOMOUS
    assert engine.required_tier("opportunity_write") == AccessTier.MULTI_AGENT
    assert engine.required_tier("unknown") == AccessTier.MULTI_AGENT  # default


def test_check_fail_open_on_store_error(karma_env, monkeypatch):
    _set_flag(monkeypatch, True)

    def _boom():
        raise RuntimeError("store down")

    monkeypatch.setattr(karma_store, "get_store", _boom)
    d = engine.check("outreach_send")
    assert d.allowed is True  # fail-open to innocent
    assert "failopen" in d.reason


# ---------------------------------------------------------------------------
# gate.py integration
# ---------------------------------------------------------------------------

def test_gate_blocks_on_low_karma_when_enabled(karma_env, monkeypatch):
    import time
    _set_flag(monkeypatch, True)
    # Floor the actor so opportunity_write (requires MULTI_AGENT) is denied.
    # updated_ts must be recent — gate.check uses real time + the vector decays
    # toward the innocent baseline over a 14-day half-life, so an ancient
    # timestamp would (correctly) have decayed the penalty away.
    karma_store.get_store().save(KarmaVector(actor="samus", success_rate=0.2,
                                             policy_compliance=0.2, resource_efficiency=0.2,
                                             stability_score=0.2, updated_ts=time.time()))

    from backend.cash_engine import gate as gate_mod
    from backend.cash_engine.models import RevenueTriggerRequest

    class _Opp:
        opportunity_id = "op_1"
        stake_sentence = "Operator-authored stake."

    class _Crm:
        def get_opportunity_for_prospect(self, pid):
            return _Opp()

    # Codex passes so we reach the karma check.
    monkeypatch.setattr(gate_mod, "check_action",
                        lambda action: type("V", (), {"allowed": True})())

    req = RevenueTriggerRequest(prospect_id="pr_1", trigger_source="manual_review")
    outcome = gate_mod.evaluate_gate(req, crm=_Crm())
    assert outcome.allowed is False
    assert outcome.required_protocol == "karma"


def test_gate_allows_innocent_when_enabled(karma_env, monkeypatch):
    _set_flag(monkeypatch, True)  # unknown actor -> innocent baseline -> autonomous

    from backend.cash_engine import gate as gate_mod
    from backend.cash_engine.models import RevenueTriggerRequest

    class _Opp:
        opportunity_id = "op_1"
        stake_sentence = "Operator-authored stake."

    class _Crm:
        def get_opportunity_for_prospect(self, pid):
            return _Opp()

    monkeypatch.setattr(gate_mod, "check_action",
                        lambda action: type("V", (), {"allowed": True})())

    req = RevenueTriggerRequest(prospect_id="pr_1", trigger_source="manual_review")
    outcome = gate_mod.evaluate_gate(req, crm=_Crm())
    assert outcome.allowed is True
