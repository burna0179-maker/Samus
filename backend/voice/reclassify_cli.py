"""Reprocessor for the gatekeeper reclassifier (Gap-19).

    python -m backend.voice.reclassify_cli

Scans TODAY's ``end_of_call`` events in ``voice_events.jsonl`` whose outcome is
``voicemail_left`` or ``no_answer``, and re-examines each transcript for a LIVE
HUMAN (gatekeeper/receptionist) who answered after a machine/hold greeting. For
each one detected, it APPENDS a corrected ``end_of_call`` event with
``outcome="gatekeeper"``, ``reclassified_gatekeeper=True``, the reason, and a
provenance flag — recovering a warm contact Vapi's AMD had discarded.

Unlike :mod:`backend.voice.reconcile` (which backfills MISSING events at dial
time), this reprocessor CORRECTS already-written end_of_call events after the
fact. It uses the transcript stored on the event when present, else fetches the
call from Vapi (reusing ``reconcile._default_fetch_call``).

Idempotent: a call that already has a gatekeeper-reclassified event is skipped.
Fail-open: a per-call error logs + skips, never aborts the sweep. Emits one JSON
line matching ``reconcile_cli``'s output shape ({scanned, reclassified, details})
so it can feed the same Drain-CallOutcomes bridge.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date
from typing import Any, Callable

from backend.common.dates import iso_now

from backend.voice.reconcile import (
    _default_fetch_call,
    _read_today_events,
)
from backend.voice.gatekeeper_detector import detect_human_engagement

_LOG = logging.getLogger("samus.voice.reclassify")


def reclassify_recent_calls(
    *,
    fetch_call: Callable[[str], dict[str, Any] | None] | None = None,
    append_event: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Reprocess today's voicemail/no_answer end_of_call events for gatekeeper
    contact. ``fetch_call`` / ``append_event`` are injection seams for tests."""
    fetch_call = fetch_call or _default_fetch_call
    if append_event is None:
        from backend.voice.service import _append_event as append_event  # type: ignore

    events = _read_today_events()

    # Idempotency: call_ids that already have a gatekeeper-reclassified event.
    already: set[str] = {
        str(e.get("call_id") or "")
        for e in events
        if e.get("kind") == "end_of_call"
        and e.get("call_id")
        and (e.get("reclassified_gatekeeper") is True or e.get("outcome") == "gatekeeper")
    }

    # Candidates: the LATEST end_of_call per call_id whose outcome is a machine
    # verdict. Latest wins so a call already corrected elsewhere is respected.
    latest_eoc: dict[str, dict[str, Any]] = {}
    for e in events:
        if e.get("kind") != "end_of_call":
            continue
        cid = str(e.get("call_id") or "")
        if not cid:
            continue
        latest_eoc[cid] = e  # events are append-ordered; last wins

    summary: dict[str, Any] = {
        "ts": iso_now(),
        "scanned": 0,
        "reclassified": 0,
        "fetch_failed": 0,
        "details": [],
    }

    for cid, eoc in latest_eoc.items():
        if eoc.get("outcome") not in ("voicemail_left", "no_answer"):
            continue
        if cid in already:
            continue
        summary["scanned"] += 1

        ended_reason = str(eoc.get("ended_reason") or "")
        transcript = str(eoc.get("transcript") or "")
        company = eoc.get("company")
        prospect_id = eoc.get("prospect_id")
        phone = eoc.get("phone")
        outbound_number_id = eoc.get("outbound_number_id")

        # No transcript stored on the event → fetch the call from Vapi.
        if not transcript.strip():
            call = fetch_call(cid)
            if call is None:
                summary["fetch_failed"] += 1
                continue
            transcript = str(call.get("transcript") or "")
            if not ended_reason:
                ended_reason = str(call.get("endedReason") or "")
            # Backfill any missing routing fields from the fetched call.
            company = company or call.get("company")

        try:
            is_human, reason = detect_human_engagement(
                transcript, ended_reason=ended_reason,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open per call
            _LOG.debug("reclassify: detect skipped for %s: %s", cid, exc)
            continue

        if not is_human:
            continue

        corrected = {
            "ts": iso_now(),
            "kind": "end_of_call",
            "call_id": cid,
            "ended_reason": ended_reason,
            "outcome": "gatekeeper",
            "company": company,
            "prospect_id": prospect_id,
            "phone": phone,
            "outbound_number_id": outbound_number_id,
            "vapi_cost": eoc.get("vapi_cost"),
            "duration_seconds": eoc.get("duration_seconds"),
            "summary": (eoc.get("summary") or "")[:500],
            "reclassified_gatekeeper": True,
            "reclassified_reason": reason,
            "reclassified_from": eoc.get("outcome"),
            "reclassified_by": "reclassify_cli",  # provenance
        }
        append_event(corrected)
        summary["reclassified"] += 1
        summary["details"].append({
            "call_id": cid,
            "prospect_id": prospect_id,
            "company": company,
            "phone": phone,
            "outcome": "gatekeeper",
            "reclassified_gatekeeper": True,
            "reason": reason,
        })
        _LOG.info(
            "reclassify: %s (%s) %s->gatekeeper: %s",
            company or cid, cid, eoc.get("outcome"), reason,
        )

    return summary


def main(argv: list[str] | None = None) -> int:
    summary = reclassify_recent_calls()
    sys.stdout.write(json.dumps(summary) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
