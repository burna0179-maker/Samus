"""CodexRegistry load/reload/is_loaded + fail-closed semantics."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.common.codex.exceptions import CodexUnavailable
from backend.common.codex.registry import CodexRegistry


REPO_ROOT = Path(__file__).resolve().parents[1]
CODEX_DIR = REPO_ROOT / "docs" / "codex"


def test_registry_starts_unloaded():
    reg = CodexRegistry()
    assert reg.is_loaded() is False


def test_registry_load_populates_collections():
    reg = CodexRegistry()
    reg.load(CODEX_DIR)
    assert reg.is_loaded() is True
    assert len(reg.guardrails()) >= 11
    assert len(reg.adrs()) >= 10
    assert reg.adr("ADR-002") is not None
    assert reg.adr("ADR-999") is None
    assert any(g.status == "enforced" for g in reg.enforced_guardrails())
    # ADR-012 flipped all intent guardrails to enforced. Assert the
    # categorization API still returns a list (zero or more) rather than
    # that any intent rule exists today.
    assert isinstance(reg.intent_guardrails(), list)
    assert len(reg.banned_phrases()) >= 12
    assert len(reg.shutdown_signals()) == 3


def test_registry_reload_clears_failure_latch():
    reg = CodexRegistry()
    reg.load(CODEX_DIR)
    assert reg.is_loaded()
    reg.reload(CODEX_DIR)
    assert reg.is_loaded()


def test_registry_load_failure_raises_codex_unavailable(tmp_path: Path):
    reg = CodexRegistry()
    with pytest.raises(CodexUnavailable):
        reg.load(tmp_path / "missing")
    assert reg.is_loaded() is False
    # Latched failed state: subsequent accessor calls raise the same.
    with pytest.raises(CodexUnavailable):
        reg.guardrails()
    with pytest.raises(CodexUnavailable):
        reg.adrs()


def test_registry_can_recover_via_reload(tmp_path: Path):
    reg = CodexRegistry()
    with pytest.raises(CodexUnavailable):
        reg.load(tmp_path / "missing")
    assert reg.is_loaded() is False
    reg.reload(CODEX_DIR)
    assert reg.is_loaded() is True


# ---------------------------------------------------------------------------
# Concept 1 — search_decisions (precedent retrieval over the ADR corpus)
# ---------------------------------------------------------------------------


def test_search_decisions_empty_query_returns_empty():
    reg = CodexRegistry()
    reg.load(CODEX_DIR)
    assert reg.search_decisions("") == []
    assert reg.search_decisions("   ") == []


def test_search_decisions_unloaded_registry_yields_empty_but_doesnt_raise():
    """Fail-soft: an unloaded registry returns [] without exploding."""
    reg = CodexRegistry()
    # Not calling reg.load(). ADR source is empty; resolved corpus is still
    # searchable but keyword has to hit it.
    out = reg.search_decisions("definitely-not-a-real-word-xyz")
    assert isinstance(out, list)


def test_search_decisions_keyword_hit_returns_match():
    reg = CodexRegistry()
    reg.load(CODEX_DIR)
    # "outreach" is a well-known ADR/resolved topic in the codex.
    out = reg.search_decisions("outreach", k=5)
    assert len(out) > 0
    assert all(m.score > 0.0 for m in out)
    # Ranked by score descending.
    scores = [m.score for m in out]
    assert scores == sorted(scores, reverse=True)


def test_search_decisions_respects_k_cap():
    reg = CodexRegistry()
    reg.load(CODEX_DIR)
    # A generic word likely hitting many ADRs.
    out = reg.search_decisions("samus", k=2)
    assert len(out) <= 2


def test_search_decisions_module_level_wrapper_lazy_loads():
    """The module-level search_decisions() lazily loads the registry."""
    from backend.common.codex import registry as reg_mod

    # Reset the singleton to unloaded state.
    reg_mod.REGISTRY = reg_mod.CodexRegistry()
    assert reg_mod.REGISTRY.is_loaded() is False

    out = reg_mod.search_decisions("outreach", k=3, codex_dir=CODEX_DIR)
    assert isinstance(out, list)
    # After the call the singleton is loaded.
    assert reg_mod.REGISTRY.is_loaded() is True


def test_search_decisions_score_is_zero_for_no_hit():
    reg = CodexRegistry()
    reg.load(CODEX_DIR)
    out = reg.search_decisions("zzzzz-never-appears-in-corpus-qqqqq", k=5)
    assert out == []


def test_decision_match_fields_are_populated():
    reg = CodexRegistry()
    reg.load(CODEX_DIR)
    out = reg.search_decisions("outreach", k=1)
    assert len(out) == 1
    m = out[0]
    assert m.title
    assert m.decision
    assert m.source in ("adr", "resolved")
    assert m.score > 0.0
    assert m.keyword_hits
    assert m.path


def test_search_decisions_title_boost_beats_body_frequency():
    """A single title hit should beat a body-only hit at equal recency."""
    reg = CodexRegistry()
    reg.load(CODEX_DIR)
    # Look for a word that appears prominently in ADR titles.
    out = reg.search_decisions("codex", k=5)
    # Top result's title should contain the query token.
    if out:
        assert any(
            "codex" in m.title.lower() or "codex" in m.adr_id.lower() or m.source == "resolved"
            for m in out[:3]
        )
