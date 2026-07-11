"""Lint guard: only ``backend.common.llm_client`` may touch Anthropic directly.

Every other LLM caller in ``backend/`` must route through
:func:`backend.common.llm_client.anthropic_messages` so per-workcell token
budgeting, outcome accounting, and (once cherry-picked from 29ba2df)
prompt-cache + global $-cap + circuit-breaker + model-floor protections
apply uniformly.

Two legacy direct-httpx fallback paths are grandfathered in
``prospecting/callsheet.py`` (``build_call_sheet_with_llm_direct``) and
``seo/content.py`` (raw httpx constants kept alongside ``_parse_llm_response``
for direct-call tests). New callers MAY NOT be added without explicit review.

This test fails if:
  - any ``backend/`` module imports ``anthropic`` and is NOT allowlisted, or
  - any ``backend/`` module contains the string literal
    ``api.anthropic.com`` and is NOT allowlisted.

Adding a new allowlist entry is the reviewer chokepoint — touch the
``_ALLOWLIST`` constant below and explain why in the commit message.

Related: ``project_samus_llm_token_policy`` memory (Lever 2.3).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Modules grandfathered to touch the Anthropic surface directly. Adding here
# requires a code review pass — see module docstring.
_ALLOWLIST: frozenset[str] = frozenset({
    "backend/common/llm_client.py",
    "backend/prospecting/callsheet.py",
    "backend/seo/content.py",
    # Triangulation leg C is BY DESIGN an independent Claude (Anthropic) read
    # that must NOT share a transport with the local model: llm_client's
    # anthropic_messages() now targets local LM Studio, so routing leg C
    # through it would collapse triangulation onto the very model being
    # audited. Direct Messages-API call, fail-soft to the local auditor.
    "backend/cognitive/triangulation.py",
    # Billing telemetry, not an LLM call: hits the Anthropic Admin API
    # /v1/organizations/cost_report to READ spend. There are no tokens to
    # budget, so the llm_client 4-layer budget wrapper does not apply.
    "backend/finance/service_cost_monitor.py",
})

# String literal that, if present, indicates direct Anthropic API surface use.
_DIRECT_URL_FRAGMENT = "api.anthropic.com"

# Module names whose import indicates direct SDK use.
_ANTHROPIC_MODULE_PREFIXES: tuple[str, ...] = ("anthropic",)


def _backend_root() -> Path:
    """Return the ``backend/`` directory at samus-worktree resolution."""
    here = Path(__file__).resolve()
    # tests/test_llm_caller_lint.py -> tests -> Samus -> backend
    return here.parent.parent / "backend"


def _iter_backend_modules() -> list[Path]:
    root = _backend_root()
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _module_imports_anthropic(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name.startswith(p) for p in _ANTHROPIC_MODULE_PREFIXES):
                    return True
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if any(mod.startswith(p) for p in _ANTHROPIC_MODULE_PREFIXES):
                return True
    return False


def _relpath(p: Path) -> str:
    root = _backend_root().parent  # Samus/
    return p.relative_to(root).as_posix()


def test_only_allowlisted_modules_touch_anthropic_surface() -> None:
    """Fail if a non-allowlisted backend module touches the Anthropic surface.

    Either via ``import anthropic`` (SDK) or via the literal
    ``api.anthropic.com`` (raw HTTP). New entries must be added to
    ``_ALLOWLIST`` with reviewer sign-off.
    """
    offenders: list[tuple[str, str]] = []
    for path in _iter_backend_modules():
        rel = _relpath(path)
        if rel in _ALLOWLIST:
            continue
        source = path.read_text(encoding="utf-8")

        if _DIRECT_URL_FRAGMENT in source:
            offenders.append((rel, f"literal '{_DIRECT_URL_FRAGMENT}' found"))
            continue

        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            # If the file doesn't parse, skip it — the broader test suite will
            # catch the actual syntax error elsewhere.
            continue
        if _module_imports_anthropic(tree):
            offenders.append((rel, "imports 'anthropic' SDK"))

    if offenders:
        bullets = "\n".join(f"  - {f}: {why}" for f, why in offenders)
        raise AssertionError(
            "New LLM caller detected that does NOT route through "
            "backend.common.llm_client.anthropic_messages():\n"
            f"{bullets}\n\n"
            "All Anthropic calls must go through the budget-aware wrapper. "
            "If this caller genuinely needs direct access (e.g. a temporary "
            "diagnostic), add it to _ALLOWLIST in this test and explain why "
            "in the commit message. See project_samus_llm_token_policy."
        )


def test_allowlisted_modules_still_exist() -> None:
    """Sanity check: every allowlisted path resolves to a file.

    Stops the allowlist from rotting silently after refactors. If a file
    is moved/renamed, this test fails and the allowlist must be updated to
    match — the move itself is then visible in the diff.
    """
    root = _backend_root().parent  # Samus/
    missing = [entry for entry in _ALLOWLIST if not (root / entry).is_file()]
    assert not missing, (
        "Allowlisted LLM caller no longer exists at expected path: "
        f"{missing}. Update _ALLOWLIST in this test."
    )
