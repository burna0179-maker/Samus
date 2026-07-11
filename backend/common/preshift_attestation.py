"""Pre-shift attestation — the operator's daily acknowledgement of autonomous execution.

By the operator's design (2026-07-03, ADR-018): running the pre-shift
preparation IS the operator's acknowledgement of what the agent will execute
that business day. So a successful pre-shift briefing stamps a durable
attestation for the current BUSINESS day, and the governed-dial consent basis
(``consent_ok`` for a business record) requires today's attestation to be
present. The attestation is fresh daily and simply absent on any day the
pre-shift did not run — so autonomous live B2B dialing self-suspends to
voicemail drafts until the operator next starts their day with the pre-shift.

Keyed on the BUSINESS date (``business_today`` — Pacific), matching the
call-hours/business-day semantics the dial fences use, so the writer and reader
never disagree the way a naive UTC ``date.today()`` would in the container.

All functions are defensive: a write fault never breaks the briefing, and a
read fault resolves to NOT attested (fail-closed — no attestation, no live
call).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

_LOG = logging.getLogger("samus.common.preshift_attestation")

_REL_DIR = "cognition"
_PREFIX = "preshift_attestation_"


def _business_date() -> str:
    try:
        from backend.common.us_timezones import business_today

        return business_today().isoformat()
    except Exception:  # noqa: BLE001
        from datetime import date

        return date.today().isoformat()


def _path_for(business_date: str):
    from backend.common import storage

    d = storage.root() / _REL_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_PREFIX}{business_date}.json"


def write_attestation(*, briefing_id: str = "", extra: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Stamp today's pre-shift attestation. Returns the path written, or None on
    fault. Idempotent per business day (re-running the pre-shift just refreshes
    the record). NEVER raises — the briefing must not fail on this."""
    try:
        from backend.common.dates import iso_now

        bd = _business_date()
        payload = {
            "kind": "preshift_attestation",
            "business_date": bd,
            "attested_at": iso_now(),
            "briefing_id": briefing_id,
            "acknowledges": (
                "Operator acknowledgement (ADR-018): running the pre-shift "
                "preparation authorizes the agent's autonomous execution for "
                "this business day, including governed B2B live dialing within "
                "all hard fences (call-hours, DNC, cooldown, stake, CODB "
                "affordability ceiling)."
            ),
        }
        if extra:
            payload.update(extra)
        path = _path_for(bd)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _LOG.info("pre-shift attestation stamped for business_date=%s", bd)
        return str(path)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("pre-shift attestation write failed: %s", exc)
        return None


def is_attested(business_date: Optional[str] = None) -> bool:
    """True iff a pre-shift attestation exists for the given (default: today's)
    business day. Fail-closed: any read fault or a missing file => False."""
    try:
        bd = business_date or _business_date()
        path = _path_for(bd)
        if not path.is_file():
            return False
        row = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(row, dict) and row.get("kind") == "preshift_attestation" \
            and row.get("business_date") == bd
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("pre-shift attestation read failed: %s", exc)
        return False
