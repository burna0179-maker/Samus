"""Tranche 3 — experiment registry, significance gate, nightly promoter."""

from __future__ import annotations


import pytest


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "jsonl")
    monkeypatch.setenv("SAMUS_ATTRIBUTION_PATH", str(tmp_path / "attr.json"))
    monkeypatch.setenv(
        "SAMUS_CONVERSION_FUNNEL_PATH",
        str(tmp_path / "funnel_default.jsonl"),
    )
    monkeypatch.delenv("DDB_ATTRIBUTION_TABLE", raising=False)
    from backend.attribution import store as attr_store

    attr_store.reset_store()
    yield
    attr_store.reset_store()


def _seed_arm(experiment_id: str, arm: str, *, trials: int, wins: int):
    from backend.attribution import store as attr_store
    from backend.attribution.store import VariantStats
    from backend.experiments.registry import build_experiment_arm_id

    arm_id = build_experiment_arm_id(experiment_id, arm)
    attr_store.get_store().save(
        VariantStats(
            arm_id=arm_id,
            trials=trials,
            wins=wins,
            reward_sum=float(wins),
        )
    )


# ---------------------------------------------------------------------------
# significance
# ---------------------------------------------------------------------------
class TestSignificance:
    def test_clear_difference_is_significant(self):
        from backend.experiments.significance import is_significant

        assert (
            is_significant(
                {"wins": 60, "trials": 100},
                {"wins": 30, "trials": 100},
            )
            is True
        )

    def test_small_sample_not_significant(self):
        from backend.experiments.significance import is_significant

        assert (
            is_significant(
                {"wins": 2, "trials": 3},
                {"wins": 1, "trials": 3},
            )
            is False
        )

    def test_identical_rates_not_significant(self):
        from backend.experiments.significance import is_significant

        assert (
            is_significant(
                {"wins": 50, "trials": 100},
                {"wins": 50, "trials": 100},
            )
            is False
        )

    def test_zero_trials_never_significant(self):
        from backend.experiments.significance import is_significant

        assert is_significant({"wins": 0, "trials": 0}, {"wins": 9, "trials": 10}) is False

    def test_z_statistic_known_value(self):
        from backend.experiments.significance import two_proportion_z

        # p_a=0.6, p_b=0.4, n=100 each, pooled=0.5 -> z = 0.2/sqrt(0.5*0.5*0.02)
        z = two_proportion_z(60, 100, 40, 100)
        assert abs(z - 2.8284271) < 1e-6

    def test_p_value_two_sided(self):
        from backend.experiments.significance import p_value_two_sided

        assert abs(p_value_two_sided(1.959963985) - 0.05) < 1e-6

    def test_degenerate_pool_no_variance(self):
        from backend.experiments.significance import two_proportion_z

        assert two_proportion_z(10, 10, 5, 5) == 0.0


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
class TestRegistry:
    def test_register_get_list_roundtrip(self):
        from backend.experiments import registry

        exp = registry.register_experiment(
            dimension="opener",
            arms=["direct", "curiosity"],
            experiment_id="exp-op-1",
            min_trials=10,
        )
        assert exp.experiment_id == "exp-op-1"
        got = registry.get_experiment("exp-op-1")
        assert got is not None and got.arms == ["direct", "curiosity"]
        assert [e.experiment_id for e in registry.list_experiments()] == ["exp-op-1"]
        assert registry.list_experiments(status="paused") == []

    def test_register_rejects_bad_input(self):
        from backend.experiments import registry

        with pytest.raises(ValueError):
            registry.register_experiment(dimension="nope", arms=["a", "b"])
        with pytest.raises(ValueError):
            registry.register_experiment(dimension="offer", arms=["only-one"])

    def test_assign_delegates_to_ucb1_with_namespaced_arms(self, monkeypatch):
        from backend.experiments import registry

        registry.register_experiment(
            dimension="subject",
            arms=["a", "b"],
            experiment_id="exp-s",
        )
        seen: list[list[str]] = []

        from backend.attribution.engine import VariantChoice

        def fake_select(cands, **kw):
            seen.append(list(cands))
            return VariantChoice(arm_id=cands[1], reason="ucb1", score=1.0)

        monkeypatch.setattr("backend.attribution.engine.select_variant", fake_select)
        out = registry.assign("exp-s", context={"prospect_id": "p1"})
        assert out["ok"] is True
        assert out["arm"] == "b"
        assert out["arm_id"] == "exp::exp-s::b"
        assert seen == [["exp::exp-s::a", "exp::exp-s::b"]]

    def test_assign_appends_ledger_and_emits_event(self, monkeypatch):
        from backend.experiments import registry

        registry.register_experiment(
            dimension="cta",
            arms=["x", "y"],
            experiment_id="exp-c",
        )
        emitted: list[tuple] = []
        monkeypatch.setattr(
            "backend.experiments.registry.emit_business_event",
            lambda et, **kw: emitted.append((et, kw)) or {},
        )
        out = registry.assign("exp-c", context={"campaign_id": "camp-1"})
        assert out["ok"] is True
        rows = registry.assignments_tail(limit=10)
        assert len(rows) == 1 and rows[0]["experiment_id"] == "exp-c"
        assert emitted[0][0] == "experiment.assigned"
        assert emitted[0][1]["campaign_id"] == "camp-1"
        assert emitted[0][1]["variant_arm_id"].startswith("exp::exp-c::")

    def test_assign_unknown_or_inactive_refuses(self):
        from backend.experiments import registry

        assert registry.assign("missing")["ok"] is False
        exp = registry.register_experiment(
            dimension="offer",
            arms=["a", "b"],
            experiment_id="exp-p",
        )
        exp.status = "paused"
        registry.save_experiment(exp)
        assert registry.assign("exp-p")["ok"] is False

    def test_record_result_and_arm_stats(self):
        from backend.experiments import registry

        registry.register_experiment(
            dimension="landing",
            arms=["v1", "v2"],
            experiment_id="exp-l",
        )
        registry.record_result("exp-l", "v1", 1.0, won=True)
        registry.record_result("exp-l", "v1", 0.0)
        stats = registry.arm_stats("exp-l")
        assert stats["v1"]["trials"] == 2
        assert stats["v1"]["wins"] == 1
        assert stats["v2"]["trials"] == 0


# ---------------------------------------------------------------------------
# promoter
# ---------------------------------------------------------------------------
class TestPromoter:
    def _experiment(self):
        from backend.experiments import registry

        return registry.register_experiment(
            dimension="opener",
            arms=["winner", "loser"],
            experiment_id="exp-promote",
            min_trials=30,
        )

    def test_winner_promoted_and_loser_archived(self, monkeypatch):
        from backend.experiments import promoter, registry

        self._experiment()
        _seed_arm("exp-promote", "winner", trials=100, wins=60)
        _seed_arm("exp-promote", "loser", trials=100, wins=20)
        # deterministic replacement (no LLM in tests)
        monkeypatch.setattr(
            promoter,
            "_generate_replacement",
            lambda exp, arm: "fresh-variant",
        )
        summary = promoter.run_nightly_promotion()
        res = summary["experiments"]["exp-promote"]
        assert res["winner"] == "winner"
        assert res["archived"][0] == {"arm": "loser", "replacement": "fresh-variant"}
        exp = registry.get_experiment("exp-promote")
        assert exp.allocation_floors["winner"] == promoter.WINNER_ALLOCATION_FLOOR
        assert "loser" in exp.archived_arms
        assert "fresh-variant" in exp.arms  # cold arm entered
        # template default registered for the dimension
        assert promoter.template_default("opener")["arm"] == "winner"

    def test_below_min_trials_skips(self):
        from backend.experiments import promoter

        self._experiment()
        _seed_arm("exp-promote", "winner", trials=5, wins=4)
        _seed_arm("exp-promote", "loser", trials=5, wins=0)
        res = promoter.run_nightly_promotion()["experiments"]["exp-promote"]
        assert res["winner"] is None and res["archived"] == []

    def test_insignificant_difference_no_action(self):
        from backend.experiments import promoter, registry

        self._experiment()
        _seed_arm("exp-promote", "winner", trials=40, wins=12)
        _seed_arm("exp-promote", "loser", trials=40, wins=10)
        res = promoter.run_nightly_promotion()["experiments"]["exp-promote"]
        assert res["winner"] is None and res["archived"] == []
        assert registry.get_experiment("exp-promote").archived_arms == []

    def test_replacement_llm_fallback(self, monkeypatch):
        from backend.experiments import promoter, registry

        exp = registry.register_experiment(
            dimension="cta",
            arms=["a", "b"],
            experiment_id="exp-f",
        )

        def _deny(**kw):
            raise RuntimeError("budget denied")

        monkeypatch.setattr(
            "backend.common.llm_client.anthropic_messages",
            _deny,
        )
        replacement = promoter._generate_replacement(exp, "b")
        assert replacement.startswith("b-alt-")

    def test_campaign_stop_rule_halts_and_creates_task(self, tmp_path, monkeypatch):
        from backend.experiments import promoter

        monkeypatch.setenv(
            "SAMUS_CONVERSION_FUNNEL_PATH",
            str(tmp_path / "funnel.jsonl"),
        )
        from backend.common.conversion_funnel import record_stage

        for i in range(60):
            record_stage("lead", entity_id=f"l{i}")
        tasks: list = []
        monkeypatch.setattr(
            "backend.crm.service.create_operator_task",
            lambda req: tasks.append(req),
        )
        out = promoter.check_campaign_stop()
        assert out["halted"] is True
        assert promoter.campaign_halted() is True
        assert len(tasks) == 1 and tasks[0].source == "experiments_promoter"

    def test_campaign_stop_rule_needs_sample(self, tmp_path, monkeypatch):
        from backend.experiments import promoter

        monkeypatch.setenv(
            "SAMUS_CONVERSION_FUNNEL_PATH",
            str(tmp_path / "funnel.jsonl"),
        )
        from backend.common.conversion_funnel import record_stage

        for i in range(5):
            record_stage("lead", entity_id=f"l{i}")
        out = promoter.check_campaign_stop()
        assert out["halted"] is False
        assert promoter.campaign_halted() is False


# ---------------------------------------------------------------------------
# gateway routes
# ---------------------------------------------------------------------------
class TestRoutes:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SAMUS_SHARED_HMAC_KEY", "test-hmac-key")
        from backend.common.settings import reload_settings

        reload_settings()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend.experiments.routes import register_routes

        app = FastAPI()
        register_routes(app)
        return TestClient(app)

    def test_register_and_list(self, client):
        resp = client.post(
            "/admin/experiments",
            json={
                "dimension": "opener",
                "arms": ["a", "b"],
                "experiment_id": "exp-r1",
                "min_trials": 12,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["experiment"]["min_trials"] == 12

        listing = client.get("/admin/experiments")
        assert listing.status_code == 200
        body = listing.json()
        assert body["experiments"][0]["experiment_id"] == "exp-r1"
        assert "arm_stats" in body["experiments"][0]
        assert body["campaign_halted"] is False

    def test_register_validates(self, client):
        assert (
            client.post(
                "/admin/experiments",
                json={
                    "dimension": "bogus",
                    "arms": ["a", "b"],
                },
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/admin/experiments",
                json={
                    "dimension": "offer",
                    "arms": ["a"],
                },
            ).status_code
            == 400
        )
