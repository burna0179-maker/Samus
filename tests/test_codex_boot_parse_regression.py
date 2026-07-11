"""Boot-parse regression suite for the live Codex.

This test reads the ACTUAL ``docs/codex/`` directory of the worktree.
If anyone edits a chapter in a way the parser can't handle, this fails
immediately — it's the canary that keeps fail-CLOSED boot honest.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.common.codex import REGISTRY


_REPO_ROOT = Path(__file__).resolve().parents[1]
_CODEX_DIR = _REPO_ROOT / "docs" / "codex"

_EXPECTED_CHAPTERS = (
    "00_INDEX.md",
    "01_premise.md",
    "02_council_verdict.md",
    "03_stake_sentence.md",
    "04_guardrails.md",
    "05_pipeline_flow.md",
    "06_modules_map.md",
    "07_operational.md",
    "08_decisions_log.md",
    "09_failure_modes.md",
    "10_glossary.md",
    "11_when_to_shut_it_down.md",
    "12_the_validation_layer.md",
)


@pytest.fixture(scope="module", autouse=True)
def _load_live_codex():
    REGISTRY.reload(_CODEX_DIR)
    yield


def test_codex_is_loaded():
    assert REGISTRY.is_loaded() is True


def test_codex_chapters_all_present():
    missing = [c for c in _EXPECTED_CHAPTERS if not (_CODEX_DIR / c).is_file()]
    assert not missing, f"missing chapters: {missing}"


def test_codex_drafts_dir_exists():
    drafts = _CODEX_DIR / "_drafts"
    assert drafts.is_dir(), "docs/codex/_drafts/ must exist"
    assert (drafts / ".gitkeep").is_file(), "_drafts/.gitkeep sentinel missing"


def test_guardrails_floor_eleven():
    rails = REGISTRY.guardrails()
    assert len(rails) >= 11, f"expected >= 11 guardrails, got {len(rails)}"


def test_every_guardrail_has_title_and_description():
    for rail in REGISTRY.guardrails():
        assert rail.title, f"{rail.id} missing title"
        assert rail.description, f"{rail.id} missing description"


def test_adrs_floor_eleven():
    adrs = REGISTRY.adrs()
    assert len(adrs) >= 11, f"expected >= 11 ADRs, got {len(adrs)}"


def test_every_adr_has_decision_body():
    for adr in REGISTRY.adrs():
        assert adr.decision, f"{adr.id} missing decision field"


def test_no_duplicate_adr_ids():
    ids = [adr.id for adr in REGISTRY.adrs()]
    assert len(ids) == len(set(ids)), f"duplicate ADR ids in {ids}"


def test_banned_phrases_floor_twelve():
    phrases = REGISTRY.banned_phrases()
    assert len(phrases) >= 12, f"expected >= 12 banned phrases, got {len(phrases)}"


def test_shutdown_signals_floor_three():
    signals = REGISTRY.shutdown_signals()
    assert len(signals) >= 3, f"expected >= 3 shutdown signals, got {len(signals)}"
