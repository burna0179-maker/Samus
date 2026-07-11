"""Variant-arm stamping for outbound Vapi calls (Tranche 3 learning loop).

Every dispatched call is stamped with a ``variant_arm_id`` identifying the
assistant configuration (script variant) that placed it, so downstream
transcript analysis can flow its [0,1] reward back to the UCB1 bandit in
:mod:`backend.attribution.engine` — script variants finally learn.

Arm identity
------------
``voice::<assistant_id>::<config_version>`` where the config version is, in
priority order:

  1. ``SAMUS_VOICE_SCRIPT_VERSION`` env (operator-pinned label), else
  2. an 8-hex content hash of the tracked assistant config
     (``backend/voice/assistant_configs/morgan_sdr.json``) — any script edit
     mints a new arm automatically, else
  3. the literal ``"v0"``.

Dispatch ledger
---------------
Because Vapi transcripts are ingested from files (no call metadata survives
the round-trip reliably), each dispatch is ALSO appended to a local
append-only ledger mapping ``call_id / prospect_id / phone tail -> arm_id``.
:func:`lookup_arm` resolves the most recent dispatch for a prospect or phone
number; the transcript analyzer uses it to credit the right arm.

All writes/reads are fail-soft — arm stamping is an optimization and must
never block or fail a dial.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any

from backend.common.dates import iso_now
from backend.common.persistence import open_ledger
from backend.common.state_paths import state_path

_LOG = logging.getLogger("samus.voice.arm_stamp")

ENV_SCRIPT_VERSION = "SAMUS_VOICE_SCRIPT_VERSION"

_LEDGER_JSONL = ("voice", "call_arm_ledger.jsonl")
_LEDGER_COLLECTION = "voice_call_arms"

_CONFIG_PATH = Path(__file__).resolve().parent / "assistant_configs" / "morgan_sdr.json"

# How many recent dispatch rows lookup_arm scans (newest wins).
_LOOKUP_TAIL = 2000


def _ledger():
    return open_ledger(
        jsonl_path=state_path(*_LEDGER_JSONL),
        collection=_LEDGER_COLLECTION,
    )


def _config_version() -> str:
    pinned = (os.getenv(ENV_SCRIPT_VERSION) or "").strip()
    if pinned:
        return pinned
    try:
        digest = hashlib.sha256(_CONFIG_PATH.read_bytes()).hexdigest()[:8]
        return f"cfg-{digest}"
    except OSError:
        return "v0"


def current_arm_id(assistant_id: str = "") -> str:
    """The variant arm identifying the active assistant config."""
    aid = (assistant_id or "default").strip() or "default"
    return f"voice::{aid}::{_config_version()}"


def _phone_tail(phone: str | None) -> str:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return digits[-4:] if digits else ""


def record_dispatch(
    *,
    call_id: str,
    prospect_id: str | None,
    phone: str | None,
    arm_id: str,
) -> bool:
    """Append one dispatch->arm mapping. Fail-soft; returns success bool."""
    row: dict[str, Any] = {
        "ts": iso_now(),
        "call_id": call_id or "",
        "prospect_id": prospect_id or "",
        "phone_tail": _phone_tail(phone),
        "arm_id": arm_id,
    }
    try:
        _ledger().append(row)
        return True
    except Exception as exc:  # noqa: BLE001 — never block a dial
        _LOG.warning("arm dispatch record failed call=%s: %s", call_id, exc)
        return False


def lookup_arm(
    *,
    prospect_id: str | None = None,
    phone: str | None = None,
    call_id: str | None = None,
) -> str:
    """Most recent arm_id dispatched to this call/prospect/phone, else ""."""
    pid = (prospect_id or "").strip()
    tail = _phone_tail(phone)
    cid = (call_id or "").strip()
    if not (pid or tail or cid):
        return ""
    try:
        rows = _ledger().tail(limit=_LOOKUP_TAIL)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("arm lookup read failed: %s", exc)
        return ""
    for row in reversed(rows):  # newest first
        if not isinstance(row, dict):
            continue
        if cid and str(row.get("call_id", "")) == cid:
            return str(row.get("arm_id", ""))
        if pid and str(row.get("prospect_id", "")) == pid:
            return str(row.get("arm_id", ""))
        if tail and str(row.get("phone_tail", "")) == tail:
            return str(row.get("arm_id", ""))
    return ""


__all__ = [
    "ENV_SCRIPT_VERSION",
    "current_arm_id",
    "record_dispatch",
    "lookup_arm",
]
