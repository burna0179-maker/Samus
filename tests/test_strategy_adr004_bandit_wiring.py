"""ADR-004 harm-aware reward -> bandit wiring (DORMANT, flag-gated).

backend.strategy.portfolio_manager.record_outcome credits the bandit arm with
the legacy won/lost scalar by default. When SAMUS_REWARD_ADR004_BANDIT_ENABLED
is set, it instead credits compute_reward (the ADR-004 opportunity-level
reward). These tests pin: (1) off-by-default = legacy behaviour, (2) on =
ADR-004 reward, (3) fail-open fallbacks so the learn path never breaks.
"""

from __future__ import annotations

import types

import pytest

from backend.strategy import portfolio_manager as pm


@pytest.fixture
def captured_bandit(monkeypatch):
    """Capture update_bandit(arm, reward) without touching the bandit store."""
    seen: dict[str, object] = {}

    def _fake_update(arm_id, reward, **_kw):
        seen["arm"] = arm_id
        seen["reward"] = reward

    monkeypatch.setattr(pm, "update_bandit", _fake_update)
    return seen


# ---------------------------------------------------------------------------
# Flag gate on record_outcome
# ---------------------------------------------------------------------------


def test_disabled_by_default_uses_scalar_credit(captured_bandit, monkeypatch):
    monkeypatch.delenv("SAMUS_REWARD_ADR004_BANDIT_ENABLED", raising=False)
    pm.record_outcome("p1", "policy_A", "won")
    assert captured_bandit == {"arm": "policy_A", "reward": 1.0}


def test_lost_outcome_floors_scalar_at_zero(captured_bandit, monkeypatch):
    monkeypatch.delenv("SAMUS_REWARD_ADR004_BANDIT_ENABLED", raising=False)
    pm.record_outcome("p1", "policy_A", "lost")  # credit -0.5 -> floored to 0.0
    assert captured_bandit["reward"] == 0.0


def test_enabled_uses_adr004_reward(captured_bandit, monkeypatch):
    monkeypatch.setenv("SAMUS_REWARD_ADR004_BANDIT_ENABLED", "1")
    monkeypatch.setattr(pm, "_adr004_reward_for_prospect", lambda pid: 42.0)
    pm.record_outcome("p1", "policy_A", "won")
    assert captured_bandit["reward"] == 42.0


def test_enabled_but_reward_unavailable_falls_back_to_scalar(captured_bandit, monkeypatch):
    monkeypatch.setenv("SAMUS_REWARD_ADR004_BANDIT_ENABLED", "1")
    monkeypatch.setattr(pm, "_adr004_reward_for_prospect", lambda pid: None)
    pm.record_outcome("p1", "policy_A", "won")
    assert captured_bandit["reward"] == 1.0  # scalar fallback


# ---------------------------------------------------------------------------
# _adr004_reward_for_prospect — lookup + fail-open
# ---------------------------------------------------------------------------


def test_reward_helper_returns_compute_reward(monkeypatch):
    from backend.crm import service as crm_service
    from backend.strategy import reward_density

    monkeypatch.setattr(
        crm_service,
        "get_opportunity_for_prospect",
        lambda pid: types.SimpleNamespace(opportunity_id="op-1"),
    )
    monkeypatch.setattr(
        reward_density,
        "compute_reward",
        lambda opp_id, **_kw: types.SimpleNamespace(reward=7.5),
    )
    assert pm._adr004_reward_for_prospect("p1") == 7.5


def test_reward_helper_none_when_no_opportunity(monkeypatch):
    from backend.crm import service as crm_service

    monkeypatch.setattr(
        crm_service,
        "get_opportunity_for_prospect",
        lambda pid: None,
    )
    assert pm._adr004_reward_for_prospect("p1") is None


def test_reward_helper_fail_open_on_compute_error(monkeypatch):
    from backend.crm import service as crm_service
    from backend.strategy import reward_density

    monkeypatch.setattr(
        crm_service,
        "get_opportunity_for_prospect",
        lambda pid: types.SimpleNamespace(opportunity_id="op-1"),
    )

    def _boom(opp_id, **_kw):
        raise RuntimeError("codex down")

    monkeypatch.setattr(reward_density, "compute_reward", _boom)
    assert pm._adr004_reward_for_prospect("p1") is None


def test_flag_parsing_truthy_values(monkeypatch):
    for raw in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("SAMUS_REWARD_ADR004_BANDIT_ENABLED", raw)
        assert pm._adr004_bandit_enabled() is True
    for raw in ("", "0", "false", "off", "no"):
        monkeypatch.setenv("SAMUS_REWARD_ADR004_BANDIT_ENABLED", raw)
        assert pm._adr004_bandit_enabled() is False
