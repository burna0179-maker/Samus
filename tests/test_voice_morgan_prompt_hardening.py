"""Morgan (Vapi SDR) prompt-hardening + snapshot-sync guards.

Covers the P0 fix for the 2026-07-02 live-call leak, where Morgan narrated its
own protocol/reasoning aloud to an answering-machine MENU that Vapi's voicemail
detector missed. Two concerns:

1. The reviewed snapshot (morgan_sdr.json) must carry the high-priority
   automated-system/IVR/voicemail rule and the banned self-narration patterns,
   so the wording that closes the leak is present in the source of truth.

2. backend.voice.sync_assistant must build a MINIMAL, correct PATCH body from
   that snapshot, be a DRY-RUN by default (send nothing), and require --apply
   before it PATCHes the live assistant.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_SNAPSHOT = (
    Path(__file__).resolve().parents[1]
    / "backend" / "voice" / "assistant_configs" / "morgan_sdr.json"
)

# The canonical banned self-narration / meta-reasoning phrases from the
# 2026-07-02 live-call leak. This list is the SINGLE SOURCE OF TRUTH for both
# the prompt-hardening guard below AND the post-batch regression guard in
# backend.voice.call_batch_analyzer (which scans real transcripts for any of
# these leaking out of Morgan's mouth). Keeping one list means the two guards
# can never drift: if the prompt bans a phrase, the batch analyzer flags it.
BANNED_SELF_NARRATION: tuple[str, ...] = (
    "this is a voicemail system prompt",
    "this is a voicemail",
    "this is an automated menu",
    "according to my voicemail script",
    "as instructed in my protocol",
    "per my protocol",
    "I need to",
    "I should",
    "you never think out loud",
    # 2026-07-02 batch #2 leak — Morgan narrated its voicemail protocol aloud
    # ("I need to hang up and leave the voice mail as instructed in my
    # protocol. According to my voice mail script, I should have left the exact
    # message.") before speaking the actual message. Ban the specific
    # narration fragments that "I need to" / "I should" alone don't cover.
    "I need to hang up",
    "leave the voice mail as instructed",
    "I should have left",
)


def _system_prompt() -> str:
    data = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    for msg in data["model"]["messages"]:
        if msg.get("role") == "system":
            return msg["content"]
    raise AssertionError("no system message in snapshot")


# ---------------------------------------------------------------------------
# 1. Snapshot prompt content
# ---------------------------------------------------------------------------

def test_snapshot_is_valid_json_and_ids_intact():
    data = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    assert data["id"] == "9538050d-1d94-415b-a537-e59fc4039bc1"
    # firstMessage + voicemailMessage untouched by the hardening.
    assert data["firstMessage"].startswith("Oh, hey")
    assert data["voicemailMessage"].startswith("Hey, it's Morgan from HustleForge. Sorry I missed you")
    # Template vars preserved.
    prompt = _system_prompt()
    for var in ("{{callsheet_voicemail}}", "{{company_name}}", "{{industry}}", "{{city}}"):
        assert var in prompt, f"template var {var} missing from prompt"


def test_prompt_has_automated_system_rule():
    prompt = _system_prompt()
    # The dedicated high-priority section exists.
    assert "AUTOMATED-SYSTEM / IVR / VOICEMAIL HANDLING" in prompt
    # It covers the exact edge case Vapi's detector missed: an interactive menu.
    assert "MENU" in prompt
    assert "press 1" in prompt
    # It names the two — and only two — permitted behaviors.
    assert "EXACTLY TWO permitted behaviors" in prompt
    # It explicitly forbids navigating / pressing keys on a menu.
    assert "press keys" in prompt or "attempt DTMF" in prompt
    # It states this is a critical failure.
    assert "CRITICAL FAILURE" in prompt


@pytest.mark.parametrize("banned", BANNED_SELF_NARRATION)
def test_prompt_bans_self_narration_patterns(banned):
    """Each meta-reasoning phrase from the live leak must be named as banned."""
    prompt = _system_prompt().lower()
    assert banned.lower() in prompt, (
        f"banned self-narration pattern {banned!r} not called out in the prompt"
    )


def test_output_contract_bans_thinking_out_loud():
    """The OUTPUT CONTRACT itself (rule 6) must forbid spoken self-narration."""
    prompt = _system_prompt()
    # Rule 6 added to the numbered OUTPUT CONTRACT list.
    assert "NEVER voice your own reasoning, meta-commentary, or self-narration" in prompt


# ---------------------------------------------------------------------------
# Gatekeeper handling (the 2026-07-02 receptionist-pitch leak)
# ---------------------------------------------------------------------------

def test_prompt_has_gatekeeper_section():
    """A dedicated GATEKEEPER HANDLING section exists, distinct from the
    post-call reclassifier and from the mid-call WRONG-PERSON handoff rule."""
    prompt = _system_prompt()
    assert "GATEKEEPER HANDLING" in prompt
    # Detects the exact greetings from the live audit.
    assert "how can i help you" in prompt.lower()
    assert "this is" in prompt.lower()
    # Names the roles that constitute a gatekeeper.
    low = prompt.lower()
    assert "receptionist" in low
    assert "front desk" in low or "front-desk" in low


def test_gatekeeper_rule_forbids_pitching_the_gatekeeper():
    """Morgan must NOT deliver the SEO / audit / security-warning pitch to a
    gatekeeper — the specific failure in the 2026-07-02 call audit."""
    prompt = _system_prompt()
    # The section explicitly forbids delivering the SEO/audit pitch to them.
    assert "must NOT deliver the SEO" in prompt
    # And forbids running the pressure ladder on them.
    assert "NEVER pressure, re-pitch, or run the consequence ladder on a gatekeeper" in prompt


def test_gatekeeper_rule_asks_for_the_decision_maker():
    """When talking to a gatekeeper Morgan asks to reach the owner / the person
    who handles the website & marketing, and uses {{owner_name}} by name."""
    prompt = _system_prompt()
    low = prompt.lower()
    # Asks for the person who handles the website / marketing.
    assert "who handles the website" in low
    # Uses the owner_name template variable (by name when populated).
    assert "{{owner_name}}" in prompt
    # Empty-safe fallback wording present for when owner_name is blank.
    assert "the owner, or whoever handles your website" in prompt


def test_gatekeeper_only_pitch_once_decision_maker_confirmed():
    """The real pitch flow runs only once Morgan is reasonably sure it's the
    owner / decision-maker."""
    prompt = _system_prompt()
    assert "reasonably sure you have the owner" in prompt or (
        "reasonably sure it's the owner" in prompt
    )


def test_gatekeeper_section_is_tts_safe():
    """The gatekeeper section obeys the OUTPUT CONTRACT: spoken lines are marked
    SPEAK:, and directions are marked INSTRUCTION (never voiced)."""
    prompt = _system_prompt()
    # Slice just the gatekeeper section for a focused check.
    start = prompt.index("GATEKEEPER HANDLING")
    end = prompt.index("You are Morgan, an SDR at HustleForge.")
    section = prompt[start:end]
    assert "SPEAK:" in section
    assert "INSTRUCTION (do not speak):" in section


def test_gatekeeper_change_did_not_touch_protected_regions():
    """The opener (firstMessage), the AUTOMATED-SYSTEM section, and the OUTPUT
    CONTRACT are unchanged by the gatekeeper edit."""
    data = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    assert data["firstMessage"].startswith("Oh, hey")
    prompt = _system_prompt()
    assert prompt.startswith("OUTPUT CONTRACT — READ FIRST, OBEY ABOVE ALL ELSE.")
    assert "AUTOMATED-SYSTEM / IVR / VOICEMAIL HANDLING — HIGHEST PRIORITY" in prompt
    # The gatekeeper section is inserted AFTER the automated-system section and
    # BEFORE the persona line — never inside the OUTPUT CONTRACT.
    assert prompt.index("AUTOMATED-SYSTEM / IVR / VOICEMAIL HANDLING") < prompt.index(
        "GATEKEEPER HANDLING"
    )
    assert prompt.index("GATEKEEPER HANDLING") < prompt.index(
        "You are Morgan, an SDR at HustleForge."
    )


def test_voicemail_detection_not_weakened():
    """Config-level: existing voicemail detection stays intact (not removed/weakened)."""
    data = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    vd = data.get("voicemailDetection")
    assert isinstance(vd, dict) and vd.get("provider") == "vapi"
    # A clean, non-empty voicemail script is still configured.
    assert data["voicemailMessage"].strip()


# ---------------------------------------------------------------------------
# Gatekeeper-aware opener + withheld-finding flow (round-2 fix 2026-07-02)
# ---------------------------------------------------------------------------

def test_first_message_is_gatekeeper_aware_and_asks_for_the_owner():
    """The static firstMessage now asks for the owner as its close (honest
    cold-call + value teaser, no specific finding, no time-permission ask)."""
    data = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    fm = data["firstMessage"]
    low = fm.lower()
    # Kept the 'Oh, hey' human open (protected-region assertion elsewhere).
    assert fm.startswith("Oh, hey")
    # Honest cold-call teaser.
    assert "cold call" in low
    # Asks for the owner / decision-maker.
    assert "owner" in low
    assert "who handles" in low or "whoever handles" in low
    # No banned time-permission ask.
    assert "thirty seconds" not in low
    assert "do you have a minute" not in low
    # No specific finding leaked in the opener.
    assert "security warning" not in low
    assert "out of a hundred" not in low


def test_prompt_exposes_callsheet_finding_variable():
    """The specific finding rides as its own {{callsheet_finding}} var so the
    prompt can reveal it to the owner without the opener carrying it."""
    prompt = _system_prompt()
    assert "{{callsheet_finding}}" in prompt


def test_prompt_reveals_finding_only_after_owner_confirmed():
    """Step 2 (the finding reveal) speaks {{callsheet_finding}} and is gated on
    talking to the owner — never to a gatekeeper. This is the core of the
    round-2 fix: the specific finding + pitch don't land until the decision-
    maker is on the line."""
    prompt = _system_prompt()
    # The reveal beat uses the specific-finding variable, not the coarse issues.
    step2 = prompt[prompt.index("Step 2"):prompt.index("Step 3")]
    assert "{{callsheet_finding}}" in step2
    # And it is explicitly gated on the owner / decision-maker.
    low = step2.lower()
    assert "owner" in low and ("only" in low or "not until" in low or "do not speak it until" in low)
    # It must NOT be revealed to a gatekeeper.
    assert "gatekeeper" in low


def test_opener_section_routes_to_decision_maker_first():
    """The OPENER section is now gatekeeper-aware: it routes on WHO answered
    (gatekeeper handoff vs owner-confirm) instead of a fixed finding-first
    pitch, and it does NOT re-ask 'are you the owner' as a separate beat."""
    prompt = _system_prompt()
    opener = prompt[prompt.index("# OPENER"):prompt.index("# AFTER PERMISSION")]
    low = opener.lower()
    # Routes explicitly on gatekeeper vs owner.
    assert "gatekeeper" in low
    assert "owner" in low
    # The opener already asked for the owner — the section says so.
    assert "already asked" in low
    # It still holds the finding back at the opener.
    assert "{{callsheet_finding}}" in opener


def test_gatekeeper_section_does_not_double_ask_for_owner():
    """The gatekeeper handoff is consistent with the opener already asking for
    the owner — it must not instruct a second full owner-ask that caused the
    stumble."""
    prompt = _system_prompt()
    start = prompt.index("GATEKEEPER HANDLING")
    end = prompt.index("You are Morgan, an SDR at HustleForge.")
    section = prompt[start:end].lower()
    # The section acknowledges the opener already made the ask.
    assert "already asked" in section
    # And still forbids revealing the specific finding to a gatekeeper.
    assert "{{callsheet_finding}}" in prompt[start:end]


# ---------------------------------------------------------------------------
# 2. sync_assistant module
# ---------------------------------------------------------------------------

def test_build_patch_body_prompt_only():
    from backend.voice import sync_assistant as sa
    snap = sa.load_snapshot()
    body = sa.build_patch_body(snap, include_voicemail=False)
    # Only the model.messages system prompt — nothing else.
    assert set(body.keys()) == {"model"}
    msg = body["model"]["messages"][0]
    assert msg["role"] == "system"
    assert "AUTOMATED-SYSTEM / IVR / VOICEMAIL HANDLING" in msg["content"]
    # Never leaks server/voice/transcriber into the patch.
    for forbidden in ("server", "voice", "transcriber", "firstMessage"):
        assert forbidden not in body


def test_build_patch_body_with_voicemail():
    from backend.voice import sync_assistant as sa
    snap = sa.load_snapshot()
    body = sa.build_patch_body(snap, include_voicemail=True)
    assert "model" in body
    assert body["voicemailMessage"].startswith("Hey, it's Morgan")
    assert body["voicemailDetection"]["provider"] == "vapi"
    # Still never server/voice/transcriber.
    for forbidden in ("server", "voice", "transcriber"):
        assert forbidden not in body


class _FakeClient:
    """Records calls; never touches the network."""

    def __init__(self, *, live_prompt: str = "OLD PROMPT"):
        self._live_prompt = live_prompt
        self.patched: list[tuple[str, dict]] = []
        self.raw_patches: list[tuple[str, dict]] = []

    def get_assistant(self, assistant_id):
        return {
            "id": assistant_id,
            "model": {"messages": [{"role": "system", "content": self._live_prompt}]},
        }

    def patch_assistant_config(self, assistant_id, *, system_prompt=None, **kw):
        self.patched.append((assistant_id, {"system_prompt": system_prompt, **kw}))
        return {"id": assistant_id}

    def _patch(self, path, body):
        self.raw_patches.append((path, body))
        return {}


def _wire(monkeypatch, fake_client, *, api_key="k", assistant_id="9538050d"):
    from backend.voice import sync_assistant as sa

    class _S:
        vapi_api_key = api_key
        vapi_assistant_id = assistant_id

    monkeypatch.setattr(sa, "get_settings", lambda: _S())
    monkeypatch.setattr(sa, "VapiClient", lambda api_key: fake_client)
    return sa


def test_dry_run_sends_nothing(monkeypatch, capsys):
    fake = _FakeClient()
    sa = _wire(monkeypatch, fake)
    rc = sa.sync(apply=False, include_voicemail=False)
    assert rc == 0
    # No PATCH of any kind was issued.
    assert fake.patched == []
    assert fake.raw_patches == []
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    # Diff shows the reviewed rule being added.
    assert "AUTOMATED-SYSTEM" in out


def test_apply_patches_system_prompt(monkeypatch, capsys):
    fake = _FakeClient()
    sa = _wire(monkeypatch, fake)
    rc = sa.sync(apply=True, include_voicemail=False)
    assert rc == 0
    # Model is sent as a FULL-object PATCH via _patch (never the messages-only
    # patch_assistant_config, which Vapi 400s: "model.provider must be one of").
    assert fake.patched == []
    assert len(fake.raw_patches) == 1
    path, body = fake.raw_patches[0]
    assert path == "/assistant/9538050d"
    assert body["model"]["provider"] == "anthropic"
    assert body["model"]["model"]  # model name present so Vapi accepts it
    assert "AUTOMATED-SYSTEM / IVR / VOICEMAIL HANDLING" in body["model"]["messages"][0]["content"]
    # prompt-only sync carries no voicemail fields.
    assert "voicemailMessage" not in body and "voicemailDetection" not in body
    assert "APPLIED" in capsys.readouterr().out


def test_apply_with_voicemail_patches_voicemail_fields(monkeypatch):
    fake = _FakeClient()
    sa = _wire(monkeypatch, fake)
    rc = sa.sync(apply=True, include_voicemail=True)
    assert rc == 0
    # One combined _patch carries the full model + voicemail fields.
    assert fake.patched == []
    assert len(fake.raw_patches) == 1
    path, body = fake.raw_patches[0]
    assert path == "/assistant/9538050d"
    assert body["model"]["provider"] == "anthropic"
    assert body["voicemailMessage"].startswith("Hey, it's Morgan")
    assert body["voicemailDetection"]["provider"] == "vapi"
    # Never carry server/voice/transcriber — the tunnel binding stays put.
    for forbidden in ("server", "voice", "transcriber"):
        assert forbidden not in body


def test_apply_required_gate(monkeypatch):
    """The exact gate: only apply=True issues a PATCH; apply=False issues none."""
    fake_dry = _FakeClient()
    sa = _wire(monkeypatch, fake_dry)
    sa.sync(apply=False, include_voicemail=True)
    assert fake_dry.patched == [] and fake_dry.raw_patches == []

    fake_live = _FakeClient()
    sa = _wire(monkeypatch, fake_live)
    sa.sync(apply=True, include_voicemail=True)
    assert len(fake_live.raw_patches) == 1


def test_missing_api_key_refuses(monkeypatch, capsys):
    fake = _FakeClient()
    sa = _wire(monkeypatch, fake, api_key="")
    rc = sa.sync(apply=True, include_voicemail=False)
    assert rc == 2
    assert fake.patched == []
    assert "VAPI_API_KEY is not set" in capsys.readouterr().err


def test_missing_assistant_id_refuses(monkeypatch, capsys):
    fake = _FakeClient()
    sa = _wire(monkeypatch, fake, assistant_id="")
    rc = sa.sync(apply=True, include_voicemail=False)
    assert rc == 2
    assert fake.patched == []
    assert "VAPI_ASSISTANT_ID is not set" in capsys.readouterr().err
