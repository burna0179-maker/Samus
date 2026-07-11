"""Fail-closed contract: unloaded registry refuses to validate."""
from __future__ import annotations

import pytest

from backend.common.codex.exceptions import CodexUnavailable
from backend.common.codex.models import ProposedAction
from backend.common.codex.registry import CodexRegistry
from backend.common.codex.validator import check_action


def test_check_action_raises_when_registry_not_loaded():
    reg = CodexRegistry()
    assert reg.is_loaded() is False
    action = ProposedAction(
        service="outreach",
        capability="run_campaign",
        action_kind="outreach_send",
        payload={"stake_sentence": "anything goes here because we never reach the rules"},
        proposed_by="outreach-workcell",
    )
    with pytest.raises(CodexUnavailable):
        check_action(action, registry=reg)


def test_failed_load_remains_unavailable_on_subsequent_calls(tmp_path):
    reg = CodexRegistry()
    with pytest.raises(CodexUnavailable):
        reg.load(tmp_path / "no-codex-here")
    action = ProposedAction(
        service="voice",
        capability="dial",
        action_kind="voice_dial",
        payload={},
        proposed_by="voice",
    )
    with pytest.raises(CodexUnavailable):
        check_action(action, registry=reg)
