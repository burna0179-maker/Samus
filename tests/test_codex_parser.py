"""Codex parser smoke tests against the live `docs/codex/` chapters."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.common.codex.exceptions import CodexParseError
from backend.common.codex.parser import parse_codex


REPO_ROOT = Path(__file__).resolve().parents[1]
CODEX_DIR = REPO_ROOT / "docs" / "codex"


def test_parses_live_codex_guardrail_count():
    parsed = parse_codex(CODEX_DIR)
    assert len(parsed.guardrails) >= 11
    ids = {g.id for g in parsed.guardrails}
    assert {"G1", "G2", "G3", "G4", "G5"} <= ids


def test_parses_live_codex_adr_count():
    parsed = parse_codex(CODEX_DIR)
    assert len(parsed.adrs) >= 10
    ids = {a.id for a in parsed.adrs}
    assert {"ADR-001", "ADR-002", "ADR-003", "ADR-008"} <= ids


def test_parses_live_codex_shutdown_signals():
    parsed = parse_codex(CODEX_DIR)
    assert [s.id for s in parsed.shutdown_signals] == ["S1", "S2", "S3"]
    for sig in parsed.shutdown_signals:
        assert sig.action
        assert sig.signal_description


def test_status_classifies_intent_vs_enforced():
    parsed = parse_codex(CODEX_DIR)
    by_id = {g.id: g for g in parsed.guardrails}
    assert by_id["G1"].status == "enforced"
    assert by_id["G2"].status == "enforced"
    # G6/G7/G8 flipped to enforced by ADR-012 (2026-05-30).
    assert by_id["G6"].status == "enforced"
    assert by_id["G7"].status == "enforced"
    assert by_id["G8"].status == "enforced"


def test_banned_phrases_passthrough_imports_from_guard():
    from backend.common.stake_sentence_guard import STAKE_SENTENCE_BANNED_PHRASES

    parsed = parse_codex(CODEX_DIR)
    parsed_phrases = [bp.phrase for bp in parsed.banned_phrases]
    assert parsed_phrases == list(STAKE_SENTENCE_BANNED_PHRASES)
    for bp in parsed.banned_phrases:
        assert bp.source.endswith("STAKE_SENTENCE_BANNED_PHRASES")


def test_glossary_terms_extracted():
    parsed = parse_codex(CODEX_DIR)
    assert "Stake Sentence" in parsed.glossary_terms
    assert "Gap Report" in parsed.glossary_terms
    assert "Fail-closed" in parsed.glossary_terms


def test_malformed_chapter_raises_codex_parse_error(tmp_path: Path):
    # Build a synthetic codex dir missing the required '**What it stops:**'
    # block under a guardrail header -> parser MUST raise, not silently skip.
    (tmp_path / "04_guardrails.md").write_text(
        "# 04 — The Guardrails\n\n## G1 — Stake Sentence required\n\n"
        "Some prose without the required marker.\n",
        encoding="utf-8",
    )
    # Provide the other required chapters so parse_codex reaches the
    # malformed one.
    (tmp_path / "08_decisions_log.md").write_text(
        "# 08\n\n## ADR-001 | 2026-05-30 | Test\n\n"
        "**Decision:** placeholder.\n\n",
        encoding="utf-8",
    )
    (tmp_path / "10_glossary.md").write_text(
        "# 10\n\n**Term** — definition here.\n\n", encoding="utf-8",
    )
    (tmp_path / "11_when_to_shut_it_down.md").write_text(
        "# 11\n\n## Three reasons to shut Samus down\n\n"
        "### 1. It worked\n\n**Signal:** s1.\n\n**Action:** a1.\n\n"
        "### 2. Asymmetry closed\n\n**Signal:** s2.\n\n**Action:** a2.\n\n"
        "### 3. Codex drift\n\n**Signal:** s3.\n\n**Action:** a3.\n\n",
        encoding="utf-8",
    )
    with pytest.raises(CodexParseError) as excinfo:
        parse_codex(tmp_path)
    assert "G1" in str(excinfo.value) or "04_guardrails" in str(excinfo.value)


def test_missing_directory_raises(tmp_path: Path):
    with pytest.raises(CodexParseError):
        parse_codex(tmp_path / "does-not-exist")
