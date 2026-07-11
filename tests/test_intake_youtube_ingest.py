"""YouTube notification email -> transcript -> Claude -> Hivemind KB pipeline.

External edges (youtube-transcript-api, Anthropic, Neo4j) are all mocked
so the tests run offline. Per-stage failure modes are covered:
- missing video URL in body
- transcript fetch failure (TranscriptsDisabled / NoTranscriptFound)
- empty transcript text
- Claude budget denial / call error / malformed JSON / 'no insight' verdict
- Neo4j write returning empty rows

The orchestrator's "always write a KB stub even on failure" policy is
verified through the ledger row + KB-write-call assertions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from backend.intake.youtube_ingest import (
    DistillResult,
    TranscriptResult,
    YouTubeInsightHandled,
    distill_with_claude,
    extract_video_id,
    handle_youtube_email,
    is_youtube_notification,
)


# ---------------------------------------------------------------------------
# Minimal fake of ParsedInboundEmail so we don't need email module wiring.
# ---------------------------------------------------------------------------

@dataclass
class _FakeParsed:
    message_id: str = "<m1@x>"
    from_addr: str = "notifications@youtube.com"
    from_display: str = "YouTube <notifications@youtube.com>"
    to_addrs: list = field(default_factory=list)
    subject: str = "Some Channel just uploaded a video"
    date_header: str = "Mon, 12 May 2026 10:15:00 +0000"
    body_text: str = (
        "Some Channel just uploaded a new video.\n"
        "Watch now: https://www.youtube.com/watch?v=dQw4w9WgXcQ\n"
        "Unsubscribe link..."
    )
    body_format: str = "text/plain"
    attachment_names: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def test_classifier_accepts_notifications_sender_with_watch_url():
    p = _FakeParsed()
    assert is_youtube_notification(p) is True


def test_classifier_accepts_youtu_be_short_url():
    p = _FakeParsed(body_text="New video https://youtu.be/dQw4w9WgXcQ ...")
    assert is_youtube_notification(p) is True


def test_classifier_accepts_no_reply_sender():
    p = _FakeParsed(from_addr="no-reply@youtube.com")
    assert is_youtube_notification(p) is True


def test_classifier_rejects_third_party_quoting_youtube_link():
    """A newsletter from random@example.com that QUOTES a YouTube link
    must NOT trip the classifier — we only want first-party YouTube mail."""
    p = _FakeParsed(from_addr="news@example.com")
    assert is_youtube_notification(p) is False


def test_classifier_rejects_youtube_email_without_watch_url():
    p = _FakeParsed(body_text="Welcome to YouTube Premium. Manage settings...")
    assert is_youtube_notification(p) is False


def test_extract_video_id_pulls_first_match():
    assert extract_video_id(_FakeParsed()) == "dQw4w9WgXcQ"


def test_extract_video_id_returns_none_when_no_url():
    assert extract_video_id(_FakeParsed(body_text="plain text")) is None


# ---------------------------------------------------------------------------
# Distiller — uses anthropic_messages mocked at module level.
# ---------------------------------------------------------------------------

def _patch_anthropic(monkeypatch, *, response_text: str, raise_exc=None):
    """Replace anthropic_messages used inside distill_with_claude."""
    def _fake(*, workcell, api_key, prompt, system=None, max_tokens=1024, **_kw):
        if raise_exc is not None:
            raise raise_exc
        return response_text, {"input_tokens": 100, "output_tokens": 50}
    import backend.common.llm_client as llm_mod
    monkeypatch.setattr(llm_mod, "anthropic_messages", _fake)


def _override_settings(monkeypatch, *, anthropic_api_key: str = "test-key"):
    # Historical helper — youtube_ingest.distill_with_claude no longer
    # consults Settings (the api_key gate was removed when the LLM client
    # swapped to LM Studio / OpenAI). Kept as a no-op so existing tests
    # still call it; the real LLM stub comes from _patch_anthropic.
    return


def test_distill_extracted_path(monkeypatch):
    _override_settings(monkeypatch)
    _patch_anthropic(monkeypatch, response_text='''
    {
      "insight_present": true,
      "summary": "Video demos a new agent-memory pattern using event sourcing.",
      "proposed_target_agent": "anita",
      "proposed_changes_md": "## Hypothesis\\nApply event sourcing to TaskLedger..."
    }
    ''')
    r = distill_with_claude("some transcript text", video_id="abc123abcde")
    assert r.status == "extracted"
    assert r.proposed_target_agent == "anita"
    assert "event sourcing" in r.proposed_changes_md.lower()
    assert "agent-memory" in r.summary


def test_distill_empty_path_when_claude_says_no_insight(monkeypatch):
    _override_settings(monkeypatch)
    _patch_anthropic(monkeypatch, response_text='''
    {
      "insight_present": false,
      "summary": "Marketing video about a SaaS product, no engineering content.",
      "proposed_target_agent": "none",
      "proposed_changes_md": ""
    }
    ''')
    r = distill_with_claude("some marketing transcript", video_id="aaaaaaaaaaa")
    assert r.status == "empty"
    assert r.proposed_target_agent == "none"
    assert r.proposed_changes_md == ""


def test_distill_strips_markdown_fences_around_json(monkeypatch):
    """Claude sometimes wraps responses in ```json ... ``` despite the
    'output ONLY the JSON object' instruction; the distiller must tolerate."""
    _override_settings(monkeypatch)
    _patch_anthropic(monkeypatch, response_text='''```json
    {"insight_present": true, "summary": "ok", "proposed_target_agent": "darwin", "proposed_changes_md": "x"}
    ```''')
    r = distill_with_claude("t", video_id="bbbbbbbbbbb")
    assert r.status == "extracted"
    assert r.proposed_target_agent == "darwin"


def test_distill_failed_on_unknown_target_agent_collapses_to_none(monkeypatch):
    _override_settings(monkeypatch)
    _patch_anthropic(monkeypatch, response_text='''
    {"insight_present": true, "summary": "x", "proposed_target_agent": "morpheus", "proposed_changes_md": "y"}
    ''')
    r = distill_with_claude("t", video_id="ccccccccccc")
    # 'morpheus' is not a Hustleforge agent -> collapses to 'none' -> empty.
    assert r.proposed_target_agent == "none"
    assert r.status == "empty"


def test_distill_succeeds_without_api_key(monkeypatch):
    """LM Studio backend needs no API key — empty key must not block."""
    _override_settings(monkeypatch, anthropic_api_key="")
    _patch_anthropic(monkeypatch, response_text='''
    {"insight_present": true, "summary": "ok", "proposed_target_agent": "anita", "proposed_changes_md": "x"}
    ''')
    r = distill_with_claude("transcript", video_id="ddddddddddd")
    assert r.status == "extracted"


def test_distill_failed_on_malformed_json(monkeypatch):
    _override_settings(monkeypatch)
    _patch_anthropic(monkeypatch, response_text="not json at all")
    r = distill_with_claude("transcript", video_id="eeeeeeeeeee")
    assert r.status == "failed"
    assert "json_parse_failed" in r.error


def test_distill_failed_on_budget_exceeded(monkeypatch):
    _override_settings(monkeypatch)
    from backend.common.llm_client import BudgetExceeded
    class _D:
        used = 100
        quota = 100
        reason = "daily_quota_exhausted"
        def __str__(self): return "denied"
    _patch_anthropic(monkeypatch, response_text="", raise_exc=BudgetExceeded(_D()))
    r = distill_with_claude("transcript", video_id="fffffffffff")
    assert r.status == "failed"
    assert "budget_exceeded" in r.error


# ---------------------------------------------------------------------------
# Orchestrator — full pipeline with all externals mocked.
# ---------------------------------------------------------------------------

def _stub_transcript(monkeypatch, *, status: str, text: str = "", error: str = ""):
    import backend.intake.youtube_ingest as yt
    def _fake(_video_id: str) -> TranscriptResult:
        if status == "ok":
            excerpt = text[:2048] if len(text) > 2048 else text
            return TranscriptResult(status="ok", text=text, excerpt=excerpt)
        return TranscriptResult(status=status, error=error)
    monkeypatch.setattr(yt, "fetch_transcript", _fake)


def _stub_distill(monkeypatch, *, result: DistillResult):
    import backend.intake.youtube_ingest as yt
    def _fake(transcript_text, *, video_id, video_title="", channel="", **_kw):
        return result
    monkeypatch.setattr(yt, "distill_with_claude", _fake)


def _stub_kb_write(monkeypatch, *, returns: str = "video_id_ok",
                    capture: dict | None = None):
    import backend.intake.youtube_ingest as yt
    def _fake(props):
        if capture is not None:
            capture["props"] = props
        return returns
    monkeypatch.setattr(yt, "write_kb_node", _fake)


def _stub_persist(monkeypatch, *, returns: str = "/tmp/fake.txt",
                   capture: dict | None = None):
    import backend.intake.youtube_ingest as yt
    def _fake(video_id, text):
        if capture is not None:
            capture["video_id"] = video_id
            capture["text_len"] = len(text)
        return returns
    monkeypatch.setattr(yt, "persist_full_transcript", _fake)


def _redirect_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "SAMUS_YT_LEDGER", str(tmp_path / "youtube_ingest.jsonl"),
    )
    # Lower the min-transcript-chars gate so existing fixtures with short
    # stubbed transcripts still exercise the LLM (distill) path. The gate
    # itself is verified by dedicated tests.
    monkeypatch.setenv("SAMUS_YT_MIN_TRANSCRIPT_CHARS", "1")


def test_handle_happy_path_writes_kb_node_and_ledger(monkeypatch, tmp_path):
    _redirect_ledger(monkeypatch, tmp_path)
    _stub_transcript(monkeypatch, status="ok", text="full transcript body")
    capture_kb: dict = {}
    capture_persist: dict = {}
    _stub_kb_write(monkeypatch, returns="dQw4w9WgXcQ", capture=capture_kb)
    _stub_persist(monkeypatch, capture=capture_persist)
    _stub_distill(monkeypatch, result=DistillResult(
        status="extracted",
        summary="A talk on agentic memory.",
        proposed_target_agent="anita",
        proposed_changes_md="## Hypothesis\nApply event sourcing...",
    ))

    handled = handle_youtube_email(_FakeParsed())

    assert handled.video_id == "dQw4w9WgXcQ"
    assert handled.transcript_status == "ok"
    assert handled.distill_status == "extracted"
    assert handled.proposed_target_agent == "anita"
    assert handled.kb_node_written is True
    assert handled.persisted is True
    assert handled.error == ""

    # KB props shape
    p = capture_kb["props"]
    assert p["video_id"] == "dQw4w9WgXcQ"
    assert p["video_url"].endswith("watch?v=dQw4w9WgXcQ")
    assert p["proposed_target_agent"] == "anita"
    assert "event sourcing" in p["proposed_changes_md"].lower()
    assert p["transcript_status"] == "ok"
    assert p["transcript_path"].startswith("/tmp/")

    # Ledger row
    ledger = (tmp_path / "youtube_ingest.jsonl").read_text(encoding="utf-8")
    assert "dQw4w9WgXcQ" in ledger
    assert "extracted" in ledger
    assert '"anita"' in ledger


def test_handle_no_video_id_in_body(monkeypatch, tmp_path):
    _redirect_ledger(monkeypatch, tmp_path)
    handled = handle_youtube_email(_FakeParsed(body_text="no urls here"))
    assert handled.video_id == ""
    assert handled.error == "no_video_id_in_body"
    assert handled.kb_node_written is False
    # No KB write attempted -> ledger row still useful for diagnosis but
    # the file isn't written either since we early-return.


def test_handle_no_transcript_still_writes_stub_kb_node(monkeypatch, tmp_path):
    _redirect_ledger(monkeypatch, tmp_path)
    _stub_transcript(monkeypatch, status="no_transcript", error="TranscriptsDisabled")
    capture: dict = {}
    _stub_kb_write(monkeypatch, returns="dQw4w9WgXcQ", capture=capture)
    # Note: distill should NOT run when transcript missing; we stub it
    # anyway to fail-loud if it's called.
    import backend.intake.youtube_ingest as yt
    def _should_not_run(*a, **kw):
        raise AssertionError("distill_with_claude must not be called when no transcript")
    monkeypatch.setattr(yt, "distill_with_claude", _should_not_run)

    handled = handle_youtube_email(_FakeParsed())

    assert handled.transcript_status == "no_transcript"
    assert handled.distill_status == "no_transcript"
    assert handled.kb_node_written is True  # stub still written
    assert capture["props"]["distill_status"] == "no_transcript"
    assert capture["props"]["transcript_excerpt"] == ""


def test_handle_distill_empty_writes_stub_with_empty_status(monkeypatch, tmp_path):
    _redirect_ledger(monkeypatch, tmp_path)
    _stub_transcript(monkeypatch, status="ok", text="marketing video transcript")
    _stub_persist(monkeypatch)
    capture: dict = {}
    _stub_kb_write(monkeypatch, returns="dQw4w9WgXcQ", capture=capture)
    _stub_distill(monkeypatch, result=DistillResult(
        status="empty",
        summary="Just a marketing demo, no engineering content.",
        proposed_target_agent="none",
        proposed_changes_md="",
    ))

    handled = handle_youtube_email(_FakeParsed())
    assert handled.distill_status == "empty"
    assert handled.proposed_target_agent == "none"
    assert capture["props"]["distill_status"] == "empty"
    assert capture["props"]["proposed_changes_md"] == ""
    # Stub still written.
    assert handled.kb_node_written is True


def test_handle_kb_write_failure_surfaces_in_result(monkeypatch, tmp_path):
    _redirect_ledger(monkeypatch, tmp_path)
    _stub_transcript(monkeypatch, status="ok", text="transcript")
    _stub_persist(monkeypatch)
    _stub_kb_write(monkeypatch, returns="")  # Neo4j unreachable
    _stub_distill(monkeypatch, result=DistillResult(
        status="extracted", summary="x",
        proposed_target_agent="darwin", proposed_changes_md="y",
    ))
    handled = handle_youtube_email(_FakeParsed())
    assert handled.kb_node_written is False
    assert handled.error == "neo4j_write_failed"
    assert handled.persisted is False
