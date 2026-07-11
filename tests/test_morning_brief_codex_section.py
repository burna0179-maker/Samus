"""Morning brief surfaces 'Open Codex ADR drafts' when drafts exist."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def fake_codex_dir(tmp_path: Path, monkeypatch):
    codex = tmp_path / "codex"
    (codex / "_drafts").mkdir(parents=True)
    draft_a = codex / "_drafts" / "ADR-012_outreach_send.draft.md"
    draft_a.write_text(
        "# ADR-012 (DRAFT) - outreach_send blocked by G3\n",
        encoding="utf-8",
    )
    draft_b = codex / "_drafts" / "ADR-013_voice_dial.draft.md"
    draft_b.write_text(
        "# ADR-013 (DRAFT) - voice_dial blocked by G7\n",
        encoding="utf-8",
    )

    import backend.common.codex.registry as registry_mod

    monkeypatch.setattr(registry_mod, "_default_codex_dir", lambda: codex)
    return codex


def test_morning_brief_emits_codex_section(fake_codex_dir):
    from backend.morning import _render_codex_drafts

    out = "\n".join(_render_codex_drafts())
    assert "OPEN CODEX ADR DRAFTS" in out
    assert "(2)" in out
    assert "ADR-012_outreach_send.draft.md" in out
    assert "ADR-013_voice_dial.draft.md" in out
    assert "G3" in out
    assert "G7" in out


def test_morning_brief_skips_when_no_drafts(tmp_path: Path, monkeypatch):
    codex = tmp_path / "codex"
    (codex / "_drafts").mkdir(parents=True)
    import backend.common.codex.registry as registry_mod

    monkeypatch.setattr(registry_mod, "_default_codex_dir", lambda: codex)
    from backend.morning import _render_codex_drafts

    assert _render_codex_drafts() == []


def test_morning_brief_fails_open_when_codex_missing(tmp_path: Path, monkeypatch):
    import backend.common.codex.registry as registry_mod

    monkeypatch.setattr(
        registry_mod,
        "_default_codex_dir",
        lambda: tmp_path / "nope" / "codex",
    )
    from backend.morning import _render_codex_drafts

    assert _render_codex_drafts() == []
