"""Coverage for G6/G7/G8 — flipped to blocking in ADR-012 (2026-05-30).

Pre-flip these rules emitted VW-* warnings. Post-flip they emit VR-G6/G7/G8
blocking verdicts. The "missing" payload now refuses; the "present" payload
passes clean (no warnings, allowed=True).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.common.codex.models import ProposedAction
from backend.common.codex.registry import CodexRegistry
from backend.common.codex.validator import check_action


REPO_ROOT = Path(__file__).resolve().parents[1]
CODEX_DIR = REPO_ROOT / "docs" / "codex"


@pytest.fixture
def loaded_registry() -> CodexRegistry:
    reg = CodexRegistry()
    reg.load(CODEX_DIR)
    return reg


def test_vr_g6_blocks_gap_report_without_evidence_sources(loaded_registry):
    action = ProposedAction(
        service="seo",
        capability="render_report",
        action_kind="gap_report_render",
        payload={"findings": [{"text": "missing HSTS"}]},
        proposed_by="seo-workcell",
    )
    verdict = check_action(action, registry=loaded_registry)
    assert verdict.allowed is False
    assert verdict.violated_rule_id == "VR-G6"


def test_vr_g6_silent_when_evidence_sources_present(loaded_registry):
    action = ProposedAction(
        service="seo",
        capability="render_report",
        action_kind="gap_report_render",
        payload={
            "findings": [{"text": "missing HSTS"}],
            "evidence_sources": ["crawled_header"],
        },
        proposed_by="seo-workcell",
    )
    verdict = check_action(action, registry=loaded_registry)
    assert verdict.allowed is True
    assert verdict.warnings == []


def test_vr_g7_blocks_reward_update_without_subtracts_harm(loaded_registry):
    action = ProposedAction(
        service="crm",
        capability="update_reward_function",
        action_kind="reward_function_update",
        payload={"k_coefficient": 0.5},
        proposed_by="crm-workcell",
    )
    verdict = check_action(action, registry=loaded_registry)
    assert verdict.allowed is False
    assert verdict.violated_rule_id == "VR-G7"


def test_vr_g7_silent_when_subtracts_harm_true(loaded_registry):
    action = ProposedAction(
        service="crm",
        capability="update_reward_function",
        action_kind="reward_function_update",
        payload={"subtracts_harm": True, "k_coefficient": 0.5},
        proposed_by="crm-workcell",
    )
    verdict = check_action(action, registry=loaded_registry)
    assert verdict.allowed is True
    assert verdict.warnings == []


def test_vr_g8_blocks_outreach_without_legitimacy_signal(loaded_registry):
    action = ProposedAction(
        service="outreach",
        capability="run_campaign",
        action_kind="outreach_send",
        payload={
            "stake_sentence": (
                "Saw the Marysville City Council minutes naming your shop for "
                "the levee-district contract — wanted you to see this."
            ),
        },
        proposed_by="outreach-workcell",
    )
    verdict = check_action(action, registry=loaded_registry)
    assert verdict.allowed is False
    assert verdict.violated_rule_id == "VR-G8"


def test_vr_g8_silent_when_legitimacy_signal_present(loaded_registry):
    action = ProposedAction(
        service="outreach",
        capability="run_campaign",
        action_kind="outreach_send",
        payload={
            "stake_sentence": (
                "Saw the Marysville City Council minutes naming your shop for "
                "the levee-district contract — wanted you to see this."
            ),
            "legitimacy_signal": "public_rfp",
        },
        proposed_by="outreach-workcell",
    )
    verdict = check_action(action, registry=loaded_registry)
    assert verdict.allowed is True
    assert verdict.warnings == []
