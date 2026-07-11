"""resolve_draft('reject') deletes the draft + logs forensics."""
from __future__ import annotations

import json
from pathlib import Path

from backend.common.codex.resolution import resolve_draft


def _seed_draft(codex_dir: Path) -> Path:
    drafts = codex_dir / "_drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    path = drafts / "ADR-099_rejected_action.draft.md"
    path.write_text("# ADR-099 (DRAFT)\n", encoding="utf-8")
    return path


def test_reject_deletes_draft_and_logs(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    draft = _seed_draft(tmp_path)
    resolve_draft(
        draft, decision="reject",
        rationale="operator chose to remove the offending call",
        operator="alex", codex_dir=tmp_path,
    )
    assert not draft.exists()
    ledger = tmp_path / "artifacts" / "host_artifacts" / "codex_rejected_drafts.jsonl"
    assert ledger.is_file()
    lines = [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 1
    assert lines[0]["draft_name"] == "ADR-099_rejected_action.draft.md"
    assert lines[0]["operator"] == "alex"
    assert "offending call" in lines[0]["rationale"]


def test_reject_missing_draft_raises(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    missing = tmp_path / "_drafts" / "nope.draft.md"
    try:
        resolve_draft(
            missing, decision="reject", rationale="x",
            codex_dir=tmp_path,
        )
    except FileNotFoundError:
        return
    raise AssertionError("expected FileNotFoundError")
