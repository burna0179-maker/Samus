"""Heat Field service — event ingestion, send accounting, live-band reads.

Ties the pure pieces together:
  * ``record_send`` — tick the "emails sent today" counter (the missing ground
    truth) from the outreach send path.
  * ``ingest_sendgrid_events`` — fold a SendGrid Event Webhook batch into the
    daily counters AND fan a per-address negative event (bounce / spamreport /
    dropped) out to the existing cash-engine halt loop + the suppression list
    (which the ComplianceGuard then honors on the next send).
  * ``current_band`` / ``send_multiplier_now`` / ``is_send_paused_now`` — the
    live reputation read the cadence loop + live-send gate consult. The
    throttle reads are gated behind ``settings.heat_field_enabled`` (default
    OFF): when off they return no-op values (multiplier 1.0, not paused) so the
    Heat Field is fully wired but dormant.

Best-effort + fail-open throughout — heat is reputation hygiene, not a hard
gate, and must never crash a send or a webhook.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from backend.common.config import get_settings
from backend.heat import store as heat_store
from backend.heat.controller import (
    band_for_score,
    is_send_paused,
    recommend_send_posture,
    send_multiplier,
)
from backend.heat.metrics import compute_heat_score

_LOG = logging.getLogger("samus.heat.service")

# SendGrid event -> the feedback handler's negative-event vocabulary.
_NEG_EVENT_MAP: dict[str, str] = {
    "bounce": "bounce",
    "dropped": "bounce",
    "spamreport": "complaint",
    "spam_report": "complaint",
    "unsubscribe": "unsubscribe",
    "group_unsubscribe": "unsubscribe",
}
_SUPPRESS_EVENTS = {"bounce", "dropped", "spamreport", "spam_report"}

_HOTLEAD_EVENTS = {"open", "click", "opened", "clicked"}
_HOTLEAD_JOURNAL_DIR = os.environ.get(
    "SAMUS_ENGAGEMENT_DIR",
    "/opt/samus/data/host_artifacts/engagement",
)


def _log_hotlead_event(ev: dict[str, Any], etype: str) -> None:
    """Append a per-prospect engagement event to the host-mounted journal.

    Best-effort — a write failure never blocks webhook ingestion."""
    import time as _time

    pid = str(ev.get("prospect_id") or "").strip()
    if not pid:
        return
    signal = "clicked" if etype in ("click", "clicked") else "opened"
    day = _time.strftime("%Y-%m-%d", _time.gmtime())
    entry = {
        "ts": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
        "prospect_id": pid,
        "company": str(ev.get("company") or ""),
        "email": str(ev.get("email") or ""),
        "signal": signal,
        "source": "sendgrid_webhook",
    }
    try:
        from pathlib import Path

        d = Path(_HOTLEAD_JOURNAL_DIR)
        d.mkdir(parents=True, exist_ok=True)
        with (d / f"engagement_{day}.jsonl").open("a", encoding="utf-8") as fh:
            import json as _json

            fh.write(_json.dumps(entry) + "\n")
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("hotlead journal write failed: %s", exc)


def _enabled() -> bool:
    return bool(getattr(get_settings(), "heat_field_enabled", False))


def record_send(count: int = 1) -> None:
    """Tick the daily sent counter. Always runs (it is ground-truth volume,
    independent of whether the throttle is armed). Best-effort."""
    try:
        heat_store.get_store().record_send(count)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("heat record_send failed: %s", exc)


def _suppress(email: str, reason: str) -> None:
    """Add an address to the suppression list (best-effort, fail-open). Mirrors
    the feedback handlers' table access so a SendGrid bounce/complaint feeds the
    same list the ComplianceGuard reads pre-send."""
    addr = (email or "").strip().lower()
    if not addr:
        return
    try:
        from backend.common import aws
        from backend.common.dates import iso_now

        settings = get_settings()
        tbl = aws.table(settings.ddb_suppression_table, settings.aws_region)
        tbl.put_item(Item={"email": addr, "reason": reason, "ts": iso_now()})
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("heat suppression write failed for %s: %s", addr, exc)


def _halt_deal(event: str, email: str, prospect_id: str, opportunity_id: str) -> None:
    """Fan a negative event to the existing cash-engine halt loop. Resolves the
    deal from echoed custom_args first, else the recipient index. Best-effort."""
    mapped = _NEG_EVENT_MAP.get(event)
    if not mapped:
        return
    pid, oid = prospect_id, opportunity_id
    if not pid and not oid and email:
        try:
            from backend.common import recipient_index

            rec = recipient_index.lookup_recipient(email) or {}
            pid = pid or str(rec.get("prospect_id", "") or "")
            oid = oid or str(rec.get("opportunity_id", "") or "")
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("heat recipient lookup failed for %s: %s", email, exc)
    if not pid and not oid:
        return
    try:
        from backend.feedback import handlers

        handlers.fire_cash_engine_signal(
            event=mapped,
            opportunity_id=oid,
            prospect_id=pid,
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("heat halt fan-out failed (%s %s/%s): %s", mapped, pid, oid, exc)


def ingest_sendgrid_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold a SendGrid Event Webhook batch into the daily counters + halt loop.

    Each event is a dict with at least ``event`` and ``email``; ``prospect_id`` /
    ``opportunity_id`` are present when we stamped them as custom_args at send
    time (SendGrid echoes custom_args as top-level fields). Returns a small
    summary. Never raises — a malformed event is skipped.
    """
    store = heat_store.get_store()
    counted = 0
    halted = 0
    suppressed = 0
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        etype = str(ev.get("event") or "").strip().lower()
        if not etype:
            continue
        try:
            if store.record_event(etype) is not None:
                counted += 1
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("heat record_event failed (%s): %s", etype, exc)

        if etype in _HOTLEAD_EVENTS:
            _log_hotlead_event(ev, etype)

        if etype in _NEG_EVENT_MAP:
            email = str(ev.get("email") or "")
            pid = str(ev.get("prospect_id") or "")
            oid = str(ev.get("opportunity_id") or "")
            _halt_deal(etype, email, pid, oid)
            halted += 1
            if etype in _SUPPRESS_EVENTS:
                _suppress(email, reason=f"sendgrid_{etype}")
                suppressed += 1
            # Penalize Samus's karma for a reputation-harming outcome (the
            # _NEG_EVENT_MAP value — bounce/complaint/unsubscribe — is a karma
            # outcome key). Best-effort; accrues even while the gate is dormant.
            try:
                from backend.governance.karma import engine as karma

                karma.apply_outcome(_NEG_EVENT_MAP[etype], ref=email)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("karma penalty failed (%s): %s", etype, exc)

    snap = store.snapshot()
    band = band_for_score(compute_heat_score(snap.to_inputs()))
    _LOG.info(
        "heat ingest: counted=%d halted=%d suppressed=%d band=%s sent=%d",
        counted,
        halted,
        suppressed,
        band,
        snap.sent,
    )
    return {
        "ok": True,
        "counted": counted,
        "halted": halted,
        "suppressed": suppressed,
        "band": band,
    }


def current_band() -> str:
    """The observed reputation band right now (independent of the throttle flag)."""
    try:
        snap = heat_store.get_store().snapshot()
        return band_for_score(compute_heat_score(snap.to_inputs()))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("heat current_band failed (treating as cool): %s", exc)
        return "cool"


def send_multiplier_now() -> float:
    """Cadence send-budget multiplier. 1.0 (no-op) when the field is dormant."""
    if not _enabled():
        return 1.0
    return send_multiplier(current_band())


def is_send_paused_now() -> bool:
    """True iff the field is armed AND the band trips safe-mode (critical)."""
    if not _enabled():
        return False
    return is_send_paused(current_band())


def status_snapshot() -> dict[str, Any]:
    """Read-only status for an ops surface."""
    snap = heat_store.get_store().snapshot()
    band = band_for_score(compute_heat_score(snap.to_inputs()))
    return {
        "enabled": _enabled(),
        "band": band,
        "posture": recommend_send_posture(band),
        "send_multiplier": send_multiplier(band) if _enabled() else 1.0,
        "paused": is_send_paused(band) if _enabled() else False,
        "counters": {
            "sent": snap.sent,
            "delivered": snap.delivered,
            "bounced": snap.bounced,
            "complained": snap.complained,
            "blocked": snap.blocked,
            "deferred": snap.deferred,
            "opened": snap.opened,
            "clicked": snap.clicked,
            "unsubscribed": snap.unsubscribed,
        },
        "bucket_day": snap.bucket_day,
    }


__all__ = [
    "record_send",
    "ingest_sendgrid_events",
    "current_band",
    "send_multiplier_now",
    "is_send_paused_now",
    "status_snapshot",
]
