"""promote_to_decisions_log renumbers to next sequential ADR-NNN."""
from __future__ import annotations

import shutil
from pathlib import Path

from backend.common.codex.registry import CodexRegistry
from backend.common.codex.resolution import (
    next_real_adr_number,
    promote_to_decisions_log,
    resolve_draft,
)


_REPO_ROOT = Path(__file__).resolve().parents[1]
_LIVE_CODEX = _REPO_ROOT / "docs" / "codex"


def _seed_draft(codex_dir: Path) -> Path:
    drafts = codex_dir / "_drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    body = (
        "# ADR-999 (DRAFT) - new_action blocked by G1\n"
        "\n"
        "**Reason:** A new gate is needed.\n"
        "\n"
        "## Decision required\n"
        "\n"
        "Allow with constraints.\n"
    )
    path = drafts / "ADR-999_new_action.draft.md"
    path.write_text(body, encoding="utf-8")
    return path


def _copy_live_codex(tmp_path: Path) -> Path:
    target = tmp_path / "codex"
    shutil.copytree(_LIVE_CODEX, target)
    return target


def test_next_real_adr_number_walks_registry_and_resolved(tmp_path: Path):
    codex = _copy_live_codex(tmp_path)
    registry = CodexRegistry()
    registry.load(codex)
    base = next_real_adr_number(registry, codex_dir=codex)
    # The live codex carries >= 11 ADRs, so the next number must be >= 12.
    assert base >= 12
    # Drop a _resolved/ entry one ahead of base and confirm we skip past it.
    resolved = codex / "_resolved"
    resolved.mkdir(parents=True, exist_ok=True)
    (resolved / f"ADR-{base:03d}_pretend.resolved.md").write_text(
        "# ADR-NNN (placeholder)\n", encoding="utf-8",
    )
    base2 = next_real_adr_number(registry, codex_dir=codex)
    assert base2 == base + 1


def test_promote_appends_before_template_marker(tmp_path: Path):
    codex = _copy_live_codex(tmp_path)
    registry = CodexRegistry()
    registry.load(codex)
    draft = _seed_draft(codex)
    resolved = resolve_draft(
        draft, decision="allow",
        rationale="operator approved", operator="alex",
        codex_dir=codex,
    )
    expected_id = f"ADR-{next_real_adr_number(registry, codex_dir=codex):03d}"
    new_id = promote_to_decisions_log(
        resolved, registry=registry, codex_dir=codex,
        decisions_log=codex / "08_decisions_log.md",
        title="new action constrained by G1",
        date_iso="2026-05-30",
    )
    assert new_id == expected_id
    log_text = (codex / "08_decisions_log.md").read_text(encoding="utf-8")
    header = f"## {new_id} | 2026-05-30 | new action constrained by G1"
    assert header in log_text
    # Ordering: the new ADR sits before the template marker, not after.
    assert log_text.index(header) < log_text.index("## Template for new ADRs")
    # Re-parse with a fresh registry to confirm the new ADR is picked up.
    fresh = CodexRegistry()
    fresh.load(codex)
    assert any(a.id == new_id for a in fresh.adrs())
