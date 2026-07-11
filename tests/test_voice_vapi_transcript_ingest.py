"""Vapi transcript-ingest source adapter — staging, dedup, manifest, round-trip.

Unit-level coverage for :mod:`backend.voice.vapi_transcript_ingest`: it pulls
recently-completed Vapi calls (read-only) and stages their transcripts as ``.txt``
into the SAME directory :func:`backend.voice.ingest_pipeline.run_ingest_pipeline`
reads, so the existing pipeline analyzes them and flows reward to the bandit. No
network — a fake client injects the calls; no LLM — the analyzer is never run here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.voice import vapi_transcript_ingest as vti
from backend.voice.models import VapiCall
from backend.voice.transcript_ingest import parse_transcript_file


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _call(
    *,
    call_id: str,
    status: str = "ended",
    transcript: str = "AI: Hi, this is Morgan.\nUser: Sure, go ahead.",
    number: str | None = "+15551230001",
    name: str | None = "Rocklin Dental",
    started_at: str | None = "2026-07-07T15:30:00Z",
) -> VapiCall:
    payload: dict = {"id": call_id, "status": status, "transcript": transcript}
    if number is not None or name is not None:
        payload["customer"] = {"number": number, "name": name}
    if started_at is not None:
        payload["startedAt"] = started_at
    return VapiCall.model_validate(payload)


class _FakeVapiClient:
    """Minimal stand-in exposing only ``list_calls`` (the read the adapter uses)."""

    def __init__(self, calls: list) -> None:
        self._calls = calls
        self.limit_seen: int | None = None

    def list_calls(self, *, limit: int = 10) -> list:
        self.limit_seen = limit
        return list(self._calls)


@pytest.fixture
def _art_root(tmp_path, monkeypatch):
    """Point storage.root() at a temp dir so every artifact write is isolated."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    return tmp_path


_NOW = datetime(2026, 7, 7, 16, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Staging + filename round-trip
# ---------------------------------------------------------------------------


def test_stages_completed_call_and_filename_round_trips(_art_root):
    client = _FakeVapiClient([_call(call_id="call_1")])

    summary = vti.pull_and_stage_recent(client=client, now=_NOW)

    assert summary["pulled"] == 1
    assert summary["eligible"] == 1
    assert summary["staged"] == 1
    assert summary["error"] is None
    assert client.limit_seen == 100  # default limit, clamped range

    staged = list((_art_root / "voice" / "transcript_staging").glob("*.txt"))
    assert len(staged) == 1

    # The staged filename must parse back through the pipeline's own parser, and
    # the dialed phone must survive — it is the reward join key downstream.
    raw = parse_transcript_file(staged[0])
    assert raw is not None
    assert raw.direction == "Outgoing"
    assert raw.contact_phone.endswith("5551230001")  # last-10 == CSV/arm join key
    assert "Morgan" in raw.raw_text


def test_missing_customer_name_falls_back_to_parseable_filename(_art_root):
    # No company name -> the contact token falls back to the call-id tail, and
    # the filename must still parse (a '__' would drop the phone).
    client = _FakeVapiClient([_call(call_id="abc123def456", name=None)])

    vti.pull_and_stage_recent(client=client, now=_NOW)

    staged = list((_art_root / "voice" / "transcript_staging").glob("*.txt"))
    assert len(staged) == 1
    raw = parse_transcript_file(staged[0])
    assert raw is not None
    assert raw.contact_phone.endswith("5551230001")


# ---------------------------------------------------------------------------
# Dedup by call_id
# ---------------------------------------------------------------------------


def test_second_pull_dedups_by_call_id(_art_root):
    client = _FakeVapiClient([_call(call_id="call_1")])

    first = vti.pull_and_stage_recent(client=client, now=_NOW)
    second = vti.pull_and_stage_recent(client=client, now=_NOW)

    assert first["staged"] == 1
    assert second["staged"] == 0
    assert second["eligible"] == 1
    assert second["skipped_already"] == 1
    # Exactly one file on disk — the re-pull did not duplicate it.
    staged = list((_art_root / "voice" / "transcript_staging").glob("*.txt"))
    assert len(staged) == 1


# ---------------------------------------------------------------------------
# Skip rules
# ---------------------------------------------------------------------------


def test_skips_not_ended_no_transcript_and_no_phone(_art_root):
    client = _FakeVapiClient(
        [
            _call(call_id="live", status="in-progress"),
            _call(call_id="silent", transcript="   "),
            _call(call_id="nonum", number=None, name=None),
            _call(call_id="good"),
        ]
    )

    summary = vti.pull_and_stage_recent(client=client, now=_NOW)

    assert summary["staged"] == 1  # only "good"
    assert summary["skipped_not_ended"] == 1
    assert summary["skipped_no_transcript"] == 1
    assert summary["skipped_no_phone"] == 1


def test_skips_calls_outside_lookback(_art_root):
    old = (_NOW - timedelta(hours=100)).isoformat()
    client = _FakeVapiClient([_call(call_id="old", started_at=old)])

    summary = vti.pull_and_stage_recent(client=client, now=_NOW, lookback_hours=72)

    assert summary["staged"] == 0
    assert summary["skipped_old"] == 1


# ---------------------------------------------------------------------------
# Manifest freshness (the acceptance freshness signal)
# ---------------------------------------------------------------------------


def test_freshens_manifest_every_pass(_art_root):
    client = _FakeVapiClient([_call(call_id="call_1")])

    vti.pull_and_stage_recent(client=client, now=_NOW)

    manifest = _art_root / "voice" / "transcript_manifest.json"
    assert manifest.exists()
    import json

    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["source"] == "vapi_in_container_ingest"
    assert data["staged"] == 1
    assert data["ts"]  # a fresh timestamp is stamped


def test_manifest_freshened_even_on_empty_pull(_art_root):
    client = _FakeVapiClient([])
    vti.pull_and_stage_recent(client=client, now=_NOW)
    assert (_art_root / "voice" / "transcript_manifest.json").exists()


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


def test_no_vapi_key_is_a_clean_no_op(_art_root, monkeypatch):
    # No injected client and no VAPI_API_KEY -> _build_client returns None.
    monkeypatch.delenv("VAPI_API_KEY", raising=False)
    from backend.common.settings import reload_settings

    reload_settings()

    summary = vti.pull_and_stage_recent(client=None, now=_NOW)

    assert summary["staged"] == 0
    assert summary["error"] == "no_vapi_key"
    # Manifest still freshened so the freshness signal reflects the attempt.
    assert (_art_root / "voice" / "transcript_manifest.json").exists()


def test_vapi_list_failure_degrades_to_summary(_art_root):
    class _Boom:
        def list_calls(self, *, limit: int = 10):
            raise RuntimeError("vapi_down")

    summary = vti.pull_and_stage_recent(client=_Boom(), now=_NOW)

    assert summary["staged"] == 0
    assert summary["error"].startswith("vapi_list_failed")
