"""resolve_draft('allow') moves the draft to _resolved/ with appended block."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.common.codex.resolution import resolve_draft


def _seed_draft(codex_dir: Path, name: str = "ADR-099_test_action.draft.md") -> Path:
    drafts = codex_dir / "_drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    body = (
        f"# ADR-099 (DRAFT) - test_action blocked by G1\n"
        "\n"
        "**Reason:** test rationale.\n"
    )
    path = drafts / name
    path.write_text(body, encoding="utf-8")
    return path


def test_resolve_allow_moves_to_resolved(tmp_path: Path):
    draft = _seed_draft(tmp_path)
    new_path = resolve_draft(
        draft, decision="allow",
        rationale="The action is permitted under the new gate.",
        operator="alex", codex_dir=tmp_path,
    )
    assert new_path.is_file()
    assert new_path.parent.name == "_resolved"
    assert not draft.exists()
    text = new_path.read_text(encoding="utf-8")
    assert "## Resolution" in text
    assert "ALLOW" in text
    assert "alex" in text


def test_resolve_allow_requires_rationale(tmp_path: Path):
    draft = _seed_draft(tmp_path)
    with pytest.raises(ValueError):
        resolve_draft(
            draft, decision="allow", rationale="   ", codex_dir=tmp_path,
        )


def test_resolve_allow_refuses_overwrite(tmp_path: Path):
    draft = _seed_draft(tmp_path)
    resolve_draft(
        draft, decision="allow", rationale="ok", codex_dir=tmp_path,
    )
    draft2 = _seed_draft(tmp_path)
    with pytest.raises(FileExistsError):
        resolve_draft(
            draft2, decision="allow", rationale="again", codex_dir=tmp_path,
        )
