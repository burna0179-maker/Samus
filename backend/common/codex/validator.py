"""Codex rule validator — the public `check_action` gate.

V1.1 ruleset (do not extend without a Codex ADR):

Blocking (Verdict.allowed=False, auto-drafts ADR stub):
  VR-G5      — voice_dial action_kind                            (ADR-002 / G5)
  VR-ADR-003 — subscription_create action_kind                   (ADR-003)
  VR-G1      — outreach_send without payload.stake_sentence      (G1)
  VR-G2      — banned phrase in stake_sentence/subject/body/...  (G2)
  VR-G6      — gap_report_render without evidence_sources         (G6, flipped 2026-05-30)
  VR-G7      — reward_function_update without subtracts_harm=True (G7, flipped 2026-05-30)
  VR-G8      — outreach_send without legitimacy_signal            (G8, flipped 2026-05-30)
  VR-ADR-008 — payload mutating STAKE_SENTENCE_BANNED_PHRASES    (ADR-008)

ADR-012 (2026-05-30) flipped G6/G7/G8 from advisory to blocking after the
backing implementations shipped: evidence_source enum + report serialization
filter (G6), harm-subtracting reward formula (G7), legitimacy filter +
needs_warm_path queue (G8).

Warning (Verdict.allowed=True, advisory only):
  — (no advisory-only rules in v1.1; all intent-only guardrails now enforced)

Operational contract:
  * Registry not loaded -> CodexUnavailable (fail-closed; refuses to validate).
  * Multiple violations collected; primary = alphabetic-first rule id, the
    rest surface in Verdict.warnings for visibility.
  * Blocking-rule path writes an ADR stub via adr_drafter. If the write
    fails the action stays blocked — losing the audit trail is itself a
    safety failure, so the verdict does not flip to allow.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .adr_drafter import draft_adr_for_violation
from .exceptions import CodexUnavailable
from .models import ProposedAction, Verdict
from .registry import REGISTRY, CodexRegistry


_LOG = logging.getLogger("samus.codex.validator")


_USER_TEXT_FIELDS = (
    "stake_sentence",
    "subject",
    "body",
    "voicemail_script",
    "opener",
)


def _get_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _is_missing(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (list, tuple, dict, set)) and not value:
        return True
    return False


def _iter_user_text_fields(payload: dict[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    lowered_targets = {name.lower() for name in _USER_TEXT_FIELDS}
    for key, value in payload.items():
        if not isinstance(key, str):
            continue
        if key.lower() not in lowered_targets:
            continue
        if isinstance(value, str) and value:
            out.append((key, value))
    return out


def _governed_dial_permitted(action: ProposedAction) -> bool:
    """ADR-016: permit voice_dial ONLY under the armed, fully-attested governed
    autonomous dial policy. DEFAULT-CLOSED — any missing/false condition returns
    False, so the VR-G5 block stands exactly as it did before ADR-016.

    Conditions (all required): the ``governed_autonomous_dial_enabled`` flag is
    armed; the payload declares ``policy == "governed_autonomous_dial"``; a
    non-empty ``stake_sentence`` is present; and the actuation path attests it
    enforced every fence (``within_call_hours``, ``cooldown_ok``,
    ``under_daily_cap``, ``dnc_ok``, and — ADR-017 — ``consent_ok`` each literally
    True). ``consent_ok`` requires a lawful consent basis; a cold no-consent
    number never clears it and is routed to a voicemail draft instead of a live
    call. Banned-phrase (G2) is still checked independently by the caller.
    """
    try:
        from backend.common.config import get_settings

        if not bool(getattr(get_settings(), "governed_autonomous_dial_enabled", False)):
            return False
    except Exception:  # noqa: BLE001 — cannot confirm the flag -> fail closed
        return False
    p = action.payload or {}
    if p.get("policy") != "governed_autonomous_dial":
        return False
    if not str(p.get("stake_sentence") or "").strip():
        return False
    for gate in ("within_call_hours", "cooldown_ok", "under_daily_cap", "dnc_ok", "consent_ok"):
        if p.get(gate) is not True:
            return False
    return True


def _check_blocking(
    action: ProposedAction,
    registry: CodexRegistry,
) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    kind = action.action_kind
    payload = action.payload or {}

    if kind == "voice_dial" and not _governed_dial_permitted(action):
        findings.append(
            (
                "VR-G5",
                "ADR-002/ADR-016/ADR-017: autonomous dialing forbidden unless the "
                "governed dial policy is armed and its preconditions (stake_sentence, "
                "within_call_hours, cooldown_ok, under_daily_cap, dnc_ok, consent_ok) "
                "are attested. A cold no-consent number is never live-dialed.",
            )
        )

    if kind == "subscription_create":
        findings.append(
            (
                "VR-ADR-003",
                "ADR-003 defers productization until >=10 paid closes.",
            )
        )

    if kind == "outreach_send" and _is_missing(payload, "stake_sentence"):
        findings.append(
            (
                "VR-G1",
                "Guardrail G1 requires Stake Sentence on every outbound dispatch.",
            )
        )

    banned_phrases = registry.banned_phrases()
    for field_name, value in _iter_user_text_fields(payload):
        lowered = value.lower()
        for phrase in banned_phrases:
            if phrase and phrase.lower() in lowered:
                findings.append(
                    (
                        "VR-G2",
                        f"Guardrail G2: field {field_name!r} contains banned phrase {phrase!r}.",
                    )
                )
                break

    # G6: the seo workcell filters unverified claims at render time and
    # passes the post-filter evidence_sources list. The Codex layer's job
    # is to ensure callers ACKNOWLEDGE the filter — a missing key means a
    # caller skipped the filter contract entirely (block). An empty list
    # is fine: it just means the filter dropped every claim because none
    # were tagged with a verified source, which IS the safe path.
    if kind == "gap_report_render" and "evidence_sources" not in payload:
        findings.append(
            (
                "VR-G6",
                "Guardrail G6: Gap Report renders must declare evidence_sources "
                "(post-filter list of EvidenceSource enum values). Empty list is "
                "permitted; absent key indicates the caller bypassed the filter.",
            )
        )

    if kind == "reward_function_update" and payload.get("subtracts_harm") is not True:
        findings.append(
            (
                "VR-G7",
                "Guardrail G7: reward function must subtract harm "
                "(retracted_claims, unsubscribes, complaints).",
            )
        )

    if kind == "outreach_send" and _is_missing(payload, "legitimacy_signal"):
        findings.append(
            (
                "VR-G8",
                "Guardrail G8: outreach refuses to fire on a prospect with no "
                "warmth signal (RFP, chamber, prior inbound, public registry).",
            )
        )

    if kind == "other":
        target = _get_str(payload, "target")
        if target == "STAKE_SENTENCE_BANNED_PHRASES":
            findings.append(
                (
                    "VR-ADR-008",
                    "ADR-008 requires a Codex ADR before any banned-phrase list change.",
                )
            )

    return findings


def _check_warnings(action: ProposedAction) -> list[str]:
    # v1.1: all intent-only guardrails (G6/G7/G8) flipped to blocking in
    # ADR-012. No advisory-only rules remain. Function kept for future
    # warning-only rules; returns empty until one is needed.
    return []


def check_action(
    action: ProposedAction,
    *,
    registry: CodexRegistry | None = None,
) -> Verdict:
    reg = registry if registry is not None else REGISTRY
    if not reg.is_loaded():
        raise CodexUnavailable(
            "codex registry not loaded; refusing to validate (fail-closed)",
        )

    blocking = _check_blocking(action, reg)
    warnings = _check_warnings(action)

    if blocking:
        blocking_sorted = sorted(blocking, key=lambda pair: pair[0])
        primary_id, primary_reason = blocking_sorted[0]
        secondary_warnings = [f"{rid}: {reason}" for rid, reason in blocking_sorted[1:]]
        drafted_path: Path | None = None
        try:
            drafted_path = draft_adr_for_violation(
                action,
                primary_id,
                primary_reason,
                reg,
            )
        except OSError as exc:
            _LOG.error(
                "codex adr_drafter write failed; verdict stays blocking: %s",
                exc,
            )
            secondary_warnings.append(
                f"adr_draft_write_failed: {exc!s}",
            )

        return Verdict(
            allowed=False,
            violated_rule_id=primary_id,
            reason=primary_reason,
            drafted_adr_path=str(drafted_path) if drafted_path else None,
            warnings=secondary_warnings + warnings,
        )

    return Verdict(
        allowed=True,
        violated_rule_id=None,
        reason=None,
        drafted_adr_path=None,
        warnings=warnings,
    )


__all__ = [
    "check_action",
]
