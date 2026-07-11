"""Tests for backend.gateway.transcript_ingest_task — close the bandit learning loop.

Two layers:

  * Plumbing — env flags, manifest update, dedup by hash.
  * Integration — the acceptance criterion: a fake completed Vapi call with a
    stamped arm flows through ``run_ingest_pass`` and produces (i) a persisted
    TranscriptAnalysis, (ii) a ``record_outcome`` call with the correct arm_id
    + reward, (iii) a freshened transcript_manifest.json.

No network — Vapi fetch is injected; LM Studio is monkeypatched to return a
canned JSON response.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.gateway import transcript_ingest_task as tit


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (tit.ENV_ENABLED, tit.ENV_INTERVAL, tit.ENV_MAX_PER_PASS):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def artifact_root(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path / "state"))
    return tmp_path


# A completed Vapi call object (the shape returned by GET /call/{id}).
_VAPI_CALL = {
    "id": "call_abc123",
    "status": "ended",
    "endedReason": "assistant-said-end-call-phrase",
    "startedAt": "2026-07-07T15:00:00Z",
    "endedAt": "2026-07-07T15:02:30Z",
    "transcript": (
        "[0:00] Morgan: Hi, this is Morgan from HustleForge. "
        "Is this the owner of Acme Plumbing?\n"
        "[0:05] Customer: Yeah, that's me.\n"
        "[0:08] Morgan: Great! I noticed your Google listing "
        "could use some attention. We help local businesses...\n"
        "[0:25] Customer: Actually, I've been meaning to look into that. "
        "Can you send me some info?\n"
        "[0:30] Morgan: Absolutely! I'll send that right over."
    ),
    "customer": {"number": "+15305551234"},
    "analysis": {"summary": "Prospect interested, requested info"},
    "cost": 0.12,
}

# The canned LLM response that analyze_transcript will parse.
_LLM_RESPONSE = json.dumps({
    "outcome": "warm_lead",
    "reward": 0.6,
    "objections_hit": [],
    "talking_points_landed": ["Google listing attention"],
    "talking_points_flopped": [],
    "conversion_signals": ["Can you send me some info?"],
    "what_to_listen_for": ["owner picks up directly"],
    "prospect_tier_correction": None,
    "script_feedback": {"opener": None, "voicemail": None,
                        "pitch": None, "objection_handlers": None},
})


def _write_voice_event(root: Path, call_id: str, **extra) -> None:
    """Append one end_of_call event to voice_events.jsonl."""
    from backend.common.dates import iso_now
    events_path = root / "voice" / "voice_events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": iso_now(),
        "kind": "end_of_call",
        "call_id": call_id,
        "outcome": "completed_conversation",
        "prospect_id": "p_001",
        "company": "Acme Plumbing",
        "phone": "+15305551234",
        **extra,
    }
    with events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def _write_dial_run(root: Path, call_id: str, **extra) -> None:
    """Write a dial_run ledger with one initiated attempt."""
    runs_dir = root / "voice" / "dial_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    # Production names dial-run files from an iso_now() (UTC) run_id, so bucket
    # by the UTC date here too — matching how the ingest task discovers them.
    today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    run_file = runs_dir / f"dial_run_{today_str}_test.json"
    run = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "attempts": [{
            "call_id": call_id,
            "outcome": "initiated",
            "prospect_id": "p_001",
            "company": "Acme Plumbing",
            "phone": "+15305551234",
            **extra,
        }],
    }
    run_file.write_text(json.dumps(run), encoding="utf-8")


def _stamp_arm(root: Path, call_id: str, arm_id: str, monkeypatch) -> None:
    """Write an arm dispatch via the real arm_stamp module."""
    from backend.voice.arm_stamp import record_dispatch
    record_dispatch(
        call_id=call_id,
        prospect_id="p_001",
        phone="+15305551234",
        arm_id=arm_id,
    )


def _fake_fetch(vapi_calls: dict[str, dict]):
    """Build a fetch_call callable from a {call_id: call_obj} map."""
    def _fetch(call_id: str):
        return vapi_calls.get(call_id)
    return _fetch


# ---------------------------------------------------------------------------
# Env / flag tests
# ---------------------------------------------------------------------------

def test_loop_disabled_by_env(monkeypatch):
    monkeypatch.setenv(tit.ENV_ENABLED, "0")
    assert tit._flag_on(tit.ENV_ENABLED) is False


def test_loop_enabled_by_default(monkeypatch):
    monkeypatch.delenv(tit.ENV_ENABLED, raising=False)
    assert tit._flag_on(tit.ENV_ENABLED) is True


def test_max_per_pass_env(monkeypatch):
    monkeypatch.setenv(tit.ENV_MAX_PER_PASS, "5")
    assert tit._int_env(tit.ENV_MAX_PER_PASS, 20) == 5


# ---------------------------------------------------------------------------
# Manifest update
# ---------------------------------------------------------------------------

def test_manifest_freshens(artifact_root):
    tit._update_manifest()
    manifest = artifact_root / "voice" / "transcript_manifest.json"
    assert manifest.exists()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert "last_ingest_pass" in data
    assert data["source"] == "transcript_ingest_task"


# ---------------------------------------------------------------------------
# Dedup — same transcript not analyzed twice
# ---------------------------------------------------------------------------

def test_dedup_by_file_hash(artifact_root, monkeypatch):
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH",
                       str(artifact_root / "voice" / "voice_events.jsonl"))

    _write_voice_event(artifact_root, "call_abc123")

    # Pre-persist an analysis with the same hash
    file_hash = tit._vapi_file_hash("call_abc123", _VAPI_CALL["transcript"])
    analyses_dir = artifact_root / "voice" / "analyses"
    analyses_dir.mkdir(parents=True, exist_ok=True)
    (analyses_dir / f"{file_hash}.json").write_text("{}", encoding="utf-8")

    result = tit.run_ingest_pass(
        fetch_call=_fake_fetch({"call_abc123": _VAPI_CALL}),
        skip_reconcile=True,
    )
    assert result["skipped_already_analyzed"] >= 1
    assert result["analyzed"] == 0


# ---------------------------------------------------------------------------
# Skips — not ended, no transcript
# ---------------------------------------------------------------------------

def test_skip_call_not_ended(artifact_root, monkeypatch):
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH",
                       str(artifact_root / "voice" / "voice_events.jsonl"))
    _write_voice_event(artifact_root, "call_inprogress")

    in_progress = {**_VAPI_CALL, "id": "call_inprogress", "status": "queued"}
    result = tit.run_ingest_pass(
        fetch_call=_fake_fetch({"call_inprogress": in_progress}),
        skip_reconcile=True,
    )
    assert result["skipped_not_ended"] == 1
    assert result["analyzed"] == 0


def test_skip_empty_transcript(artifact_root, monkeypatch):
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH",
                       str(artifact_root / "voice" / "voice_events.jsonl"))
    _write_voice_event(artifact_root, "call_notx")

    no_transcript = {**_VAPI_CALL, "id": "call_notx", "transcript": ""}
    result = tit.run_ingest_pass(
        fetch_call=_fake_fetch({"call_notx": no_transcript}),
        skip_reconcile=True,
    )
    assert result["skipped_no_transcript"] == 1


# ---------------------------------------------------------------------------
# Fetch failure
# ---------------------------------------------------------------------------

def test_fetch_failure_counted(artifact_root, monkeypatch):
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH",
                       str(artifact_root / "voice" / "voice_events.jsonl"))
    _write_voice_event(artifact_root, "call_gone")

    result = tit.run_ingest_pass(
        fetch_call=lambda cid: None,
        skip_reconcile=True,
    )
    assert result["fetch_failed"] == 1


# ---------------------------------------------------------------------------
# No candidates — pass runs cleanly
# ---------------------------------------------------------------------------

def test_no_candidates(artifact_root, monkeypatch):
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH",
                       str(artifact_root / "voice" / "voice_events.jsonl"))
    # empty events file
    events = artifact_root / "voice" / "voice_events.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text("", encoding="utf-8")

    result = tit.run_ingest_pass(
        fetch_call=lambda cid: None,
        skip_reconcile=True,
    )
    assert result["candidates"] == 0
    assert result["analyzed"] == 0
    # manifest still freshened
    manifest = artifact_root / "voice" / "transcript_manifest.json"
    assert manifest.exists()


# ---------------------------------------------------------------------------
# ACCEPTANCE: end-to-end — arm stamp -> ingest -> record_outcome
# ---------------------------------------------------------------------------

def test_e2e_arm_stamp_through_ingest_to_record_outcome(artifact_root, monkeypatch):
    """THE headline test. A completed Vapi call whose arm was stamped at dial
    time flows through run_ingest_pass and produces:
      (i)   a persisted TranscriptAnalysis
      (ii)  a record_outcome call with the correct arm_id + reward
      (iii) a freshened transcript_manifest.json
    """
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH",
                       str(artifact_root / "voice" / "voice_events.jsonl"))

    arm_id = "voice::ast_test::cfg-deadbeef"
    call_id = "call_abc123"

    # 1. Stamp the arm (simulates dialer.py at dial time)
    _stamp_arm(artifact_root, call_id, arm_id, monkeypatch)

    # 2. Write the end_of_call event (simulates webhook or reconcile)
    _write_voice_event(artifact_root, call_id)

    # 3. Monkeypatch LM Studio to return canned analysis
    import backend.voice.transcript_analyzer as _ta
    monkeypatch.setattr(_ta, "llm_chat", lambda *a, **kw: _LLM_RESPONSE)

    # 4. Monkeypatch record_outcome to capture the call
    # Patch in BOTH the engine module (canonical) and the transcript_analyzer
    # module (where _flow_reward_to_bandit does a deferred import).
    recorded = []

    def _capture_record_outcome(arm, reward, *, won=False):
        recorded.append({"arm_id": arm, "reward": reward, "won": won})

    import backend.attribution.engine as _ae
    monkeypatch.setattr(_ae, "record_outcome", _capture_record_outcome)

    # 5. Run the ingest pass
    result = tit.run_ingest_pass(
        fetch_call=_fake_fetch({call_id: _VAPI_CALL}),
        skip_reconcile=True,
    )

    # (i) Analysis persisted
    assert result["analyzed"] == 1, f"unexpected result: {result}"
    analyses_dir = artifact_root / "voice" / "analyses"
    analysis_files = list(analyses_dir.glob("*.json"))
    assert len(analysis_files) >= 1, "no analysis file persisted"

    persisted = json.loads(analysis_files[0].read_text(encoding="utf-8"))
    assert persisted["outcome"] == "warm_lead"
    assert persisted["reward"] == 0.6
    assert persisted["source_file"] == f"vapi_{call_id}"

    # (ii) record_outcome called with correct arm_id + reward
    assert len(recorded) >= 1, f"record_outcome not called; result={result}"
    assert recorded[0]["arm_id"] == arm_id
    assert recorded[0]["reward"] == 0.6
    assert recorded[0]["won"] is False

    # (iii) Manifest freshened
    manifest = artifact_root / "voice" / "transcript_manifest.json"
    assert manifest.exists()
    mdata = json.loads(manifest.read_text(encoding="utf-8"))
    assert "last_ingest_pass" in mdata


def test_e2e_converted_call_records_won(artifact_root, monkeypatch):
    """A converted call flows won=True to record_outcome."""
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH",
                       str(artifact_root / "voice" / "voice_events.jsonl"))

    arm_id = "voice::ast_test::cfg-12345678"
    call_id = "call_converted"
    _stamp_arm(artifact_root, call_id, arm_id, monkeypatch)
    _write_voice_event(artifact_root, call_id)

    converted_llm = json.dumps({
        "outcome": "converted",
        "reward": 1.0,
        "objections_hit": [],
        "talking_points_landed": [],
        "talking_points_flopped": [],
        "conversion_signals": ["Let's do it"],
        "what_to_listen_for": [],
        "prospect_tier_correction": "hot",
        "script_feedback": {},
    })
    import backend.voice.transcript_analyzer as _ta
    monkeypatch.setattr(_ta, "llm_chat", lambda *a, **kw: converted_llm)

    recorded = []
    import backend.attribution.engine as _ae
    monkeypatch.setattr(
        _ae, "record_outcome",
        lambda arm, reward, *, won=False: recorded.append(
            {"arm_id": arm, "reward": reward, "won": won}
        ),
    )

    converted_call = {**_VAPI_CALL, "id": call_id}
    result = tit.run_ingest_pass(
        fetch_call=_fake_fetch({call_id: converted_call}),
        skip_reconcile=True,
    )

    assert result["analyzed"] == 1
    assert len(recorded) >= 1
    assert recorded[0]["arm_id"] == arm_id
    assert recorded[0]["reward"] == 1.0
    assert recorded[0]["won"] is True


def test_e2e_from_dial_run_ledger(artifact_root, monkeypatch):
    """Calls discovered from dial_run ledgers (not just voice_events) are
    analyzed, so the ingest cadence covers both webhook-sourced and
    dialer-sourced call ids."""
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH",
                       str(artifact_root / "voice" / "voice_events.jsonl"))
    # Create empty events file so the reader doesn't fail
    events = artifact_root / "voice" / "voice_events.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text("", encoding="utf-8")

    call_id = "call_from_dialer"
    arm_id = "voice::ast_x::cfg-abcdef01"
    _stamp_arm(artifact_root, call_id, arm_id, monkeypatch)
    _write_dial_run(artifact_root, call_id)

    # Patch the analyzer's bound ``llm_chat`` symbol — it did
    # ``from backend.common.local_llm import chat as llm_chat`` at import, so
    # patching ``backend.common.local_llm.chat`` here would NOT intercept it and
    # the test would make a real, network-dependent LLM call (the flake source).
    # Match how the sibling e2e tests patch it.
    import backend.voice.transcript_analyzer as _ta
    monkeypatch.setattr(_ta, "llm_chat", lambda *a, **kw: _LLM_RESPONSE)

    recorded = []
    import backend.attribution.engine as _ae
    monkeypatch.setattr(
        _ae, "record_outcome",
        lambda arm, reward, *, won=False: recorded.append(
            {"arm_id": arm, "reward": reward, "won": won}
        ),
    )

    dial_call = {**_VAPI_CALL, "id": call_id}
    result = tit.run_ingest_pass(
        fetch_call=_fake_fetch({call_id: dial_call}),
        skip_reconcile=True,
    )

    assert result["analyzed"] == 1
    assert len(recorded) >= 1
    assert recorded[0]["arm_id"] == arm_id


# ---------------------------------------------------------------------------
# Max-per-pass cap
# ---------------------------------------------------------------------------

def test_max_per_pass_caps_analysis(artifact_root, monkeypatch):
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH",
                       str(artifact_root / "voice" / "voice_events.jsonl"))
    monkeypatch.setenv(tit.ENV_MAX_PER_PASS, "1")

    _write_voice_event(artifact_root, "call_1")
    _write_voice_event(artifact_root, "call_2")

    monkeypatch.setattr("backend.common.local_llm.chat", lambda *a, **kw: _LLM_RESPONSE)
    import backend.attribution.engine as _ae
    monkeypatch.setattr(_ae, "record_outcome", lambda *a, **kw: None)

    calls = {
        "call_1": {**_VAPI_CALL, "id": "call_1"},
        "call_2": {**_VAPI_CALL, "id": "call_2"},
    }
    result = tit.run_ingest_pass(
        fetch_call=_fake_fetch(calls),
        skip_reconcile=True,
    )
    # Only 1 analyzed due to cap
    assert result["analyzed"] <= 1


# ---------------------------------------------------------------------------
# Asyncio loop lifecycle
# ---------------------------------------------------------------------------

def test_start_stop_lifecycle():
    """Start and stop the loop without errors."""
    import asyncio

    class _FakeApp:
        class state:
            transcript_ingest_task = None

    app = _FakeApp()

    async def _run():
        task = await tit.start_transcript_ingest_loop(app)
        assert task is not None
        assert not task.done()
        await tit.stop_transcript_ingest_loop(app)
        assert app.state.transcript_ingest_task is None

    asyncio.run(_run())


def test_start_disabled(monkeypatch):
    import asyncio

    monkeypatch.setenv(tit.ENV_ENABLED, "0")

    class _FakeApp:
        class state:
            transcript_ingest_task = None

    async def _run():
        task = await tit.start_transcript_ingest_loop(_FakeApp())
        assert task is None

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Vapi timestamp parsing
# ---------------------------------------------------------------------------

def test_parse_vapi_ts_valid():
    dt = tit._parse_vapi_ts("2026-07-07T15:00:00Z")
    assert dt.year == 2026
    assert dt.month == 7
    assert dt.hour == 15


def test_parse_vapi_ts_invalid():
    dt = tit._parse_vapi_ts("not-a-date")
    assert isinstance(dt, datetime)


def test_parse_vapi_ts_empty():
    dt = tit._parse_vapi_ts("")
    assert isinstance(dt, datetime)


# ---------------------------------------------------------------------------
# File hash determinism
# ---------------------------------------------------------------------------

def test_vapi_file_hash_deterministic():
    h1 = tit._vapi_file_hash("call_1", "hello world")
    h2 = tit._vapi_file_hash("call_1", "hello world")
    h3 = tit._vapi_file_hash("call_2", "hello world")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 16
