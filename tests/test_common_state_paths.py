"""Coverage for backend.common.state_paths — the writable state-root resolver.

The container image root (/opt/samus) is read-only; only the samus-data
volume at /opt/samus/data is writable. This resolver routes durable
governance/observability state to SAMUS_STATE_ROOT (set to the volume in
compose) and falls back to <code root>/state on the host so test + host-venv
behaviour is unchanged.
"""
from __future__ import annotations

from pathlib import Path

from backend.common import state_paths
from backend.common.state_paths import state_path, state_root


def test_default_falls_back_to_code_root_state(monkeypatch):
    monkeypatch.delenv("SAMUS_STATE_ROOT", raising=False)
    root = state_root()
    # <code root>/state — the prior on-host behaviour, preserved.
    assert root.name == "state"
    assert root == state_paths._CODE_ROOT / "state"


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("SAMUS_STATE_ROOT", "/opt/samus/data/state")
    assert state_root() == Path("/opt/samus/data/state")


def test_blank_env_falls_back(monkeypatch):
    # An empty/whitespace value must NOT shadow the fallback.
    monkeypatch.setenv("SAMUS_STATE_ROOT", "   ")
    assert state_root() == state_paths._CODE_ROOT / "state"


def test_state_path_joins_under_override(monkeypatch):
    monkeypatch.setenv("SAMUS_STATE_ROOT", "/opt/samus/data/state")
    assert state_path("value_exchanges").as_posix() == "/opt/samus/data/state/value_exchanges"
    assert state_path("pdc", "findings").as_posix() == "/opt/samus/data/state/pdc/findings"


def test_state_path_is_resolved_live_not_cached(monkeypatch):
    # Resolution reads the env on each call (no import-time freeze), so a
    # later override is honoured.
    monkeypatch.delenv("SAMUS_STATE_ROOT", raising=False)
    before = state_path("rbl")
    monkeypatch.setenv("SAMUS_STATE_ROOT", "/opt/samus/data/state")
    after = state_path("rbl")
    assert before != after
    assert after.as_posix() == "/opt/samus/data/state/rbl"
