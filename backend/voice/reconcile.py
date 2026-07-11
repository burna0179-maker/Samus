"""Post-call reconciliation — backfill end_of_call events for calls whose
Vapi end-of-call-report webhook never arrived.

Why this exists (Gap-8, 2026-06-30): the first live call ended on voicemail,
Vapi computed the analysis, but the ``end-of-call-report`` webhook was never
delivered (a Vapi-side / tunnel delivery gap — the report type was in
``serverMessages`` and the assistant ``server.url`` was set, yet only the two
non-terminal events arrived). With no end_of_call event written, the outcome
is invisible to the session monitor, the pattern aggregator, the morning
briefing, and CRM writeback. The most common cold-call outcome (voicemail)
would silently vanish from the feedback loop.

The fix is a RECOVERY MECHANISM, not a chase for why one webhook dropped:
Vapi's REST API holds the source-of-truth outcome. This module reads today's
``dial_attempt`` events, finds the ``initiated`` ones with no matching
``end_of_call`` event, fetches each call from Vapi, and writes the missing
end_of_call event — making the feedback loop resilient to ANY dropped webhook.

Idempotent: a call that already has an end_of_call event is skipped. Best-
effort + fail-open: a Vapi fetch error logs + skips that call, never aborts
the sweep. Designed to run as a periodic scheduled sweep AND on-demand.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from backend.common.config import get_settings
from backend.common.dates import iso_now

_LOG = logging.getLogger("samus.voice.reconcile")

_EVENTS_PATH_DEFAULT = "/opt/samus/data/voice/voice_events.jsonl"
_DIAL_RUNS_PATH_DEFAULT = "/opt/samus/data/artifacts/voice/dial_runs"


# ---------------------------------------------------------------------------
# ended_reason -> outcome (mirrors call_session_monitor._infer_outcome so the
# reconciled events classify identically to webhook-written ones)
# ---------------------------------------------------------------------------


def _outcome_from_ended_reason(ended_reason: str) -> str:
    ended = (ended_reason or "").lower()
    if ended in (
        "no-answer",
        "machine_detected_silence",
        "customer-did-not-answer",
        "twilio-failed-to-connect-call",
    ):
        return "no_answer"
    if "voicemail" in ended or ended in (
        "machine_end_beep",
        "machine_end_silence",
        "machine_end_other",
    ):
        return "voicemail_left"
    if "customer-ended" in ended or "customer-busy" in ended:
        return "not_interested"
    if "assistant-ended" in ended or ended == "assistant-said-end-call-phrase":
        return "completed_conversation"
    return "no_answer"


def _duration_seconds(call: dict[str, Any]) -> float | None:
    """Reliably derive call duration (seconds) from a Vapi call object.

    Opener effectiveness is measured by time-to-hangup, so a null duration on
    the reconcile backstop blinds that metric. Vapi's call object often omits
    the top-level ``durationSeconds`` on records the reconcile sweep picks up,
    so this falls back through several sources:

      1. ``durationSeconds`` (or legacy ``duration``) when present, and
      2. ``endedAt`` − ``startedAt`` (ISO-8601 timestamps) computed here, and
      3. ``durationMs`` / ``duration_ms`` (milliseconds) as a last resort.

    Returns a non-negative float, or None only when no source yields a value.
    """
    raw = call.get("durationSeconds")
    if raw is None:
        raw = call.get("duration")
    if raw is not None:
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            pass

    started = _parse_iso(call.get("startedAt"))
    ended = _parse_iso(call.get("endedAt"))
    if started is not None and ended is not None:
        delta = (ended - started).total_seconds()
        if delta >= 0:
            return delta

    for key in ("durationMs", "duration_ms"):
        ms = call.get(key)
        if ms is not None:
            try:
                return max(0.0, float(ms) / 1000.0)
            except (TypeError, ValueError):
                continue
    return None


def _parse_iso(value: Any) -> datetime | None:
    """Parse a Vapi ISO-8601 timestamp string to a datetime, else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _gatekeeper_detect_enabled() -> bool:
    """Gatekeeper reclassification (Gap-19) is a correctness fix — default ON.
    Set SAMUS_VOICE_GATEKEEPER_DETECT=0 to disable."""
    return os.getenv("SAMUS_VOICE_GATEKEEPER_DETECT", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _events_path() -> Path:
    return Path(os.getenv("SAMUS_VOICE_EVENTS_PATH", _EVENTS_PATH_DEFAULT))


def _read_today_events() -> list[dict[str, Any]]:
    path = _events_path()
    if not path.exists():
        return []
    today = date.today().isoformat()
    out: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if str(rec.get("ts", "")).startswith(today):
                out.append(rec)
    except OSError as exc:
        _LOG.warning("reconcile: failed to read events: %s", exc)
    return out


def _dial_runs_path() -> Path:
    return Path(os.getenv("SAMUS_VOICE_DIAL_RUNS_PATH", _DIAL_RUNS_PATH_DEFAULT))


def _read_today_dial_runs() -> dict[str, dict[str, Any]]:
    """Second candidate source: today's host-dialer dial_run files.

    The host dialer (``scripts/Start-MorningDial.ps1`` -> ``backend.voice.dialer``)
    runs ON THE HOST and writes each run to host ``dial_runs/*.json`` — NOT to the
    container's ``voice_events.jsonl``. Those calls would otherwise be structurally
    invisible to reconcile. The host dial_runs dir is bind-mounted into the
    container, so we read it directly.

    Returns ``{call_id: {company, prospect_id, phone, outbound_number_id}}`` for
    every INITIATED call (non-null ``call_id``) in today's run files. "Today" is
    decided by the run's internal ``ts`` field, matching ``_read_today_events``.

    Fail-open: a missing dir / unreadable file / bad json is skipped, never raised.
    """
    out: dict[str, dict[str, Any]] = {}
    base = _dial_runs_path()
    if not base.exists():
        return out
    today = date.today().isoformat()
    try:
        files = sorted(base.glob("*.json"))
    except OSError as exc:
        _LOG.warning("reconcile: failed to list dial_runs: %s", exc)
        return out
    for fp in files:
        try:
            run = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _LOG.debug("reconcile: skip dial_run %s: %s", fp.name, exc)
            continue
        if not isinstance(run, dict):
            continue
        if not str(run.get("ts", "")).startswith(today):
            continue
        attempts = run.get("attempts")
        if not isinstance(attempts, list):
            continue
        for att in attempts:
            if not isinstance(att, dict):
                continue
            if att.get("outcome") != "initiated":
                continue
            cid = str(att.get("call_id") or "")
            if not cid:
                continue
            # later run files (sorted) win for a given call_id
            out[cid] = {
                "company": att.get("company"),
                "prospect_id": att.get("prospect_id"),
                "phone": att.get("phone"),
                "outbound_number_id": att.get("outbound_number_id"),
            }
    return out


def _default_fetch_call(call_id: str) -> dict[str, Any] | None:
    """Fetch a Vapi call object by id. Returns None on any error (fail-open)."""
    api_key = (get_settings().vapi_api_key or "").strip()
    if not api_key:
        return None
    try:
        import httpx

        resp = httpx.get(
            f"https://api.vapi.ai/call/{call_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=20.0,
        )
        if resp.status_code != 200:
            _LOG.warning("reconcile: Vapi call %s HTTP %s", call_id, resp.status_code)
            return None
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("reconcile: Vapi fetch %s failed: %s", call_id, exc)
        return None


def reconcile_recent_calls(
    *,
    fetch_call: Callable[[str], dict[str, Any] | None] | None = None,
    append_event: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Backfill end_of_call events for today's initiated dials that are missing
    one. Returns a summary dict. ``fetch_call`` / ``append_event`` are injection
    seams for tests; production resolves the real Vapi fetch + the voice event
    ledger.
    """
    fetch_call = fetch_call or _default_fetch_call
    if append_event is None:
        from .service import _append_event as append_event  # type: ignore

    events = _read_today_events()

    # call_ids that already have an end_of_call event — never double-write.
    have_eoc: set[str] = {
        str(e.get("call_id") or "")
        for e in events
        if e.get("kind") == "end_of_call" and e.get("call_id")
    }
    # initiated dials (call_id present) are the candidates. Two sources, unioned:
    #   1. container voice_events.jsonl dial_attempt/initiated records, and
    #   2. host dial_runs/*.json initiated attempts (calls placed by the HOST
    #      dialer that never wrote to voice_events). Both normalized to the same
    #      shape so downstream lookups (company/prospect_id/phone/
    #      outbound_number_id) work regardless of source.
    candidates: dict[str, dict[str, Any]] = {}
    for e in events:
        if e.get("kind") != "dial_attempt":
            continue
        if e.get("outcome") != "initiated":
            continue
        cid = str(e.get("call_id") or "")
        if cid:
            candidates[cid] = {
                "company": e.get("company"),
                "prospect_id": e.get("prospect_id"),
                "phone": e.get("phone"),
                "outbound_number_id": e.get("outbound_number_id"),
            }  # last dial_attempt for this call_id wins

    # Union in host dial_runs. voice_events entries take precedence (they carry
    # the same fields and are the in-container source of truth) but dial_runs
    # fills in any call_id voice_events never saw.
    for cid, dial in _read_today_dial_runs().items():
        candidates.setdefault(cid, dial)

    # Apply the idempotency filter to the MERGED set: drop any call_id that
    # already has an end_of_call event in voice_events.
    candidates = {cid: dial for cid, dial in candidates.items() if cid not in have_eoc}

    summary: dict[str, Any] = {
        "ts": iso_now(),
        "today_events": len(events),
        "candidates": len(candidates),
        "reconciled": 0,
        "still_in_progress": 0,
        "fetch_failed": 0,
        "details": [],
    }

    for cid, dial in candidates.items():
        call = fetch_call(cid)
        if call is None:
            summary["fetch_failed"] += 1
            continue
        status = str(call.get("status") or "")
        if status != "ended":
            # Call still live or queued — not ready to reconcile this pass.
            summary["still_in_progress"] += 1
            continue
        ended_reason = str(call.get("endedReason") or "")
        outcome = _outcome_from_ended_reason(ended_reason)
        analysis = call.get("analysis") or {}
        transcript = str(call.get("transcript") or "")

        # Gatekeeper reclassification (Gap-19): Vapi's AMD stamps the whole call
        # voicemail/no_answer by its opening audio, but a LIVE HUMAN
        # (gatekeeper/receptionist) may have answered after the machine greeting.
        # Those are real warm contacts wrongly dropped from the operator list.
        # Best-effort + flag-gated; never blocks reconciliation.
        gatekeeper_reason = ""
        if outcome in ("voicemail_left", "no_answer") and _gatekeeper_detect_enabled():
            try:
                from .gatekeeper_detector import detect_human_engagement

                is_human, gk_reason = detect_human_engagement(
                    transcript,
                    ended_reason=ended_reason,
                )
                if is_human:
                    outcome = "gatekeeper"
                    gatekeeper_reason = gk_reason
                    _LOG.info(
                        "reconcile: reclassified %s (%s) voicemail->gatekeeper: %s",
                        dial.get("company") or cid,
                        cid,
                        gk_reason,
                    )
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("reconcile: gatekeeper detect skipped: %s", exc)

        event = {
            "ts": iso_now(),
            "kind": "end_of_call",
            "call_id": cid,
            "ended_reason": ended_reason,
            "outcome": outcome,
            "company": dial.get("company"),
            "prospect_id": dial.get("prospect_id"),
            "outbound_number_id": dial.get("outbound_number_id"),
            "vapi_cost": call.get("cost"),
            "duration_seconds": _duration_seconds(call),
            "summary": (analysis.get("summary") or "")[:500],
            "reconciled": True,  # provenance: backfilled, not webhook-delivered
            "reconciled_reason": "missing_end_of_call_webhook",
        }
        if gatekeeper_reason:
            event["reclassified_gatekeeper"] = True
            event["reclassified_reason"] = gatekeeper_reason
        append_event(event)
        summary["reconciled"] += 1
        detail = {
            "call_id": cid,
            "prospect_id": dial.get("prospect_id"),
            "company": dial.get("company"),
            "outcome": outcome,
            "phone": dial.get("phone"),
        }
        if gatekeeper_reason:
            detail["reclassified_gatekeeper"] = True
            detail["reason"] = gatekeeper_reason

        # Deferred-lead detection: a machine-answered call (voicemail/no_answer)
        # whose transcript announces a temporary closure with a reopen date is a
        # LEAD, not a dead-end — schedule a callback for the reopen date instead
        # of letting it drop. Best-effort; never blocks reconciliation.
        if outcome in ("voicemail_left", "no_answer"):
            try:
                from .closure_detector import detect_closure_callback
                from .callback_queue import schedule_callback

                transcript = str(call.get("transcript") or "")
                cb_date, reason = detect_closure_callback(transcript)
                if cb_date and dial.get("prospect_id"):
                    schedule_callback(
                        prospect_id=str(dial.get("prospect_id")),
                        callback_date=cb_date,
                        company=str(dial.get("company") or ""),
                        phone=str(dial.get("phone") or ""),
                        reason=reason,
                    )
                    detail["callback_scheduled"] = cb_date
                    summary["callbacks_scheduled"] = summary.get("callbacks_scheduled", 0) + 1
                    _LOG.info(
                        "reconcile: scheduled callback for %s on %s (%s)",
                        dial.get("company") or cid,
                        cb_date,
                        reason,
                    )
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("reconcile: closure detect skipped: %s", exc)

        summary["details"].append(detail)
        _LOG.info(
            "reconcile: backfilled end_of_call for %s (%s) outcome=%s",
            dial.get("company") or cid,
            cid,
            outcome,
        )

    # After backfilling, give the session monitor a chance to act on the now-
    # complete picture (it no-ops if disabled / below the calls floor).
    if summary["reconciled"]:
        try:
            from .call_session_monitor import check_session

            check_session()
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("reconcile: post-backfill session check skipped: %s", exc)

    return summary
