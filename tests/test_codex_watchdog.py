"""Watcher tests — polling reload + fail-OPEN on malformed change."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from backend.common.codex.registry import CodexRegistry
from backend.common.codex.watchdog import (
    start_codex_watcher,
    stop_codex_watcher,
)


_REPO_ROOT = Path(__file__).resolve().parents[1]
_LIVE_CODEX = _REPO_ROOT / "docs" / "codex"


@pytest.fixture
def codex_copy(tmp_path: Path) -> Path:
    target = tmp_path / "codex"
    shutil.copytree(_LIVE_CODEX, target)
    return target


def _wait_for(predicate, *, timeout: float = 5.0, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def test_watcher_reloads_on_change(codex_copy: Path, monkeypatch):
    monkeypatch.setenv("SAMUS_CODEX_AUTO_RELOAD", "1")
    registry = CodexRegistry()
    registry.load(codex_copy)
    initial_reloads = 0
    handle = start_codex_watcher(
        codex_copy,
        registry=registry,
        poll_interval=0.1,
        force=True,
    )
    assert handle is not None
    try:
        glossary = codex_copy / "10_glossary.md"
        text = glossary.read_text(encoding="utf-8")
        glossary.write_text(text + "\n", encoding="utf-8")
        assert _wait_for(lambda: handle.reload_count > initial_reloads, timeout=5.0)
        assert handle.last_error is None
    finally:
        stop_codex_watcher(handle)


def test_watcher_fails_open_on_malformed_change(codex_copy: Path, monkeypatch):
    monkeypatch.setenv("SAMUS_CODEX_AUTO_RELOAD", "1")
    registry = CodexRegistry()
    registry.load(codex_copy)
    pre_guardrails = len(registry.guardrails())
    handle = start_codex_watcher(
        codex_copy,
        registry=registry,
        poll_interval=0.1,
        force=True,
    )
    assert handle is not None
    try:
        guardrails = codex_copy / "04_guardrails.md"
        guardrails.write_text("INTENTIONALLY BROKEN\n", encoding="utf-8")
        assert _wait_for(lambda: handle.last_error is not None, timeout=5.0)
        # Fail-OPEN: previous registry still serves the old rule count.
        assert registry.is_loaded() is True
        assert len(registry.guardrails()) == pre_guardrails
    finally:
        stop_codex_watcher(handle)


def test_watcher_disabled_via_env(codex_copy: Path, monkeypatch):
    monkeypatch.setenv("SAMUS_CODEX_AUTO_RELOAD", "0")
    handle = start_codex_watcher(codex_copy, poll_interval=0.1)
    assert handle is None


def test_watcher_clean_stop(codex_copy: Path, monkeypatch):
    monkeypatch.setenv("SAMUS_CODEX_AUTO_RELOAD", "1")
    registry = CodexRegistry()
    registry.load(codex_copy)
    handle = start_codex_watcher(
        codex_copy,
        registry=registry,
        poll_interval=0.1,
        force=True,
    )
    assert handle is not None
    stop_codex_watcher(handle, timeout=3.0)
    assert handle.is_alive() is False
