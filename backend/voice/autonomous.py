"""B2 + C — Vapi mid-call adaptation bridge + the DORMANT autonomous closer /
voice_dial capability.

Both halves are coded IN PLACE but inert by default, and the autonomous DIAL
path is permanently fenced by the Codex (ADR-002 / VR-G5) regardless of any
flag. This builds the capability without activating it and without weakening
the guardrail.

B2 — mid-call adaptation (:func:`process_midcall_transcript`)
    Vapi streams live ``transcript`` / ``conversation-update`` events during a
    call. Today :func:`backend.voice.service.handle_webhook_event` audit-logs
    and drops them. When ``SAMUS_VOICE_MIDCALL_ENABLED`` is set, each final user
    turn is fed to :func:`backend.voice.adaptive.adapt` (and, when the
    autonomous closer is ALSO on, to
    :func:`backend.outreach.closer.run_closer_step` for the next *conversational*
    action — never a dial) and the recommendation is returned for recording.
    Default OFF → byte-identical to today (the caller logs + drops).

C — autonomous dial gate (:func:`attempt_autonomous_dial`)
    The capability ADR-002 forbids: autonomously INITIATING an outbound call.
    The would-dial path is coded in place but is gated by TWO independent
    fences, neither of which this module ever bypasses:
      1. ``SAMUS_AUTONOMOUS_CLOSER_ENABLED`` (default OFF) — dormant.
      2. The Codex: the attempt is routed through ``check_action`` with
         ``action_kind="voice_dial"``, which VR-G5 blocks UNCONDITIONALLY. So
         even with the flag on, the dial is refused. A Codex that is
         unavailable is itself fail-closed (refuse, never dial).
    The actual ``initiate_call`` is only ever reached if the Codex *allows* the
    action — which VR-G5 guarantees it never will. The guardrail is the
    deactivation; we keep it enforcing.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from backend.outreach import closer as _closer
from backend.voice import adaptive as _adaptive

_LOG = logging.getLogger("samus.voice.autonomous")

ENV_MIDCALL = "SAMUS_VOICE_MIDCALL_ENABLED"
ENV_AUTONOMOUS = "SAMUS_AUTONOMOUS_CLOSER_ENABLED"

# Vapi live (mid-call) message types this bridge consumes.
MIDCALL_TRANSCRIPT_TYPES: frozenset[str] = frozenset(
    {
        "transcript",
        "conversation-update",
    }
)


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on", "y")


def midcall_enabled() -> bool:
    """Read ``SAMUS_VOICE_MIDCALL_ENABLED`` live. Default OFF (dormant)."""
    return _flag(ENV_MIDCALL)


def autonomous_closer_enabled() -> bool:
    """Read ``SAMUS_AUTONOMOUS_CLOSER_ENABLED`` live. Default OFF (dormant)."""
    return _flag(ENV_AUTONOMOUS)


# ---------------------------------------------------------------------------
# B2 — mid-call adaptation bridge.
# ---------------------------------------------------------------------------


def process_midcall_transcript(
    msg: Any,
    *,
    intel: dict[str, Any] | None = None,
    current_state: str | None = None,
) -> dict[str, Any] | None:
    """Bridge a Vapi mid-call transcript event to the adaptive layer.

    Returns ``None`` (no-op) when:
      * ``SAMUS_VOICE_MIDCALL_ENABLED`` is unset (DORMANT — the default), or
      * the message is not a mid-call transcript type, or
      * the turn is empty / a non-final / non-user turn (we adapt on the
        prospect's completed speech, not the assistant's or partial fragments).

    Otherwise returns the adaptation recommendation dict. When the autonomous
    closer is ALSO enabled AND usable intel is present, the next *conversational*
    action (deliver_pitch / ask_question / attempt_close / ... — NOT a dial) is
    added under ``next_action`` / ``next_state``. Recording is the caller's job.
    """
    if not midcall_enabled():
        return None

    msg_type = getattr(msg, "type", None)
    if msg_type not in MIDCALL_TRANSCRIPT_TYPES:
        return None

    transcript = (getattr(msg, "transcript", None) or "").strip()
    if not transcript:
        return None

    # Adapt on the prospect's FINAL turns only. role / transcriptType are
    # best-effort (Vapi may omit them); when present we filter, when absent we
    # proceed (a bare transcript event is treated as a usable turn).
    role = getattr(msg, "role", None)
    if role is not None and role != "user":
        return None
    transcript_type = getattr(msg, "transcriptType", None)
    if transcript_type is not None and transcript_type != "final":
        return None

    state = (current_state or _closer.INITIAL_STATE).strip() or _closer.INITIAL_STATE
    intel = intel or {}

    adaptation = _adaptive.adapt(transcript, state, intel)
    result: dict[str, Any] = {
        "adapted": True,
        "state": state,
        "transcript_chars": len(transcript),
        **adaptation,
    }

    # Conversational next-action (NOT a dial). Only when the autonomous closer
    # is enabled and intel carries the products run_closer_step requires.
    if autonomous_closer_enabled() and isinstance(intel.get("products"), dict):
        try:
            step = _closer.run_closer_step(state, transcript, intel)
            result["next_action"] = step.get("action")
            result["next_state"] = step.get("next_state")
        except Exception as exc:  # noqa: BLE001 — never let the closer fault a webhook
            _LOG.warning("midcall closer step failed: %s", exc)

    _LOG.info(
        "midcall adapt: state=%s sentiment=%s strategy=%s next_action=%s",
        state,
        result.get("sentiment"),
        result.get("strategy"),
        result.get("next_action"),
    )
    return result


# ---------------------------------------------------------------------------
# C — autonomous dial gate (ADR-002 / VR-G5).
# ---------------------------------------------------------------------------


def attempt_autonomous_dial(
    prospect_id: str,
    *,
    phone: str | None = None,
    campaign: str | None = None,
    correlation_id: str = "",
) -> dict[str, Any]:
    """Attempt an AUTONOMOUS outbound dial — and get refused, by design.

    The capability is coded in place but is gated by two fences this function
    never bypasses:

      1. DORMANT flag ``SAMUS_AUTONOMOUS_CLOSER_ENABLED`` (default OFF): when
         unset we refuse before touching the Codex or any dialer.
      2. The Codex: we route the intent through ``check_action`` with
         ``action_kind="voice_dial"``; VR-G5 (ADR-002) blocks it
         unconditionally. The ``initiate_call`` capability is reached ONLY in
         the ``verdict.allowed`` branch — which VR-G5 guarantees is never taken.
         A Codex that is unavailable is itself fail-closed (refuse).

    Returns a result dict; ``dialed`` is ALWAYS ``False`` in practice. This
    function never weakens or skips the guardrail.
    """
    base = {
        "prospect_id": prospect_id,
        "dialed": False,
        "autonomous_enabled": autonomous_closer_enabled(),
    }

    # Fence 1 — dormant flag.
    if not autonomous_closer_enabled():
        _LOG.info("autonomous dial refused: dormant (set %s=1)", ENV_AUTONOMOUS)
        return {
            **base,
            "blocked": True,
            "rule": "DORMANT",
            "reason": f"autonomous closer dormant ({ENV_AUTONOMOUS} unset)",
        }

    # Fence 2 — Codex gate. action_kind="voice_dial" → VR-G5 (ADR-002).
    from backend.common.codex import (  # local import: codex primed at app boot
        CodexUnavailable,
        ProposedAction,
        check_action,
    )

    action = ProposedAction(
        service="voice",
        capability="voice_dial",
        action_kind="voice_dial",
        payload={
            "prospect_id": prospect_id,
            "phone": phone or "",
            "campaign": campaign or "",
        },
        proposed_by="voice.autonomous.attempt_autonomous_dial",
        correlation_id=correlation_id or None,
    )

    try:
        verdict = check_action(action)
    except CodexUnavailable as exc:
        # Cannot verify ⇒ cannot dial. Fail-closed.
        _LOG.error("autonomous dial refused: codex unavailable (%s)", exc)
        return {
            **base,
            "blocked": True,
            "rule": "CODEX_UNAVAILABLE",
            "reason": f"codex unavailable; fail-closed: {exc}",
        }

    if not verdict.allowed:
        _LOG.warning(
            "autonomous dial blocked by codex: rule=%s reason=%s",
            verdict.violated_rule_id,
            verdict.reason,
        )
        return {
            **base,
            "blocked": True,
            "rule": verdict.violated_rule_id or "VR-UNKNOWN",
            "reason": verdict.reason or "codex blocked voice_dial",
        }

    # UNREACHABLE under ADR-002: VR-G5 blocks every voice_dial. The dial
    # capability lives here, in place, behind the guardrail — if the Codex ever
    # allowed it (it must not), this is where the human-equivalent dial would
    # fire. We log loudly and STILL refuse to auto-dial: ADR-002 says the voice
    # leg produces voicemail drafts only, so even a (defective) allow must not
    # become an autonomous live call.
    _LOG.error(
        "AUTONOMOUS DIAL: codex unexpectedly ALLOWED voice_dial for prospect=%s "
        "— refusing anyway per ADR-002 (auto-dialer structurally removed).",
        prospect_id,
    )
    return {
        **base,
        "blocked": True,
        "rule": "ADR-002",
        "reason": "auto-dialer structurally removed; voicemail drafts only",
    }


def run_autonomous_closer_cycle(
    prospects: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The dormant autonomous outbound loop.

    When ``SAMUS_AUTONOMOUS_CLOSER_ENABLED`` is unset (default) this is a no-op.
    When set, it would iterate the candidate prospects and attempt a dial for
    each — every attempt is refused by :func:`attempt_autonomous_dial` (VR-G5).
    The loop scaffold exists so the autonomous architecture is in place; it can
    never place a live call.
    """
    if not autonomous_closer_enabled():
        return {"ran": False, "reason": f"dormant ({ENV_AUTONOMOUS} unset)", "attempts": []}

    attempts = [
        attempt_autonomous_dial(
            str(p.get("prospect_id") or ""),
            phone=p.get("phone"),
            campaign=p.get("campaign"),
        )
        for p in (prospects or [])
    ]
    return {
        "ran": True,
        "dialed_count": sum(1 for a in attempts if a.get("dialed")),  # always 0
        "blocked_count": sum(1 for a in attempts if a.get("blocked")),
        "attempts": attempts,
    }


__all__ = [
    "ENV_MIDCALL",
    "ENV_AUTONOMOUS",
    "MIDCALL_TRANSCRIPT_TYPES",
    "midcall_enabled",
    "autonomous_closer_enabled",
    "process_midcall_transcript",
    "attempt_autonomous_dial",
    "run_autonomous_closer_cycle",
]
