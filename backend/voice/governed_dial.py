"""Governed autonomous dial actuation (ADR-016).

The bridge from "the Codex permits this dial" to "a call is placed." It is the
ONLY path allowed to place an autonomous outbound call, and it is fail-closed at
every step:

  1. LOCAL fail-closed — refuse before touching the Codex unless EVERY fence
     (`within_call_hours`, `cooldown_ok`, `under_daily_cap`, `dnc_ok`) is True and
     a non-empty operator-authored `stake_sentence` is present. A single failed
     fence returns blocked without proposing anything.
  2. CODEX ratifies — build the attested `voice_dial` action and route it through
     `check_action`. Under ADR-016 the Codex allows it ONLY when the governed
     policy is armed AND the attestation is complete; otherwise VR-G5 blocks.
     A Codex that is unavailable fails closed (blocked).
  3. DIAL — only in the `verdict.allowed` branch do we call the injected
     `dial_fn` (the real dialer's place-call). A dial error returns blocked, not
     a partial success.

Fence COMPUTATION (business-hours, cooldown, daily cap, DNC) and prospect data
(stake, phone) are the CALLER's job — the idle-production drive gathers them and
passes them in. This module owns only the gate->dial safety, so it stays pure
and fully testable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

_LOG = logging.getLogger("samus.voice.governed_dial")

_REQUIRED_FENCES = (
    "within_call_hours",  # prospect-local TCPA window (8–21)
    "cooldown_ok",  # per-number cooldown floor honored
    "under_daily_cap",  # daily dial cap not exceeded
    "dnc_ok",  # not on the suppression/DNC list
    "consent_ok",  # ADR-017: lawful consent basis (inbound/opted-in/customer);
    # a cold no-consent number is never live-dialed — it is
    # routed to a voicemail draft (ADR-002) by the caller.
)


@dataclass
class GovernedDialResult:
    dialed: bool
    blocked: bool
    rule: str = ""
    reason: str = ""
    call: Any = None
    attested: dict[str, Any] = field(default_factory=dict)


def place_governed_dial(
    *,
    prospect_id: str,
    stake_sentence: str,
    fences: dict[str, Any],
    dial_fn: Callable[..., Any],
    phone: str = "",
    campaign: str = "",
    correlation_id: str = "",
    variable_values: dict[str, Any] | None = None,
    customer_name: str = "",
) -> GovernedDialResult:
    """Place ONE governed autonomous dial, or refuse. Never raises.

    ``fences`` must carry every key in ``_REQUIRED_FENCES`` set to ``True`` (the
    caller computed + enforced them). ``dial_fn`` is the injected place-call —
    called ONLY after the Codex allows. Returns a :class:`GovernedDialResult`;
    ``dialed`` is True only when a call was actually placed.
    """
    # Step 1 — local fail-closed. Never propose a dial with an incomplete fence
    # set; a missing OR false fence blocks here, before the Codex is consulted.
    failed = [k for k in _REQUIRED_FENCES if fences.get(k) is not True]
    if failed:
        return GovernedDialResult(
            dialed=False,
            blocked=True,
            rule="FENCE",
            reason=f"fence(s) not satisfied: {', '.join(failed)}",
        )
    stake = str(stake_sentence or "").strip()
    if not stake:
        return GovernedDialResult(
            dialed=False,
            blocked=True,
            rule="G1",
            reason="stake_sentence required (operator-authored)",
        )

    # Step 2 — Codex ratifies the attested action (ADR-016).
    from backend.common.codex import (  # local import: codex primed at app boot
        CodexUnavailable,
        ProposedAction,
        check_action,
    )

    attested = {
        "policy": "governed_autonomous_dial",
        "stake_sentence": stake,
        "prospect_id": prospect_id,
        "phone": phone,
        "campaign": campaign,
        **{k: True for k in _REQUIRED_FENCES},
    }
    action = ProposedAction(
        service="voice",
        capability="voice_dial",
        action_kind="voice_dial",
        payload=attested,
        proposed_by="voice.governed_dial.place_governed_dial",
        correlation_id=correlation_id or None,
    )
    try:
        verdict = check_action(action)
    except CodexUnavailable as exc:
        _LOG.error("governed dial refused: codex unavailable (%s)", exc)
        return GovernedDialResult(
            dialed=False,
            blocked=True,
            rule="CODEX_UNAVAILABLE",
            reason=f"codex unavailable; fail-closed: {exc}",
            attested=attested,
        )
    if not verdict.allowed:
        _LOG.info(
            "governed dial blocked by codex: rule=%s prospect=%s",
            verdict.violated_rule_id,
            prospect_id,
        )
        return GovernedDialResult(
            dialed=False,
            blocked=True,
            rule=verdict.violated_rule_id or "VR-UNKNOWN",
            reason=verdict.reason or "codex blocked voice_dial",
            attested=attested,
        )

    # Step 3 — approved. Place the call via the injected dialer. The optional
    # variable_values (Morgan's per-prospect opener / voicemail / prior-touch
    # context) + customer_name make the call INFORMED; a dial_fn that ignores
    # them still works (back-compat).
    try:
        outcome = dial_fn(
            prospect_id=prospect_id,
            phone=phone,
            stake_sentence=stake,
            campaign=campaign,
            correlation_id=correlation_id,
            variable_values=variable_values,
            customer_name=customer_name,
        )
    except Exception as exc:  # noqa: BLE001 — a dial fault is a block, not a raise
        _LOG.warning("governed dial place-call failed prospect=%s: %s", prospect_id, exc)
        return GovernedDialResult(
            dialed=False,
            blocked=False,
            rule="DIAL_ERROR",
            reason=f"place-call failed: {exc}",
            attested=attested,
        )
    _LOG.info("governed dial PLACED for prospect=%s", prospect_id)
    return GovernedDialResult(
        dialed=True,
        blocked=False,
        reason="governed dial placed",
        call=outcome,
        attested=attested,
    )
