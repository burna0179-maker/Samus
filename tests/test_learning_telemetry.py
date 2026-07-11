"""Learning telemetry — bandit / attribution updates are reconstructable.

Covers the standalone module (record shape, persisted filter, fail-soft,
counter) and its wiring into the two learners
(``strategy.portfolio_manager.update_bandit`` +
``attribution.engine.record_outcome``) so every learning update emits a
reconstructable record — what arm, from what outcome, to what wins/trials, and
whether the durable write persisted — without changing the learning outcome.
"""

from __future__ import annotations

import pytest

from backend.common import learning_telemetry, metrics


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SAMUS_LEARNING_TELEMETRY_PATH",
        str(tmp_path / "bandit_learning.jsonl"),
    )
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "jsonl")
    monkeypatch.setenv("SAMUS_STRATEGY_BANDIT_PATH", str(tmp_path / "bandit.json"))
    monkeypatch.setenv("DDB_STRATEGY_BANDIT_TABLE", "")
    monkeypatch.setenv("SAMUS_ATTRIBUTION_PATH", str(tmp_path / "attribution.json"))
    monkeypatch.setenv("DDB_ATTRIBUTION_TABLE", "")
    # Reset the cached singleton stores so they re-read the tmp paths above.
    import backend.strategy.portfolio_manager as pm
    from backend.attribution import store as attr_store

    pm.reset_bandit()
    attr_store.reset_store()


# ---------------------------------------------------------------------------
# Module-direct
# ---------------------------------------------------------------------------


class TestRecordLearningUpdate:
    def test_bandit_update_is_reconstructable(self):
        learning_telemetry.record_learning_update(
            kind=learning_telemetry.KIND_BANDIT,
            arm_id="plumbing::aggressive",
            outcome=1.0,
            reward=0.73,
            wins=3.0,
            trials=5,
            persisted=True,
            density_applied=True,
        )
        rows = learning_telemetry.read_learning(kind="bandit")
        assert len(rows) == 1
        rec = rows[0]
        assert rec["arm_id"] == "plumbing::aggressive"
        assert rec["outcome"] == 1.0
        assert rec["reward"] == 0.73
        assert rec["trials"] == 5
        assert rec["persisted"] is True
        assert rec["density_applied"] is True
        assert rec["update_id"] and rec["trace_id"] and rec["ts"]

    def test_not_persisted_is_recorded_and_filterable(self):
        learning_telemetry.record_learning_update(
            kind=learning_telemetry.KIND_BANDIT,
            arm_id="a",
            outcome=1.0,
            reward=1.0,
            persisted=False,
        )
        learning_telemetry.record_learning_update(
            kind=learning_telemetry.KIND_BANDIT,
            arm_id="b",
            outcome=1.0,
            reward=1.0,
            persisted=True,
        )
        degraded = learning_telemetry.read_learning(persisted=False)
        assert len(degraded) == 1
        assert degraded[0]["arm_id"] == "a"
        kept = learning_telemetry.read_learning(persisted=True)
        assert [r["arm_id"] for r in kept] == ["b"]

    def test_read_filters_by_kind_and_arm(self):
        learning_telemetry.record_learning_update(
            kind=learning_telemetry.KIND_BANDIT,
            arm_id="x",
            outcome=1.0,
            reward=1.0,
        )
        learning_telemetry.record_learning_update(
            kind=learning_telemetry.KIND_VARIANT,
            arm_id="y",
            outcome=1.0,
            reward=1.0,
        )
        assert len(learning_telemetry.read_learning(kind="variant")) == 1
        assert len(learning_telemetry.read_learning(arm_id="x")) == 1

    def test_failsoft_on_ledger_error(self, monkeypatch):
        def _boom(**kwargs):
            raise OSError("disk gone")

        monkeypatch.setattr(learning_telemetry, "open_ledger", _boom)
        rec = learning_telemetry.record_learning_update(
            kind=learning_telemetry.KIND_BANDIT,
            arm_id="a",
            outcome=1.0,
            reward=1.0,
        )
        assert rec["arm_id"] == "a"  # returned despite persistence failure

    def test_counter_increments_with_persisted_label(self):
        before = metrics.SAMUS_LEARNING_UPDATES_TOTAL.labels(
            kind="bandit",
            persisted="false",
        )._value.get()
        learning_telemetry.record_learning_update(
            kind=learning_telemetry.KIND_BANDIT,
            arm_id="a",
            outcome=1.0,
            reward=1.0,
            persisted=False,
        )
        after = metrics.SAMUS_LEARNING_UPDATES_TOTAL.labels(
            kind="bandit",
            persisted="false",
        )._value.get()
        assert after == before + 1


# ---------------------------------------------------------------------------
# Integration: the learners emit
# ---------------------------------------------------------------------------


class TestBanditEmitsLearning:
    def test_update_bandit_emits_learning(self):
        import backend.strategy.portfolio_manager as pm

        pm.update_bandit("accelerate", 1.0)
        rows = learning_telemetry.read_learning(kind="bandit", arm_id="accelerate")
        assert len(rows) == 1
        assert rows[0]["outcome"] == 1.0
        assert rows[0]["trials"] == 1
        assert rows[0]["persisted"] is True  # real JSON store accepts the write

    def test_update_policy_bandit_emits_composite_arm(self):
        import backend.strategy.portfolio_manager as pm

        pm.update_policy_bandit("plumbing", "aggressive", 1.0)
        rows = learning_telemetry.read_learning(kind="bandit")
        assert len(rows) == 1
        # Delegates to update_bandit with the composite key -> one seam covers all.
        assert "::" in rows[0]["arm_id"]
        assert "plumbing" in rows[0]["arm_id"]

    def test_update_bandit_is_failsoft(self, monkeypatch):
        """A telemetry fault must NOT break the learning write."""
        import backend.strategy.portfolio_manager as pm

        def _boom(**kwargs):
            raise RuntimeError("telemetry down")

        monkeypatch.setattr(
            "backend.common.learning_telemetry.record_learning_update",
            _boom,
        )
        pm.update_bandit("accelerate", 1.0)  # must not raise
        assert pm.get_bandit_stats()["accelerate"]["trials"] == 1


class TestVariantEmitsLearning:
    def test_record_outcome_emits_learning(self):
        from backend.attribution import engine

        engine.record_outcome("tmpl::subjA::ctaB", 1.0, won=True)
        rows = learning_telemetry.read_learning(kind="variant")
        assert len(rows) == 1
        rec = rows[0]
        assert rec["arm_id"] == "tmpl::subjA::ctaB"
        assert rec["outcome"] == 1.0
        assert rec["trials"] == 1
        assert rec["persisted"] is True
        assert rec["extra"]["won"] is True

    def test_record_outcome_empty_arm_emits_nothing(self):
        from backend.attribution import engine

        engine.record_outcome("", 1.0)  # early-returns before any learn
        assert learning_telemetry.read_learning(kind="variant") == []
