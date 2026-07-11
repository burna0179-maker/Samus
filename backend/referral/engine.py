"""Referral engine — code generation, trigger, attribution, qualification.

Pure functions over explicit inputs (no hidden global state); the only side
effect is an optional append to a JSONL tracking ledger. Codes are
deterministic (a hash of referrer id + program salt) so the same customer
always gets the same shareable code — idempotent and test-stable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict
from pathlib import Path

from backend.referral.models import Referral, ReferralProgram

_LOG = logging.getLogger("samus.referral.engine")

_LEDGER_PATH = Path(
    os.getenv("SAMUS_REFERRAL_LEDGER_PATH", "/opt/samus/data/referral/referrals.jsonl")
)
# Unambiguous code alphabet (no 0/O/1/I) for human-shareable codes.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LEN = 8


def generate_code(referrer_id: str, program: ReferralProgram) -> str:
    """Deterministic share code for a referrer. Same input -> same code."""
    digest = hashlib.sha256(f"{program.code_salt}:{referrer_id}".encode()).digest()
    chars = [_ALPHABET[b % len(_ALPHABET)] for b in digest[:_CODE_LEN]]
    return "".join(chars)


def build_link(referrer_id: str, program: ReferralProgram) -> str:
    """The shareable referral URL for a referrer."""
    code = generate_code(referrer_id, program)
    base = program.base_url.rstrip("/")
    return f"{base}/?ref={code}"


def should_offer_referral(events: list[str], already_offered: bool) -> bool:
    """Fire the referral ask at the moment of first result — when the customer
    first sees a meaningful outcome, not at signup or billing. Returns True only
    once (gated by ``already_offered``)."""
    if already_offered:
        return False
    trigger_events = {"first_result", "aha_moment", "first_value_delivered"}
    return any(e in trigger_events for e in events)


def record_referral(
    code: str,
    referrer_id: str,
    referee_id: str,
    program: ReferralProgram,
    *,
    now_iso: str,
    ledger_path: str | Path | None = None,
    persist: bool = True,
) -> Referral:
    """Attribute a referee signup to a referrer's code. Status starts pending.

    Self-referral (referee == referrer) is rejected up front."""
    status = "rejected" if referee_id == referrer_id else "pending"
    referral = Referral(
        code=code,
        referrer_id=referrer_id,
        referee_id=referee_id,
        status=status,
        created_at=now_iso,
    )
    if persist:
        _append(referral, ledger_path)
    return referral


def qualify_referral(
    referral: Referral,
    conversion_event: str,
    program: ReferralProgram,
    *,
    now_iso: str,
    ledger_path: str | Path | None = None,
    persist: bool = True,
) -> Referral:
    """Promote a pending referral to qualified when the referee fires the
    program's qualify event, attaching dual-sided rewards. No-op (returns the
    referral unchanged) for non-pending referrals or a non-matching event."""
    if referral.status != "pending":
        return referral
    if conversion_event != program.qualify_event:
        return referral
    referral.status = "qualified"
    referral.referrer_reward = program.referrer_reward
    referral.referee_reward = program.referee_reward
    referral.qualified_at = now_iso
    if persist:
        _append(referral, ledger_path)
    return referral


def compute_rewards(referral: Referral, program: ReferralProgram) -> dict[str, str]:
    """Return the dual-sided rewards owed for a qualified referral, else empty."""
    if referral.status not in ("qualified", "rewarded"):
        return {}
    return {
        "referrer_id": referral.referrer_id,
        "referrer_reward": program.referrer_reward,
        "referee_id": referral.referee_id,
        "referee_reward": program.referee_reward,
    }


# ---------------------------------------------------------------------------
# HTTP-adapter handlers (dict in, dict out)
# ---------------------------------------------------------------------------


def _program_from(payload: dict) -> ReferralProgram:
    p = payload.get("program") if isinstance(payload.get("program"), dict) else {}
    defaults = ReferralProgram()
    return ReferralProgram(
        name=str(p.get("name", defaults.name)),
        base_url=str(p.get("base_url", defaults.base_url)),
        referrer_reward=str(p.get("referrer_reward", defaults.referrer_reward)),
        referee_reward=str(p.get("referee_reward", defaults.referee_reward)),
        qualify_event=str(p.get("qualify_event", defaults.qualify_event)),
        code_salt=str(p.get("code_salt", defaults.code_salt)),
    )


def handle_referral_code(payload: dict) -> dict:
    program = _program_from(payload)
    referrer_id = str(payload.get("referrer_id") or "")
    if not referrer_id:
        return {"error": "referrer_id_required"}
    return {
        "referrer_id": referrer_id,
        "code": generate_code(referrer_id, program),
        "link": build_link(referrer_id, program),
    }


def handle_referral_record(payload: dict) -> dict:
    program = _program_from(payload)
    referrer_id = str(payload.get("referrer_id") or "")
    referee_id = str(payload.get("referee_id") or "")
    if not referrer_id or not referee_id:
        return {"error": "referrer_id_and_referee_id_required"}
    code = str(payload.get("code") or generate_code(referrer_id, program))
    ref = record_referral(
        code, referrer_id, referee_id, program, now_iso=str(payload.get("now") or "")
    )
    return asdict(ref)


def handle_referral_qualify(payload: dict) -> dict:
    program = _program_from(payload)
    raw = payload.get("referral")
    if not isinstance(raw, dict):
        return {"error": "referral_required"}
    referral = Referral(
        code=str(raw.get("code", "")),
        referrer_id=str(raw.get("referrer_id", "")),
        referee_id=str(raw.get("referee_id", "")),
        status=raw.get("status", "pending"),
        created_at=str(raw.get("created_at", "")),
    )
    out = qualify_referral(
        referral, str(payload.get("event") or ""), program, now_iso=str(payload.get("now") or "")
    )
    return {**asdict(out), "rewards": compute_rewards(out, program)}


def _append(referral: Referral, ledger_path: str | Path | None) -> None:
    path = Path(ledger_path) if ledger_path else _LEDGER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(referral), ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        _LOG.warning("referral ledger append failed: %s", exc)
