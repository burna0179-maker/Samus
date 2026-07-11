"""Tranche 3 — close the reward loop (automatic learning).

Covers:
  * the business-events shim (no-op when the sibling module is absent,
    passthrough when present, fail-soft when the module errors);
  * the CRM lifecycle terminal-transition -> compute_reward hook;
  * voice arm stamping (arm identity, dispatch ledger, lookup) and the
    transcript-analysis -> bandit reward flow;
  * the outreach interaction JSONL ledger + snapshot rebuild.
"""

from __future__ import annotations

import sys
import types

import pytest


# ---------------------------------------------------------------------------
# business_events_shim_t3
# ---------------------------------------------------------------------------
class TestBusinessEventsShim:
    """T3 shim delegation tests.

    Post-HOTL-T1 merge the shim resolves the real module per-call via
    ``sys.modules`` (so ``monkeypatch.setitem(sys.modules, ...)`` swaps it
    freely — same pattern the T1 shim tests use). Pre-merge these tests
    reached into the shim's module-level ``_be`` reference; that attribute
    no longer exists.
    """

    def test_noop_when_module_absent(self, monkeypatch):
        from backend.common import business_events_shim_t3 as shim

        # Force absence: point sys.modules at None so _real_module()'s
        # sys.modules.get() hit returns None AND then the import fallback
        # sees a poisoned sentinel. Simplest form: replace with a broken
        # module that raises AttributeError on emit/read (fail-soft path
        # returns {} / []).
        monkeypatch.setitem(sys.modules, "backend.common.business_events", None)
        # sys.modules[name] = None flips the finder to ImportError on
        # importlib.import_module — which is exactly the "absent" case.
        assert shim.events_available() is False
        assert shim.emit_business_event("decision.made", workcell="test") == {}
        assert shim.read_events(prospect_id="p1") == []

    def test_passthrough_when_module_present(self, monkeypatch):
        from backend.common import business_events_shim_t3 as shim

        calls: list[tuple] = []
        fake = types.ModuleType("backend.common.business_events")
        fake.emit_business_event = lambda et, **kw: calls.append((et, kw)) or {"event_type": et}
        fake.read_events = lambda **kw: [{"event_type": "experiment.assigned"}]
        monkeypatch.setitem(sys.modules, "backend.common.business_events", fake)
        out = shim.emit_business_event(
            "experiment.assigned",
            workcell="experiments",
            prospect_id="p1",
            variant_arm_id="exp::e1::a",
        )
        assert out == {"event_type": "experiment.assigned"}
        assert calls[0][0] == "experiment.assigned"
        assert calls[0][1]["workcell"] == "experiments"
        assert calls[0][1]["variant_arm_id"] == "exp::e1::a"
        assert shim.read_events(limit=5) == [{"event_type": "experiment.assigned"}]

    def test_failsoft_when_module_raises(self, monkeypatch):
        from backend.common import business_events_shim_t3 as shim

        def _boom(*a, **k):
            raise RuntimeError("stream down")

        fake = types.ModuleType("backend.common.business_events")
        fake.emit_business_event = _boom
        fake.read_events = _boom
        monkeypatch.setitem(sys.modules, "backend.common.business_events", fake)
        assert shim.emit_business_event("decision.made", workcell="x") == {}
        assert shim.read_events() == []


# ---------------------------------------------------------------------------
# CRM lifecycle terminal-transition reward hook
# ---------------------------------------------------------------------------
def _opp(stage: str = "closed_won"):
    from backend.crm.models import Opportunity

    return Opportunity(opportunity_id="opp-t3-1", prospect_id="p-1", stage=stage)


class TestLifecycleRewardHook:
    def test_terminal_transition_triggers_compute_reward(self, monkeypatch):
        from backend.crm import lifecycle

        seen: list[str] = []

        def fake_compute(opportunity_id, *, correlation_id=""):
            seen.append(opportunity_id)
            return types.SimpleNamespace(reward=1.0)

        monkeypatch.setattr(
            "backend.strategy.reward_density.compute_reward",
            fake_compute,
        )
        tasks = lifecycle.tasks_for_stage_advance(
            _opp("closed_won"),
            "proposal",
            "closed_won",
        )
        assert seen == ["opp-t3-1"]
        assert tasks  # deliver task still produced

    def test_closed_lost_also_triggers(self, monkeypatch):
        from backend.crm import lifecycle

        seen: list[str] = []
        monkeypatch.setattr(
            "backend.strategy.reward_density.compute_reward",
            lambda oid, **kw: seen.append(oid) or types.SimpleNamespace(reward=0.0),
        )
        lifecycle.tasks_for_stage_advance(_opp("closed_lost"), "proposal", "closed_lost")
        assert seen == ["opp-t3-1"]

    def test_non_terminal_does_not_trigger(self, monkeypatch):
        from backend.crm import lifecycle

        seen: list[str] = []
        monkeypatch.setattr(
            "backend.strategy.reward_density.compute_reward",
            lambda oid, **kw: seen.append(oid),
        )
        lifecycle.tasks_for_stage_advance(_opp("proposal"), "qualified", "proposal")
        assert seen == []

    def test_reward_failure_is_swallowed(self, monkeypatch):
        from backend.crm import lifecycle

        def _boom(oid, **kw):
            raise RuntimeError("codex veto")

        monkeypatch.setattr("backend.strategy.reward_density.compute_reward", _boom)
        tasks = lifecycle.tasks_for_stage_advance(
            _opp("closed_won"),
            "proposal",
            "closed_won",
        )
        assert tasks  # transition path unaffected

    def test_trigger_ignores_non_terminal_stage(self):
        from backend.crm import lifecycle

        assert lifecycle.trigger_terminal_reward("x", new_stage="proposal") is False


# ---------------------------------------------------------------------------
# Voice arm stamping + reward flow
# ---------------------------------------------------------------------------
@pytest.fixture
def state_root(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "jsonl")
    return tmp_path


class TestArmStamp:
    def test_arm_id_shape_and_pin(self, monkeypatch):
        from backend.voice import arm_stamp

        monkeypatch.setenv(arm_stamp.ENV_SCRIPT_VERSION, "v7")
        assert arm_stamp.current_arm_id("asst_123") == "voice::asst_123::v7"
        assert arm_stamp.current_arm_id("") == "voice::default::v7"

    def test_arm_id_hashes_config_when_unpinned(self, monkeypatch):
        from backend.voice import arm_stamp

        monkeypatch.delenv(arm_stamp.ENV_SCRIPT_VERSION, raising=False)
        arm = arm_stamp.current_arm_id("a")
        assert arm.startswith("voice::a::")
        version = arm.split("::")[2]
        assert version == "v0" or version.startswith("cfg-")

    def test_record_and_lookup_roundtrip(self, state_root):
        from backend.voice import arm_stamp

        ok = arm_stamp.record_dispatch(
            call_id="call-1",
            prospect_id="p-9",
            phone="+1 (530) 555-1234",
            arm_id="voice::a::v1",
        )
        assert ok is True
        assert arm_stamp.lookup_arm(prospect_id="p-9") == "voice::a::v1"
        assert arm_stamp.lookup_arm(phone="5551234") == "voice::a::v1"
        assert arm_stamp.lookup_arm(call_id="call-1") == "voice::a::v1"
        assert arm_stamp.lookup_arm(prospect_id="unknown") == ""

    def test_lookup_newest_wins(self, state_root):
        from backend.voice import arm_stamp

        arm_stamp.record_dispatch(call_id="c1", prospect_id="p", phone="", arm_id="voice::a::v1")
        arm_stamp.record_dispatch(call_id="c2", prospect_id="p", phone="", arm_id="voice::a::v2")
        assert arm_stamp.lookup_arm(prospect_id="p") == "voice::a::v2"

    def test_lookup_empty_inputs(self, state_root):
        from backend.voice import arm_stamp

        assert arm_stamp.lookup_arm() == ""


class TestTranscriptRewardFlow:
    def _analysis(self, **over):
        from backend.voice.transcript_analyzer import TranscriptAnalysis

        base = dict(
            source_file="t.txt",
            file_hash="h1",
            call_ts_iso="2026-07-05T00:00:00",
            direction="outbound",
            contact_phone="+15305551234",
            contact_name="Kelly",
            prospect_id="p-9",
            company_name="Acme",
            outcome="converted",
            reward=1.0,
        )
        base.update(over)
        return TranscriptAnalysis(**base)

    def test_reward_flows_to_bandit(self, state_root, monkeypatch):
        from backend.voice import arm_stamp, transcript_analyzer

        arm_stamp.record_dispatch(
            call_id="c1",
            prospect_id="p-9",
            phone="+15305551234",
            arm_id="voice::a::v1",
        )
        recorded: list[tuple] = []
        monkeypatch.setattr(
            "backend.attribution.engine.record_outcome",
            lambda arm, reward, won=False, **kw: recorded.append((arm, reward, won)),
        )
        arm = transcript_analyzer._flow_reward_to_bandit(self._analysis())
        assert arm == "voice::a::v1"
        assert recorded == [("voice::a::v1", 1.0, True)]

    def test_no_arm_is_noop(self, state_root, monkeypatch):
        from backend.voice import transcript_analyzer

        recorded: list = []
        monkeypatch.setattr(
            "backend.attribution.engine.record_outcome",
            lambda *a, **k: recorded.append(a),
        )
        arm = transcript_analyzer._flow_reward_to_bandit(
            self._analysis(prospect_id=None, contact_phone=""),
        )
        assert arm == ""
        assert recorded == []

    def test_flow_failure_is_swallowed(self, state_root, monkeypatch):
        from backend.voice import transcript_analyzer

        def _boom(**kw):
            raise RuntimeError("ledger down")

        monkeypatch.setattr("backend.voice.arm_stamp.lookup_arm", _boom)
        assert transcript_analyzer._flow_reward_to_bandit(self._analysis()) == ""


# ---------------------------------------------------------------------------
# Outreach interaction ledger
# ---------------------------------------------------------------------------
class TestOutreachInteractionLedger:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "jsonl")
        monkeypatch.setenv(
            "SAMUS_FEEDBACK_STORE_PATH",
            str(tmp_path / "metrics.json"),
        )
        from backend.outreach import metrics

        metrics.reset_metrics()
        yield
        metrics.reset_metrics()

    def test_log_appends_to_ledger(self, tmp_path):
        from backend.outreach import metrics

        metrics.log_interaction("p1", "closed", None, "seo_audit", "authority")
        ledger_file = tmp_path / "state" / "outreach" / "interaction_ledger.jsonl"
        assert ledger_file.exists()
        rows = metrics._ledger().tail(limit=10)
        assert len(rows) == 1
        assert rows[0]["outcome"] == "closed"
        assert rows[0]["product"] == "seo_audit"

    def test_rebuild_reconstructs_snapshot(self):
        from backend.outreach import metrics

        metrics.log_interaction("p1", "closed", None, "seo_audit", "authority")
        metrics.log_interaction("p2", "rejected", "too expensive", "seo_audit", "authority")
        before = metrics.snapshot()
        # wipe the folded counters, then rebuild from the ledger
        metrics.reset_metrics()
        assert metrics.snapshot()["closes"] == {}
        rebuilt = metrics.snapshot(rebuild=True)
        assert rebuilt == before
        assert rebuilt["closes"] == {"seo_audit": 1}
        assert rebuilt["objections"] == {"too expensive": 1}
        assert rebuilt["angles"]["authority"] == {"wins": 1, "losses": 1}

    def test_ledger_failure_does_not_break_logging(self, monkeypatch):
        from backend.outreach import metrics

        def _boom():
            raise OSError("disk full")

        monkeypatch.setattr(metrics, "_ledger", _boom)
        metrics.log_interaction("p1", "closed", None, "seo_audit", "authority")
        assert metrics.snapshot()["closes"] == {"seo_audit": 1}
        assert metrics.snapshot(rebuild=True)["closes"] == {"seo_audit": 1}
