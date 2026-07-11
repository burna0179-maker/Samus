"""Gateway voice transcript-ingest cadence — driver + end-to-end reward flow.

Covers :mod:`backend.gateway.voice_ingest_task`:
  * the enable gate + the "only run the pipeline on new data" guard, and
  * the ACCEPTANCE path — a completed Vapi call with a stamped variant arm,
    driven through the ingest, results in a persisted transcript analysis AND a
    ``record_outcome`` on that call's arm (bandit trial + reward move), with the
    transcript manifest freshened — within one cadence.

Fully offline: a fake Vapi client injects the call; the LM Studio analyzer is
stubbed to a canned outcome; every artifact/state/attribution write is isolated
to ``tmp_path``.
"""
from __future__ import annotations

import asyncio
import csv
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from backend.gateway import voice_ingest_task as vit
from backend.voice.models import VapiCall
from backend.prospecting.csv_export import CSV_COLUMNS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ARM_ID = "voice::ast_test::cfg-deadbeef"
_PROSPECT_ID = "p_bandit"
_CALL_NUMBER = "+15551230001"          # dialed number (E.164)
_CSV_PHONE = "5551230001"              # same number as it appears on the CSV
_CANNED_LLM = json.dumps({
    "outcome": "converted",
    "reward": 1.0,
    "objections_hit": [],
    "talking_points_landed": ["free audit hook"],
    "talking_points_flopped": [],
    "conversion_signals": ["send me the contract"],
    "what_to_listen_for": [],
    "prospect_tier_correction": None,
    "script_feedback": {},
})


class _FakeVapiClient:
    def __init__(self, calls: list) -> None:
        self._calls = calls

    def list_calls(self, *, limit: int = 10) -> list:
        return list(self._calls)


def _completed_call(call_id: str = "call_bandit_1") -> VapiCall:
    started = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    return VapiCall.model_validate({
        "id": call_id,
        "status": "ended",
        "endedReason": "customer-ended-call",
        "transcript": (
            "AI: Hi, this is Morgan with HustleForge — quick question about your "
            "website.\nUser: Sure. Actually that sounds great, send me the contract."
        ),
        "customer": {"number": _CALL_NUMBER, "name": "Rocklin Dental"},
        "startedAt": started,
    })


def _seed_call_list_csv(root) -> None:
    """Write today's call_list CSV so privacy_gate matches the prospect by phone."""
    d = root / "daily_calls"
    d.mkdir(parents=True, exist_ok=True)
    row = {c: "" for c in CSV_COLUMNS}
    row.update({
        "prospect_id": _PROSPECT_ID,
        "company_name": "Rocklin Dental",
        "phone": _CSV_PHONE,
        "state": "CA",
        "call_priority": "hot",
        "lead_score": "80",
        "seo_score": "30",
        "industry": "dentist",
    })
    path = d / f"call_list_{date.today().isoformat()}.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(CSV_COLUMNS))
        w.writeheader()
        w.writerow(row)


@pytest.fixture
def _loop_env(tmp_path, monkeypatch):
    """Enable the loop + isolate every write (artifacts, state, attribution, CRM)."""
    monkeypatch.setenv("SAMUS_VOICE_INGEST_LOOP_ENABLED", "1")
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("SAMUS_ATTRIBUTION_PATH", str(tmp_path / "attr.json"))
    monkeypatch.setenv("DDB_ATTRIBUTION_TABLE", "")
    monkeypatch.setenv("HF_DATA_DIR", str(tmp_path / "hfdata"))
    # Don't let the prospect index reach real host CSVs — keep it hermetic + fast.
    monkeypatch.setattr("backend.voice.prospect_lookup._FALLBACK_ROOTS", ())
    from backend.attribution import store as attr_store
    attr_store.reset_store()
    yield tmp_path
    attr_store.reset_store()


def _stub_llm(monkeypatch):
    """Stub the LM Studio analyzer (and the strategy reasoner that shares it)."""
    def _fake(*_a, **_k):
        return _CANNED_LLM
    monkeypatch.setattr("backend.voice.transcript_analyzer.llm_chat", _fake)
    monkeypatch.setattr("backend.voice.call_strategy_reasoner.llm_chat", _fake)


def _spy_record_outcome(monkeypatch):
    """Wrap attribution.record_outcome: capture args AND call through to the
    real (tmp-isolated) store so the bandit trial/reward really move."""
    import backend.attribution.engine as engine
    captured: list[dict] = []
    real = engine.record_outcome

    def _spy(arm_id, reward, *, won=False, now=None):
        captured.append({"arm_id": arm_id, "reward": reward, "won": won})
        return real(arm_id, reward, won=won, now=now)

    monkeypatch.setattr(engine, "record_outcome", _spy)
    return captured


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

def test_run_once_disabled_is_a_no_op(monkeypatch):
    monkeypatch.setenv("SAMUS_VOICE_INGEST_LOOP_ENABLED", "0")
    result = vit.run_once(client=_FakeVapiClient([_completed_call()]))
    assert result == {"ran": False, "reason": "disabled"}


def test_no_new_data_skips_the_pipeline(_loop_env, monkeypatch):
    # An empty pull -> staged 0 -> run_ingest_pipeline must NOT run (it would
    # otherwise burn an LLM strategy pass every idle cadence).
    calls_seen = {"n": 0}

    def _tripwire(*_a, **_k):
        calls_seen["n"] += 1
        return {}

    monkeypatch.setattr("backend.voice.ingest_pipeline.run_ingest_pipeline", _tripwire)

    result = vit.run_once(client=_FakeVapiClient([]))

    assert result["ran"] is True
    assert result["staged"] == 0
    assert result["analyzed"] == 0
    assert calls_seen["n"] == 0  # pipeline never invoked


# ---------------------------------------------------------------------------
# End-to-end: completed call -> analysis persisted + reward on the arm
# ---------------------------------------------------------------------------

def test_completed_call_flows_reward_to_stamped_arm(_loop_env, monkeypatch):
    root = _loop_env / "artifacts"
    _seed_call_list_csv(root)
    _stub_llm(monkeypatch)
    captured = _spy_record_outcome(monkeypatch)

    # FRONT of the loop: stamp the dispatch->arm mapping the dialer would write.
    from backend.voice import arm_stamp
    arm_stamp.record_dispatch(
        call_id="call_bandit_1", prospect_id=_PROSPECT_ID,
        phone=_CALL_NUMBER, arm_id=_ARM_ID,
    )
    # Sanity: the arm ledger round-trips under the isolated state root, else the
    # reward join below would silently no-op.
    assert arm_stamp.lookup_arm(prospect_id=_PROSPECT_ID) == _ARM_ID

    result = vit.run_once(client=_FakeVapiClient([_completed_call()]))

    # (i) the pipeline analyzed exactly the one new transcript
    assert result["ran"] is True
    assert result["staged"] == 1
    assert result["analyzed"] == 1

    # (ii) record_outcome fired once, crediting the RIGHT arm with the reward
    assert captured == [{"arm_id": _ARM_ID, "reward": 1.0, "won": True}]

    # (ii-b) the bandit's durable stats for that arm actually moved
    import backend.attribution.engine as engine
    snap = engine.snapshot(_ARM_ID)
    assert snap["trials"] == 1
    assert snap["wins"] == 1
    assert snap["mean_reward"] == pytest.approx(1.0)

    # (iii) a TranscriptAnalysis was persisted
    analyses = list((root / "voice" / "analyses").glob("*.json"))
    assert len(analyses) == 1
    persisted = json.loads(analyses[0].read_text(encoding="utf-8"))
    assert persisted["outcome"] == "converted"
    assert persisted["prospect_id"] == _PROSPECT_ID

    # (iv) the transcript manifest is freshened
    manifest = root / "voice" / "transcript_manifest.json"
    assert manifest.exists()
    assert json.loads(manifest.read_text(encoding="utf-8"))["source"] == \
        "vapi_in_container_ingest"


def test_reward_flows_exactly_once_across_cadences(_loop_env, monkeypatch):
    root = _loop_env / "artifacts"
    _seed_call_list_csv(root)
    _stub_llm(monkeypatch)
    captured = _spy_record_outcome(monkeypatch)

    from backend.voice import arm_stamp
    arm_stamp.record_dispatch(
        call_id="call_bandit_1", prospect_id=_PROSPECT_ID,
        phone=_CALL_NUMBER, arm_id=_ARM_ID,
    )

    client = _FakeVapiClient([_completed_call()])
    first = vit.run_once(client=client)
    second = vit.run_once(client=client)  # same call re-listed next cadence

    assert first["analyzed"] == 1
    assert second["staged"] == 0        # dedup'd by call_id -> not re-staged
    assert second["analyzed"] == 0
    # The bandit was credited exactly once for the call.
    assert len(captured) == 1
    import backend.attribution.engine as engine
    assert engine.snapshot(_ARM_ID)["trials"] == 1


# ---------------------------------------------------------------------------
# Lifespan hooks
# ---------------------------------------------------------------------------

def test_start_and_stop_loop_lifecycle(monkeypatch):
    monkeypatch.setenv("SAMUS_VOICE_INGEST_LOOP_ENABLED", "1")
    # A very long interval so the loop parks in its initial sleep and never ticks.
    monkeypatch.setenv("SAMUS_VOICE_INGEST_INTERVAL_SEC", "9999")

    class _App:
        class state:  # noqa: N801 — mimic FastAPI app.state attribute bag
            pass

    async def _run():
        app = _App()
        task = await vit.start_voice_ingest_loop(app)
        assert task is not None
        assert getattr(app.state, "voice_ingest_task", None) is task
        # Idempotent: a second start returns the same task.
        assert await vit.start_voice_ingest_loop(app) is task
        await vit.stop_voice_ingest_loop(app)
        assert app.state.voice_ingest_task is None
        assert task.cancelled() or task.done()

    asyncio.run(_run())


def test_start_loop_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("SAMUS_VOICE_INGEST_LOOP_ENABLED", "0")

    class _App:
        class state:  # noqa: N801
            pass

    async def _run():
        assert await vit.start_voice_ingest_loop(_App()) is None

    asyncio.run(_run())
