"""ADR auto-drafter — next-N numbering + template contents."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.common.codex.adr_drafter import draft_adr_for_violation
from backend.common.codex.models import ProposedAction
from backend.common.codex.registry import CodexRegistry


REPO_ROOT = Path(__file__).resolve().parents[1]
CODEX_DIR = REPO_ROOT / "docs" / "codex"


@pytest.fixture
def loaded_registry() -> CodexRegistry:
    reg = CodexRegistry()
    reg.load(CODEX_DIR)
    return reg


def test_drafter_numbering_is_next_after_highest_existing_adr(
    loaded_registry, tmp_path: Path,
):
    highest = max(int(a.id.split("-")[1]) for a in loaded_registry.adrs())
    expected_id = f"ADR-{highest + 1:03d}"
    action = ProposedAction(
        service="voice",
        capability="dispatch_call",
        action_kind="voice_dial",
        payload={"number": "+15555550000"},
        proposed_by="voice-workcell",
        correlation_id="corr-xyz",
    )
    path = draft_adr_for_violation(
        action, "VR-G5", "test reason", loaded_registry, drafts_dir=tmp_path,
    )
    assert path.is_file()
    assert path.name.startswith(expected_id)
    assert path.suffix == ".md"
    assert path.name.endswith(".draft.md")


def test_drafter_writes_expected_template_fields(loaded_registry, tmp_path: Path):
    action = ProposedAction(
        service="outreach",
        capability="run_campaign",
        action_kind="outreach_send",
        payload={"to": "ceo@example.com", "stake_sentence": ""},
        proposed_by="outreach-workcell",
        correlation_id="corr-template-test",
    )
    path = draft_adr_for_violation(
        action,
        "VR-G1",
        "Guardrail G1 requires Stake Sentence on every outbound dispatch.",
        loaded_registry,
        drafts_dir=tmp_path,
    )
    contents = path.read_text(encoding="utf-8")
    assert "DRAFT" in contents
    assert "VR-G1" in contents
    assert "outreach.run_campaign" in contents
    assert "outreach_send" in contents
    assert "corr-template-test" in contents
    assert "Guardrail G1 requires Stake Sentence" in contents
    assert "## Proposed action (raw)" in contents
    assert "ceo@example.com" in contents
    assert "## Decision required" in contents
    assert "A — ALLOW" in contents
    assert "B — REJECT" in contents


def test_drafter_creates_drafts_directory_if_missing(loaded_registry, tmp_path: Path):
    target = tmp_path / "nested" / "path" / "_drafts"
    assert not target.exists()
    action = ProposedAction(
        service="finance",
        capability="create_subscription",
        action_kind="subscription_create",
        payload={"plan": "x"},
        proposed_by="finance",
    )
    path = draft_adr_for_violation(
        action, "VR-ADR-003", "deferred", loaded_registry, drafts_dir=target,
    )
    assert target.is_dir()
    assert path.parent == target


def test_drafter_handles_missing_correlation_id(loaded_registry, tmp_path: Path):
    action = ProposedAction(
        service="x",
        capability="y",
        action_kind="voice_dial",
        payload={},
        proposed_by="z",
        correlation_id=None,
    )
    path = draft_adr_for_violation(
        action, "VR-G5", "no dial", loaded_registry, drafts_dir=tmp_path,
    )
    contents = path.read_text(encoding="utf-8")
    assert "(none)" in contents
