"""Integration tests — portfolio_manager bandit on the durable store.

These prove the public pure-logic API (``update_bandit``,
``update_policy_bandit``, ``get_bandit_stats``, ``select_best_policy``,
``_ucb1_score``, ``reset_bandit``) behaves identically to the old in-memory
bandit when reading its own writes, AND that the state now survives the
decide / learn process split (two store instances on the same backing file).

The conftest points the bandit store's JSON fallback at a per-process
tmpfile and forces ``DDB_STRATEGY_BANDIT_TABLE=""``, so these run against the
JSON file only — no real DDB.
"""

from __future__ import annotations

import pytest

import backend.strategy.portfolio_manager as pm
from backend.strategy.bandit_store import BanditStore, set_default_store


# ---------------------------------------------------------------------------
# Read-your-own-writes — identical behaviour to the in-memory bandit
# ---------------------------------------------------------------------------


def test_update_bandit_persists_and_reads_back():
    """update_bandit -> get_bandit_stats round-trips through the store."""
    pm.reset_bandit()
    pm.update_bandit("accelerate", 1.0)
    pm.update_bandit("accelerate", 0.0)

    stats = pm.get_bandit_stats()
    assert stats["accelerate"]["trials"] == 2
    assert stats["accelerate"]["wins"] == pytest.approx(1.0)
    pm.reset_bandit()


def test_update_policy_bandit_persists_composite_arm():
    """update_policy_bandit's composite arm is durable + readable."""
    pm.reset_bandit()
    pm.update_policy_bandit("hvac", "fast_quote_mode", 1.0)
    pm.update_policy_bandit("hvac", "fast_quote_mode", 0.5)

    stats = pm.get_bandit_stats()
    assert stats["hvac::fast_quote_mode"]["trials"] == 2
    assert stats["hvac::fast_quote_mode"]["wins"] == pytest.approx(1.5)
    pm.reset_bandit()


def test_reward_signal_density_still_flows_through_persistence():
    """reward_signal density weighting is unchanged — only storage moved."""
    pm.reset_bandit()
    from backend.strategy.reward_density import RewardSignal, compute_reward_density

    sig = RewardSignal(
        outcome=1.0,
        owner_email=True,
        social_facebook=True,
        social_instagram=False,
        seo_score=0.2,
        contactability=0.8,
        infrastructure_health=0.7,
    )
    pm.update_bandit("vertical_arm", 0.0, reward_signal=sig)
    stats = pm.get_bandit_stats()
    assert stats["vertical_arm"]["wins"] == pytest.approx(compute_reward_density(sig))
    pm.reset_bandit()


def test_select_best_policy_exploits_persisted_winner():
    """select_best_policy reads persisted arms — exploits the clear winner."""
    pm.reset_bandit()
    from backend.strategy.policy_compiler import POLICY_FAMILIES

    families = POLICY_FAMILIES["dentist"]
    winner = families[1]
    for fam in families:
        reward = 5.0 if fam == winner else 0.0
        pm.update_policy_bandit("dentist", fam, reward)

    assert pm.select_best_policy("dentist") == winner
    pm.reset_bandit()


def test_get_policy_bandit_stats_isolates_industry_from_store():
    """get_policy_bandit_stats still filters to one industry after the move."""
    pm.reset_bandit()
    pm.update_policy_bandit("hvac", "fast_quote_mode", 1.0)
    pm.update_policy_bandit("dentist", "new_patient_offer", 1.0)
    pm.update_bandit("flat_arm", 1.0)

    hvac = pm.get_policy_bandit_stats("hvac")
    assert set(hvac) == {"hvac::fast_quote_mode"}
    pm.reset_bandit()


def test_reset_bandit_clears_persisted_state():
    """reset_bandit wipes the store's JSON fallback, not just memory."""
    pm.reset_bandit()
    pm.update_bandit("accelerate", 1.0)
    assert pm.get_bandit_stats()

    pm.reset_bandit()
    assert pm.get_bandit_stats() == {}


# ---------------------------------------------------------------------------
# Survives restart / cross-process — the whole point of Unit 1
# ---------------------------------------------------------------------------


def test_bandit_survives_a_simulated_process_restart(tmp_path):
    """State written by one store instance is visible after a 'restart'.

    A process restart == a brand-new BanditStore with an empty cache reading
    the same backing file. We swap portfolio_manager's default store to two
    successive instances on the same path.
    """
    json_path = str(tmp_path / "restart.json")

    # --- process 1: the host-side decide+learn writes some history ---
    set_default_store(BanditStore(ddb_table="", json_path=json_path))
    pm.update_bandit("hvac", 1.0)
    pm.update_policy_bandit("hvac", "fast_quote_mode", 1.0)

    # --- process 2: restart — fresh store, fresh cache, same file ---
    set_default_store(BanditStore(ddb_table="", json_path=json_path))
    stats = pm.get_bandit_stats()
    assert stats["hvac"]["trials"] == 1
    assert stats["hvac::fast_quote_mode"]["trials"] == 1

    set_default_store(None)


def test_decide_step_sees_learn_step_writes_from_another_process(tmp_path):
    """The decide / learn split: learn writes in process B, decide reads in A.

    portfolio_manager always reaches the *current* default store, so we model
    the container-side learn step by pointing a second store instance at the
    same file and recording an outcome, then model the host-side decide step
    by swapping back and selecting.
    """
    json_path = str(tmp_path / "decide_learn.json")
    families_store = BanditStore(ddb_table="", json_path=json_path)

    # Container-side LEARN: record strong reward on one policy family.
    set_default_store(families_store)
    from backend.strategy.policy_compiler import POLICY_FAMILIES

    families = POLICY_FAMILIES["plumber"]
    winner = families[2]
    for fam in families:
        pm.update_policy_bandit("plumber", fam, 9.0 if fam == winner else 0.0)

    # Host-side DECIDE: a fresh store instance (separate process) selects.
    set_default_store(BanditStore(ddb_table="", json_path=json_path))
    assert pm.select_best_policy("plumber") == winner

    set_default_store(None)


# ---------------------------------------------------------------------------
# Graceful degradation through the public API
# ---------------------------------------------------------------------------


def test_update_bandit_does_not_raise_on_store_failure(monkeypatch):
    """A catastrophic store failure must not break update_bandit."""
    pm.reset_bandit()
    from backend.strategy import bandit_store as bs

    class _BrokenStore:
        def add(self, *_a, **_kw):
            raise RuntimeError("store gone")

        def read_all(self):
            raise RuntimeError("store gone")

        def clear(self):
            pass

    monkeypatch.setattr(bs, "get_default_store", lambda: _BrokenStore())
    # Must degrade, not raise.
    pm.update_bandit("accelerate", 1.0)


def test_select_best_policy_does_not_raise_on_store_failure(monkeypatch):
    """select_best_policy degrades to exploration when the store is down."""
    from backend.strategy import bandit_store as bs
    from backend.strategy.policy_compiler import POLICY_FAMILIES

    class _BrokenStore:
        def add(self, *_a, **_kw):
            return False

        def read_all(self):
            raise RuntimeError("store gone")

        def clear(self):
            pass

    monkeypatch.setattr(bs, "get_default_store", lambda: _BrokenStore())
    # No persisted arms -> every family is unseen -> returns a valid family.
    choice = pm.select_best_policy("hvac")
    assert choice in POLICY_FAMILIES["hvac"]


def test_get_bandit_stats_degrades_to_empty_on_store_failure(monkeypatch):
    """get_bandit_stats returns {} rather than raising when the store fails."""
    from backend.strategy import bandit_store as bs

    class _BrokenStore:
        def add(self, *_a, **_kw):
            return False

        def read_all(self):
            raise RuntimeError("store gone")

        def clear(self):
            pass

    monkeypatch.setattr(bs, "get_default_store", lambda: _BrokenStore())
    assert pm.get_bandit_stats() == {}


# ---------------------------------------------------------------------------
# _BANDIT_TOTAL derivation — sum of all arms' trials
# ---------------------------------------------------------------------------


def test_bandit_total_is_sum_of_trials():
    """_BANDIT_TOTAL is derived as the sum of every arm's trial count."""
    pm.reset_bandit()
    pm.update_bandit("a", 1.0)
    pm.update_bandit("a", 0.0)
    pm.update_bandit("b", 1.0)
    # Force a refresh of the cached total.
    pm._load_bandit_state()
    assert pm._BANDIT_TOTAL == 3  # 2 trials on a + 1 on b
    pm.reset_bandit()


def test_ucb1_score_is_pure_and_unchanged():
    """_ucb1_score stays a pure function — unseen arm -> inf, else mean+bonus."""
    assert pm._ucb1_score(0.0, 0, 10) == float("inf")
    # mean only when total <= 0
    assert pm._ucb1_score(2.0, 4, 0) == pytest.approx(0.5)
    # mean + exploration bonus when total > 0
    assert pm._ucb1_score(2.0, 4, 10) > 0.5
