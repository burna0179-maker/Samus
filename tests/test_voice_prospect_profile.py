"""Tests for backend.voice.prospect_profile.build_prospect_context.

The module reads the operator-action ledger at
``HF_DATA_DIR/call_outcomes/call_outcomes_<date>.jsonl`` and projects a
prospect's most-recent prior outcome + note. These tests point HF_DATA_DIR at
a tmp dir and write synthetic journals.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from backend.voice.prospect_profile import build_prospect_context


def _write_journal(call_outcomes_dir, day, rows) -> None:
    """Write a ``call_outcomes_<day>.jsonl`` file with the given row dicts."""
    path = call_outcomes_dir / f"call_outcomes_{day.isoformat()}.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )


@pytest.fixture()
def journal_dir(tmp_path, monkeypatch):
    """Point HF_DATA_DIR at a tmp dir and return its call_outcomes subdir."""
    monkeypatch.setenv("HF_DATA_DIR", str(tmp_path))
    co = tmp_path / "call_outcomes"
    co.mkdir()
    return co


def test_returns_latest_outcome_and_notes(journal_dir):
    today = date.today()
    _write_journal(journal_dir, today, [
        {
            "ts": f"{today.isoformat()}T10:00:00",
            "prospect_id": "pr_ABC",
            "company": "Acme",
            "phone": "(530) 111-2222",
            "outcome": "follow_up",
            "notes": "Send the SEO audit; decision-maker is Pat.",
            "source": "forge-ui",
        },
    ])

    ctx = build_prospect_context("pr_ABC")

    assert ctx["prior_outcome"] == "follow_up"
    assert ctx["prior_notes"] == "Send the SEO audit; decision-maker is Pat."
    assert today.isoformat() in ctx["prior_contact_summary"]
    assert "outcome: follow_up" in ctx["prior_contact_summary"]
    assert "note: Send the SEO audit" in ctx["prior_contact_summary"]


def test_latest_wins_across_days(journal_dir):
    today = date.today()
    yesterday = today - timedelta(days=1)

    # Older prior-day record.
    _write_journal(journal_dir, yesterday, [
        {
            "ts": f"{yesterday.isoformat()}T09:00:00",
            "prospect_id": "pr_ABC",
            "outcome": "not_interested",
            "notes": "Old note.",
            "source": "forge-ui",
        },
    ])
    # Newer record today -> must win.
    _write_journal(journal_dir, today, [
        {
            "ts": f"{today.isoformat()}T15:30:00",
            "prospect_id": "pr_ABC",
            "outcome": "voicemail",
            "notes": "Left a voicemail today.",
            "source": "forge-ui",
        },
    ])

    ctx = build_prospect_context("pr_ABC")

    assert ctx["prior_outcome"] == "voicemail"
    assert ctx["prior_notes"] == "Left a voicemail today."


def test_latest_wins_within_same_day(journal_dir):
    today = date.today()
    _write_journal(journal_dir, today, [
        {
            "ts": f"{today.isoformat()}T08:00:00",
            "prospect_id": "pr_ABC",
            "outcome": "no_answer",
            "notes": "early",
            "source": "forge-ui",
        },
        {
            "ts": f"{today.isoformat()}T17:00:00",
            "prospect_id": "pr_ABC",
            "outcome": "follow_up",
            "notes": "later",
            "source": "forge-ui",
        },
    ])

    ctx = build_prospect_context("pr_ABC")

    assert ctx["prior_outcome"] == "follow_up"
    assert ctx["prior_notes"] == "later"


def test_unknown_prospect_returns_empty(journal_dir):
    today = date.today()
    _write_journal(journal_dir, today, [
        {
            "ts": f"{today.isoformat()}T10:00:00",
            "prospect_id": "pr_SOMEONE_ELSE",
            "outcome": "follow_up",
            "notes": "not for us",
            "source": "forge-ui",
        },
    ])

    ctx = build_prospect_context("pr_NOBODY")

    assert ctx == {
        "prior_outcome": "",
        "prior_notes": "",
        "prior_contact_summary": "",
    }


def test_notes_truncated(journal_dir):
    today = date.today()
    long_note = "x" * 500
    _write_journal(journal_dir, today, [
        {
            "ts": f"{today.isoformat()}T10:00:00",
            "prospect_id": "pr_ABC",
            "outcome": "noted",
            "notes": long_note,
            "source": "forge-ui",
        },
    ])

    ctx = build_prospect_context("pr_ABC")

    assert len(ctx["prior_notes"]) <= 200
    assert ctx["prior_notes"].endswith("…")


def test_missing_dir_fails_soft(tmp_path, monkeypatch):
    # HF_DATA_DIR points at a dir with NO call_outcomes subdir.
    monkeypatch.setenv("HF_DATA_DIR", str(tmp_path / "does_not_exist"))

    ctx = build_prospect_context("pr_ABC")

    assert ctx == {
        "prior_outcome": "",
        "prior_notes": "",
        "prior_contact_summary": "",
    }


def test_empty_prospect_id_returns_empty(journal_dir):
    assert build_prospect_context("") == {
        "prior_outcome": "",
        "prior_notes": "",
        "prior_contact_summary": "",
    }


def test_malformed_lines_skipped(journal_dir):
    today = date.today()
    path = journal_dir / f"call_outcomes_{today.isoformat()}.jsonl"
    path.write_text(
        "this is not json\n"
        "\n"
        + json.dumps({
            "ts": f"{today.isoformat()}T10:00:00",
            "prospect_id": "pr_ABC",
            "outcome": "follow_up",
            "notes": "valid",
            "source": "forge-ui",
        }) + "\n"
        + "{broken json\n",
        encoding="utf-8",
    )

    ctx = build_prospect_context("pr_ABC")

    assert ctx["prior_outcome"] == "follow_up"
    assert ctx["prior_notes"] == "valid"


def test_outside_window_excluded(journal_dir):
    today = date.today()
    old_day = today - timedelta(days=40)
    _write_journal(journal_dir, old_day, [
        {
            "ts": f"{old_day.isoformat()}T10:00:00",
            "prospect_id": "pr_ABC",
            "outcome": "follow_up",
            "notes": "ancient",
            "source": "forge-ui",
        },
    ])

    # Default days=30 -> the 40-day-old record is outside the scan window.
    ctx = build_prospect_context("pr_ABC")

    assert ctx["prior_outcome"] == ""
    assert ctx["prior_contact_summary"] == ""
