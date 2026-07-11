"""Tranche 3 — memory consolidation (distill/promote/calibrate/compress),
calibration override store, and the nightly timer scheduling math."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest


TODAY = date.today().isoformat()


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "jsonl")
    monkeypatch.setenv("SAMUS_ATTRIBUTION_PATH", str(tmp_path / "attr.json"))
    monkeypatch.setenv("SAMUS_CONVERSION_FUNNEL_PATH", str(tmp_path / "funnel.jsonl"))
    monkeypatch.setenv("SAMUS_REWARD_PERSIST_PATH", str(tmp_path / "rewards.jsonl"))
    monkeypatch.setenv("SAMUS_FEEDBACK_STORE_PATH", str(tmp_path / "metrics.json"))
    monkeypatch.setenv("SAMUS_CONSOLIDATION_LLM_ENABLED", "0")
    monkeypatch.delenv("DDB_ATTRIBUTION_TABLE", raising=False)
    from backend.attribution import store as attr_store

    attr_store.reset_store()
    yield
    attr_store.reset_store()


def _seed_funnel(*, leads=0, opportunities=0, closed_won=0, industry="plumbing"):
    from backend.common.conversion_funnel import record_stage

    for i in range(leads):
        record_stage("lead", entity_id=f"l{i}", industry=industry)
    for i in range(opportunities):
        record_stage("opportunity", entity_id=f"o{i}", industry=industry)
    for i in range(closed_won):
        record_stage("closed_won", entity_id=f"w{i}", industry=industry)


def _seed_rewards(tmp_path, count=3, *, paid=1, day=TODAY):
    rows = []
    for i in range(count):
        rows.append(
            {
                "opportunity_id": f"opp-{i}",
                "reward": 4.0,
                "components": {"terminal_paid": 1.0 if i < paid else 0.0},
                "computed_at": f"{day}T10:0{i}:00+00:00",
                "correlation_id": "",
            }
        )
    path = tmp_path / "rewards.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# calibration store
# ---------------------------------------------------------------------------
class TestCalibrationStore:
    def test_roundtrip_and_overrides(self):
        from backend.common import calibration

        assert calibration.read_calibration() == {}
        assert calibration.calibrated_tier_close_probability("hot", 0.35) == 0.35
        ok = calibration.write_calibration(
            tier_close_probability={"hot": 0.5, "low": 0.02},
            optimizer_seeds={"conversion_prob_default": 0.12},
            samples={"opportunities": 40},
            day=TODAY,
        )
        assert ok is True
        assert calibration.calibrated_tier_close_probability("hot", 0.35) == 0.5
        assert calibration.calibrated_tier_close_probability("warm", 0.15) == 0.15
        assert calibration.calibrated_optimizer_seed("conversion_prob_default", 0.1) == 0.12
        assert calibration.calibrated_optimizer_seed("unknown", 0.3) == 0.3

    def test_malformed_store_fails_open(self, tmp_path, monkeypatch):
        from backend.common import calibration

        bad = tmp_path / "cal.json"
        bad.write_text("{not json", encoding="utf-8")
        monkeypatch.setenv(calibration.ENV_CALIBRATION_PATH, str(bad))
        assert calibration.read_calibration() == {}
        assert calibration.calibrated_tier_close_probability("hot", 0.35) == 0.35


# ---------------------------------------------------------------------------
# distill
# ---------------------------------------------------------------------------
class TestDistill:
    def test_lessons_written_accepted_and_reach_reason_context(self, tmp_path):
        from backend.cognitive import consolidator

        _seed_funnel(leads=10, opportunities=6, closed_won=3)
        _seed_rewards(tmp_path)
        out = consolidator.distill(TODAY)
        assert out["lessons"] >= 2
        assert out["accepted"] == out["ingested"] >= 2

        # The REASON-stage seam: a distilled lesson must appear in the
        # active guidance context the cognitive runner injects.
        from backend.cognitive.guidance import GuidanceLedger
        from backend.cognitive.intelligence_cycle import active_guidance_context

        ctx = active_guidance_context(ledger=GuidanceLedger())
        assert "Active Strategic Guidance" in ctx
        assert "pattern:" in ctx

    def test_distilled_records_carry_provenance(self, tmp_path):
        from backend.cognitive import consolidator
        from backend.cognitive.guidance import GuidanceLedger

        _seed_funnel(opportunities=4, closed_won=2)
        consolidator.distill(TODAY)
        recs = GuidanceLedger().all_latest()
        assert recs
        assert all(r.briefing_id == f"distilled-{TODAY}" for r in recs)
        assert all(r.source_question == "distilled" for r in recs)
        assert any("provenance:" in r.rationale for r in recs)
        assert all(r.status == "accepted" for r in recs)

    def test_empty_day_is_noop(self):
        from backend.cognitive import consolidator

        out = consolidator.distill(TODAY)
        assert out == {"lessons": 0, "ingested": 0}

    def test_llm_rephrase_fallback(self, tmp_path, monkeypatch):
        from backend.cognitive import consolidator

        monkeypatch.setenv("SAMUS_CONSOLIDATION_LLM_ENABLED", "1")

        def _deny(**kw):
            raise RuntimeError("budget denied")

        monkeypatch.setattr("backend.common.llm_client.anthropic_messages", _deny)
        lessons = [{"recommendation": "pattern: x", "rationale": "provenance: y"}]
        out = consolidator._maybe_rephrase([dict(l) for l in lessons])
        assert out[0]["recommendation"] == "pattern: x"  # deterministic kept


# ---------------------------------------------------------------------------
# calibrate
# ---------------------------------------------------------------------------
class TestCalibrate:
    def test_sufficient_sample_writes_overrides(self):
        from backend.cognitive import consolidator
        from backend.common.calibration import read_calibration

        _seed_funnel(opportunities=30, closed_won=9)  # observed 0.30 vs warm 0.15
        out = consolidator.calibrate(TODAY)
        assert out["written"] is True
        assert abs(out["factor"] - 2.0) < 1e-6
        doc = read_calibration()
        assert doc["tier_close_probability"]["warm"] == pytest.approx(0.30)
        assert doc["tier_close_probability"]["hot"] == pytest.approx(0.70)
        assert doc["optimizer_seeds"]["conversion_prob_default"] == pytest.approx(0.30)

    def test_small_sample_writes_nothing(self):
        from backend.cognitive import consolidator
        from backend.common.calibration import read_calibration

        _seed_funnel(opportunities=3, closed_won=1)
        out = consolidator.calibrate(TODAY)
        assert out["written"] is False
        assert read_calibration() == {}

    def test_factor_clamped(self):
        from backend.cognitive import consolidator

        _seed_funnel(opportunities=40, closed_won=40)  # observed 1.0 -> raw 6.67x
        out = consolidator.calibrate(TODAY)
        assert out["factor"] == consolidator._CALIBRATION_FACTOR_MAX


# ---------------------------------------------------------------------------
# compress
# ---------------------------------------------------------------------------
class TestCompress:
    def test_old_rows_rotate_to_archive(self, tmp_path, monkeypatch):
        from backend.cognitive import consolidator

        monkeypatch.setenv("SAMUS_CONSOLIDATION_RETENTION_DAYS", "30")
        old_ts = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        funnel = tmp_path / "funnel.jsonl"
        funnel.write_text(
            json.dumps({"ts": old_ts, "stage": "lead", "entity_id": "old"})
            + "\n"
            + json.dumps(
                {"ts": datetime.now(timezone.utc).isoformat(), "stage": "lead", "entity_id": "new"}
            )
            + "\n",
            encoding="utf-8",
        )
        out = consolidator.compress()
        assert out["rotated"]["conversion_funnel"] == 1
        archive = tmp_path / "funnel.archive.jsonl"
        assert archive.exists()
        assert "old" in archive.read_text(encoding="utf-8")
        assert "new" in funnel.read_text(encoding="utf-8")

    def test_missing_ledgers_are_zero(self):
        from backend.cognitive import consolidator

        out = consolidator.compress()
        assert set(out["rotated"].values()) <= {0}


# ---------------------------------------------------------------------------
# full run + timer math
# ---------------------------------------------------------------------------
class TestRunConsolidation:
    def test_all_stages_run_and_never_raise(self, tmp_path, monkeypatch):
        from backend.cognitive import consolidator

        _seed_funnel(opportunities=25, closed_won=5)
        _seed_rewards(tmp_path)
        out = consolidator.run_consolidation()
        assert set(out["stages"]) == {
            "distill",
            "promote",
            "calibrate",
            "compress",
            "redteam",
            "hypothesize",
        }
        assert out["ok"] is True

    def test_stage_fault_is_contained(self, monkeypatch):
        from backend.cognitive import consolidator

        def _boom(day):
            raise RuntimeError("distill exploded")

        monkeypatch.setattr(consolidator, "distill", _boom)
        out = consolidator.run_consolidation()
        assert out["ok"] is False
        assert "distill exploded" in out["stages"]["distill"]["error"]
        assert "error" not in out["stages"]["compress"]

    def test_cli_main(self, capsys):
        from backend.cognitive import consolidator

        rc = consolidator.main(["--day", TODAY])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["day"] == TODAY


class TestConsolidationTimer:
    def test_seconds_until_next_fire_before_hour(self, monkeypatch):
        from backend.cognitive import consolidation_task as ct

        monkeypatch.setenv(ct.ENV_HOUR, "2")
        now = datetime(2026, 7, 5, 0, 0, 0)
        assert ct.seconds_until_next_fire(now) == pytest.approx(2 * 3600)

    def test_seconds_until_next_fire_after_hour_rolls_over(self, monkeypatch):
        from backend.cognitive import consolidation_task as ct

        monkeypatch.setenv(ct.ENV_HOUR, "2")
        now = datetime(2026, 7, 5, 3, 0, 0)
        assert ct.seconds_until_next_fire(now) == pytest.approx(23 * 3600)

    def test_bad_hour_falls_back(self, monkeypatch):
        from backend.cognitive import consolidation_task as ct

        monkeypatch.setenv(ct.ENV_HOUR, "99")
        assert ct._fire_hour() == 2

    def test_start_stop_loop(self):
        import asyncio
        from types import SimpleNamespace
        from backend.cognitive import consolidation_task as ct

        async def scenario():
            app = SimpleNamespace(state=SimpleNamespace())
            task = await ct.start_consolidation_loop(app)
            assert task is not None
            # idempotent
            assert await ct.start_consolidation_loop(app) is task
            await ct.stop_consolidation_loop(app)
            assert app.state.consolidation_task is None

        asyncio.run(scenario())

    def test_disabled_loop(self, monkeypatch):
        import asyncio
        from types import SimpleNamespace
        from backend.cognitive import consolidation_task as ct

        monkeypatch.setenv(ct.ENV_ENABLED, "0")

        async def scenario():
            app = SimpleNamespace(state=SimpleNamespace())
            assert await ct.start_consolidation_loop(app) is None

        asyncio.run(scenario())


# ---------------------------------------------------------------------------
# expire_stale_guidance — SAFE triage-drain for never-triaged recommendations
# ---------------------------------------------------------------------------
def _seed_guidance(recommendation, *, status, age_days, rid):
    """Append one guidance rec at a controlled age + status. Returns its id."""
    from datetime import date as _date, timedelta as _td
    from backend.cognitive.guidance import GuidanceLedger
    from backend.cognitive.guidance_models import GuidanceRecord

    ts = f"{(_date.today() - _td(days=age_days)).isoformat()}T09:00:00+00:00"
    rec = GuidanceRecord(
        recommendation_id=rid,
        briefing_id="daystart-test",
        ts=ts,
        updated_ts=ts,
        recommendation=recommendation,
        status=status,
    )
    GuidanceLedger().append(rec)
    return rid


class TestExpireStaleGuidance:
    def test_disabled_by_default_is_noop(self, monkeypatch):
        """Unset SAMUS_GUIDANCE_STALE_DAYS -> pure no-op (wire-not-arm)."""
        from backend.cognitive import consolidator
        from backend.cognitive.guidance import GuidanceLedger
        from backend.cognitive.guidance_models import GuidanceStatus

        monkeypatch.delenv(consolidator.ENV_GUIDANCE_STALE_DAYS, raising=False)
        _seed_guidance("old proposed rec", status="proposed", age_days=90, rid="g-old")

        out = consolidator.expire_stale_guidance(TODAY)
        assert out == {"enabled": False, "stale_days": 0, "abandoned": 0}
        # rec is untouched — still proposed
        rec = GuidanceLedger().get("g-old")
        assert rec.status == GuidanceStatus.PROPOSED.value

    def test_abandons_only_stale_proposed(self, monkeypatch):
        from backend.cognitive import consolidator
        from backend.cognitive.guidance import GuidanceLedger
        from backend.cognitive.guidance_models import GuidanceStatus

        monkeypatch.setenv(consolidator.ENV_GUIDANCE_STALE_DAYS, "14")
        _seed_guidance("stale proposed", status="proposed", age_days=20, rid="g-stale")
        _seed_guidance("fresh proposed", status="proposed", age_days=2, rid="g-fresh")
        _seed_guidance("stale accepted", status="accepted", age_days=40, rid="g-acc")

        out = consolidator.expire_stale_guidance(TODAY)
        assert out["enabled"] is True
        assert out["abandoned"] == 1

        led = GuidanceLedger()
        stale = led.get("g-stale")
        assert stale.status == GuidanceStatus.ABANDONED.value
        assert stale.outcome == "abandoned: expired: never triaged"
        # fresh proposed + stale accepted are left alone
        assert led.get("g-fresh").status == GuidanceStatus.PROPOSED.value
        assert led.get("g-acc").status == GuidanceStatus.ACCEPTED.value
        # drained from the open backlog
        assert "g-stale" not in {r.recommendation_id for r in led.open_items()}

    def test_boundary_exactly_n_days_is_abandoned(self, monkeypatch):
        """A rec exactly N days old is at/over the cutoff -> abandoned."""
        from backend.cognitive import consolidator
        from backend.cognitive.guidance import GuidanceLedger
        from backend.cognitive.guidance_models import GuidanceStatus

        monkeypatch.setenv(consolidator.ENV_GUIDANCE_STALE_DAYS, "14")
        _seed_guidance("exactly 14d", status="proposed", age_days=14, rid="g-14")
        _seed_guidance("13d young", status="proposed", age_days=13, rid="g-13")

        consolidator.expire_stale_guidance(TODAY)
        led = GuidanceLedger()
        assert led.get("g-14").status == GuidanceStatus.ABANDONED.value
        assert led.get("g-13").status == GuidanceStatus.PROPOSED.value

    def test_wired_into_run_consolidation(self, monkeypatch):
        """The stage runs as part of the nightly pipeline."""
        from backend.cognitive import consolidator

        monkeypatch.setenv(consolidator.ENV_GUIDANCE_STALE_DAYS, "7")
        _seed_guidance("old proposed", status="proposed", age_days=30, rid="g-run")

        result = consolidator.run_consolidation(TODAY)
        stage = result["stages"]["expire_guidance"]
        assert stage["enabled"] is True
        assert stage["abandoned"] == 1
