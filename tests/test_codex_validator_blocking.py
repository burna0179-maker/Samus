"""Blocking-rule coverage for the Codex Validation Layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.common.codex import adr_drafter
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


@pytest.fixture
def patch_drafts_dir(monkeypatch, tmp_path: Path) -> Path:
    drafts = tmp_path / "_drafts"

    original = adr_drafter.draft_adr_for_violation

    def _wrapped(action, violated_rule_id, reason, registry, drafts_dir=None):
        return original(
            action,
            violated_rule_id,
            reason,
            registry,
            drafts_dir=drafts_dir or drafts,
        )

    # The validator imports the function directly into its own module, so
    # patch BOTH the source and the validator-module reference.
    monkeypatch.setattr(adr_drafter, "draft_adr_for_violation", _wrapped)
    from backend.common.codex import validator as _v

    monkeypatch.setattr(_v, "draft_adr_for_violation", _wrapped)
    return drafts


def _verdict_for(action: ProposedAction, reg: CodexRegistry):
    return check_action(action, registry=reg)


def test_vr_g5_blocks_voice_dial(loaded_registry, patch_drafts_dir):
    action = ProposedAction(
        service="voice",
        capability="dispatch_call",
        action_kind="voice_dial",
        payload={"number": "+15555550000"},
        proposed_by="voice-workcell",
        correlation_id="corr-1",
    )
    verdict = _verdict_for(action, loaded_registry)
    assert verdict.allowed is False
    assert verdict.violated_rule_id == "VR-G5"
    assert verdict.drafted_adr_path
    assert Path(verdict.drafted_adr_path).is_file()


def test_vr_adr_003_blocks_subscription_create(loaded_registry, patch_drafts_dir):
    action = ProposedAction(
        service="finance",
        capability="create_subscription",
        action_kind="subscription_create",
        payload={"plan": "posture-monitor-39"},
        proposed_by="finance-workcell",
    )
    verdict = _verdict_for(action, loaded_registry)
    assert verdict.allowed is False
    assert verdict.violated_rule_id == "VR-ADR-003"
    assert Path(verdict.drafted_adr_path).is_file()


def test_vr_g1_blocks_outreach_without_stake_sentence(loaded_registry, patch_drafts_dir):
    action = ProposedAction(
        service="outreach",
        capability="run_campaign",
        action_kind="outreach_send",
        payload={
            "to": "ceo@example.com",
            "subject": "Quick note",
            "body": "Hi there.",
        },
        proposed_by="outreach-workcell",
    )
    verdict = _verdict_for(action, loaded_registry)
    assert verdict.allowed is False
    assert verdict.violated_rule_id == "VR-G1"
    assert Path(verdict.drafted_adr_path).is_file()


def test_vr_g2_blocks_banned_phrase_in_user_text(loaded_registry, patch_drafts_dir):
    action = ProposedAction(
        service="outreach",
        capability="compose_body",
        action_kind="outreach_send",
        payload={
            "stake_sentence": (
                "I hope this finds you well — saw your Yuba City storefront on "
                "the way to Beale AFB last Tuesday."
            ),
            "subject": "Hi",
            "body": "We help businesses like yours.",
        },
        proposed_by="outreach-workcell",
    )
    verdict = _verdict_for(action, loaded_registry)
    assert verdict.allowed is False
    # Multiple banned phrases trip; alphabetic primary is VR-G2.
    assert verdict.violated_rule_id == "VR-G2"
    assert Path(verdict.drafted_adr_path).is_file()


def test_vr_adr_008_blocks_banned_phrase_list_mutation(
    loaded_registry,
    patch_drafts_dir,
):
    action = ProposedAction(
        service="admin",
        capability="mutate_constants",
        action_kind="other",
        payload={
            "target": "STAKE_SENTENCE_BANNED_PHRASES",
            "op": "remove",
            "phrase": "we help businesses",
        },
        proposed_by="admin-tooling",
    )
    verdict = _verdict_for(action, loaded_registry)
    assert verdict.allowed is False
    assert verdict.violated_rule_id == "VR-ADR-008"
    assert Path(verdict.drafted_adr_path).is_file()


# ---------------------------------------------------------------------------
# ADR-016 governed autonomous dial policy — voice_dial conditional allow
# ---------------------------------------------------------------------------


def _arm(monkeypatch, on: bool) -> None:
    """Arm/disarm the governed dial flag via env + settings reload (real path)."""
    from backend.common.settings import reload_settings

    if on:
        monkeypatch.setenv("SAMUS_GOVERNED_AUTONOMOUS_DIAL_ENABLED", "true")
    else:
        monkeypatch.delenv("SAMUS_GOVERNED_AUTONOMOUS_DIAL_ENABLED", raising=False)
    reload_settings()


def _governed_dial(**over) -> ProposedAction:
    payload = {
        "policy": "governed_autonomous_dial",
        "stake_sentence": "Alex flagged Acme because their trust grade is F.",
        "within_call_hours": True,
        "cooldown_ok": True,
        "under_daily_cap": True,
        "dnc_ok": True,
        "consent_ok": True,
        "prospect_id": "p1",
        "phone": "+15555550000",
    }
    payload.update(over)
    return ProposedAction(
        service="voice",
        capability="voice_dial",
        action_kind="voice_dial",
        payload=payload,
        proposed_by="voice.autonomous.governed",
        correlation_id="c1",
    )


def test_governed_dial_blocked_when_flag_off(loaded_registry, patch_drafts_dir, monkeypatch):
    _arm(monkeypatch, on=False)  # default posture
    v = _verdict_for(_governed_dial(), loaded_registry)  # fully attested but unarmed
    assert v.allowed is False
    assert v.violated_rule_id == "VR-G5"


def test_governed_dial_allowed_when_armed_and_attested(
    loaded_registry, patch_drafts_dir, monkeypatch
):
    _arm(monkeypatch, on=True)
    v = _verdict_for(_governed_dial(), loaded_registry)
    assert v.allowed is True


def test_governed_dial_blocked_missing_stake(loaded_registry, patch_drafts_dir, monkeypatch):
    _arm(monkeypatch, on=True)
    v = _verdict_for(_governed_dial(stake_sentence=""), loaded_registry)
    assert v.allowed is False
    assert v.violated_rule_id == "VR-G5"


def test_governed_dial_blocked_wrong_policy(loaded_registry, patch_drafts_dir, monkeypatch):
    _arm(monkeypatch, on=True)
    v = _verdict_for(_governed_dial(policy="something_else"), loaded_registry)
    assert v.allowed is False


@pytest.mark.parametrize(
    "fence", ["within_call_hours", "cooldown_ok", "under_daily_cap", "dnc_ok", "consent_ok"]
)
def test_governed_dial_blocked_when_any_fence_false(
    fence, loaded_registry, patch_drafts_dir, monkeypatch
):
    _arm(monkeypatch, on=True)
    v = _verdict_for(_governed_dial(**{fence: False}), loaded_registry)
    assert v.allowed is False, f"fence {fence}=False must block"


@pytest.mark.parametrize(
    "fence", ["within_call_hours", "cooldown_ok", "under_daily_cap", "dnc_ok", "consent_ok"]
)
def test_governed_dial_blocked_when_any_fence_missing(
    fence, loaded_registry, patch_drafts_dir, monkeypatch
):
    _arm(monkeypatch, on=True)
    payload_missing = _governed_dial()
    del payload_missing.payload[fence]  # attestation absent, not just False
    v = _verdict_for(payload_missing, loaded_registry)
    assert v.allowed is False, f"missing fence {fence} must block (default-closed)"


def test_governed_dial_blocked_when_no_consent(loaded_registry, patch_drafts_dir, monkeypatch):
    """ADR-017: a fully-fenced, armed, attested dial to a NON-consented number is
    still blocked — consent is a policy-level precondition, not a courtesy."""
    _arm(monkeypatch, on=True)
    v = _verdict_for(_governed_dial(consent_ok=False), loaded_registry)
    assert v.allowed is False
    assert v.violated_rule_id == "VR-G5"


def test_governed_dial_blocked_when_consent_missing(loaded_registry, patch_drafts_dir, monkeypatch):
    _arm(monkeypatch, on=True)
    action = _governed_dial()
    del action.payload["consent_ok"]  # attestation absent -> default-closed
    v = _verdict_for(action, loaded_registry)
    assert v.allowed is False


def test_governed_dial_banned_phrase_still_blocks(loaded_registry, patch_drafts_dir, monkeypatch):
    _arm(monkeypatch, on=True)
    banned = list(loaded_registry.banned_phrases())
    if not banned:
        pytest.skip("no banned phrases in registry")
    v = _verdict_for(
        _governed_dial(stake_sentence=f"Hi there {banned[0]} deal now"), loaded_registry
    )
    assert v.allowed is False  # G2 fires independently even under the governed policy
