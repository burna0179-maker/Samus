"""Open-no-click nudge watcher.

When a tracked outbound email gets an ``open`` event but no ``click`` within
the dwell window (default 24 h), this module fires a single soft-nudge email
referencing the original subject + the same CTA URL.

The signal source is the SendGrid Event Webhook journal already written by
:mod:`backend.heat.service` to ``<engagement_dir>/engagement_<day>.jsonl`` —
no polling, no extra API quota. Per-record state lives in a small append-then-
rewrite ledger at ``<artifact_root>/outreach/open_no_click_watch.json``.

Wired-DORMANT — two keys:

  1. ``register(...)`` always runs. The watch record is created the moment a
     tracked email ships, regardless of any flag, so a future arm doesn't
     lose retroactive context.
  2. ``tick(...)`` updates state (clicked → closed; opened → marked) regardless
     of flags. **Firing the nudge** requires
     ``settings.outreach_open_no_click_nudge_enabled=True``; with the flag OFF
     the watcher annotates the record (``would_nudge=True``) and skips the
     send — the standard wired-DORMANT posture
     (cf. ``feedback_wire_not_arm_autonomy``).

Idempotent: a record is fired at most once (``nudged=True`` blocks repeat
sends). Bounce / spamreport / dropped events close the record without a
nudge. ``closed_won`` is honoured via the optional ``mark_closed_won`` hook
so a Stripe payment cancels any pending nudge automatically.

Never raises into the caller — a journal-read fault simply yields zero
candidates this tick.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

_LOG = logging.getLogger("samus.outreach.open_no_click_watch")

_DEFAULT_ARTIFACT_ROOT = "/opt/samus/data/artifacts"
_DEFAULT_ENGAGEMENT_DIR = "/opt/samus/data/host_artifacts/engagement"


# ---------------------------------------------------------------------------
# Path helpers — env-aware, mirror buying_signal_route's convention
# ---------------------------------------------------------------------------


def _artifact_root() -> str:
    return os.getenv("SAMUS_ARTIFACT_ROOT", _DEFAULT_ARTIFACT_ROOT)


def _watch_store_path() -> str:
    return os.path.join(_artifact_root(), "outreach", "open_no_click_watch.json")


def _engagement_dir() -> str:
    return os.getenv("SAMUS_ENGAGEMENT_DIR", _DEFAULT_ENGAGEMENT_DIR)


# ---------------------------------------------------------------------------
# Watch store — same atomic-replace pattern as buying_signal_route
# ---------------------------------------------------------------------------


def _read() -> list[dict[str, Any]]:
    path = _watch_store_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        _LOG.warning("watch store read failed (%s): %s", path, exc)
        return []
    return data if isinstance(data, list) else []


def _write(records: list[dict[str, Any]]) -> bool:
    path = _watch_store_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".onc_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(records, fh, indent=2)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return True
    except OSError as exc:
        _LOG.warning("watch store write failed (%s): %s", path, exc)
        return False


# ---------------------------------------------------------------------------
# Engagement-journal scan — extract per-prospect signals from the SendGrid
# event webhook's journal already written by backend.heat.service.
# ---------------------------------------------------------------------------

_OPENED = {"opened"}
_CLICKED = {"clicked"}


def _iter_engagement_records(
    *,
    since: datetime,
    prospect_id: str,
) -> Iterable[dict[str, Any]]:
    """Yield engagement journal entries for ``prospect_id`` on or after ``since``.

    Best-effort: a missing engagement dir yields nothing. The journal is one
    file per UTC day so we walk every day from ``since`` through today
    inclusive — bounded, cheap, no full-dir glob.
    """
    edir = Path(_engagement_dir())
    if not edir.exists():
        return
    cursor = since.astimezone(timezone.utc).date()
    today = datetime.now(timezone.utc).date()
    while cursor <= today:
        path = edir / f"engagement_{cursor.isoformat()}.jsonl"
        cursor = cursor + timedelta(days=1)
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if str(rec.get("prospect_id") or "") != prospect_id:
                        continue
                    yield rec
        except OSError as exc:
            _LOG.warning("engagement journal read failed (%s): %s", path, exc)
            continue


def _summarise_signals(
    *,
    prospect_id: str,
    sent_at: datetime,
) -> dict[str, str | None]:
    """Return ``{"first_open_at": iso|None, "first_click_at": iso|None}``.

    Only events at or after ``sent_at`` count — older opens / clicks belong to
    earlier sends to the same prospect.
    """
    first_open: str | None = None
    first_click: str | None = None
    for rec in _iter_engagement_records(since=sent_at, prospect_id=prospect_id):
        sig = str(rec.get("signal") or "").strip().lower()
        ts = str(rec.get("ts") or "").strip()
        if not ts:
            continue
        try:
            ts_dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts_dt < sent_at:
            continue
        if sig in _OPENED and first_open is None:
            first_open = ts
        elif sig in _CLICKED and first_click is None:
            first_click = ts
    return {"first_open_at": first_open, "first_click_at": first_click}


# ---------------------------------------------------------------------------
# Public API: register / tick / mark_closed
# ---------------------------------------------------------------------------


def register(
    *,
    prospect_id: str,
    email: str,
    sent_at_iso: str,
    subject: str,
    buy_url: str,
    message_id: str = "",
    company: str = "",
    campaign_id: str = "",
) -> dict[str, Any]:
    """Add a tracked-send watch record. Idempotent on (prospect_id, message_id).

    The record is created regardless of the nudge-enabled flag so an arm
    later still has the context.
    """
    if not prospect_id:
        return {"registered": False, "reason": "no_prospect_id"}
    records = _read()
    for rec in records:
        if (
            rec.get("prospect_id") == prospect_id
            and rec.get("message_id") == message_id
            and not rec.get("closed")
        ):
            return {
                "registered": False,
                "reason": "already_watching",
                "prospect_id": prospect_id,
                "message_id": message_id,
            }
    records.append(
        {
            "prospect_id": prospect_id,
            "email": email,
            "sent_at": sent_at_iso,
            "subject": subject,
            "buy_url": buy_url,
            "message_id": message_id,
            "company": company,
            "campaign_id": campaign_id,
            # Mutable state — tick() fills these in.
            "first_open_at": None,
            "first_click_at": None,
            "nudged": False,
            "nudged_at": None,
            "closed": False,
            "closed_reason": None,
        }
    )
    if not _write(records):
        return {"registered": False, "reason": "persist_failed"}
    _LOG.info(
        "open-no-click watch registered prospect=%s message=%s",
        prospect_id,
        message_id,
    )
    return {"registered": True, "prospect_id": prospect_id, "message_id": message_id}


def mark_closed(
    *,
    prospect_id: str,
    reason: str,
    message_id: str = "",
) -> int:
    """Close any open watch records for ``prospect_id``. Returns the count
    closed. Used to cancel a pending nudge when a Stripe payment lands or
    the operator manually retires the deal."""
    records = _read()
    n = 0
    for rec in records:
        if rec.get("prospect_id") != prospect_id:
            continue
        if message_id and rec.get("message_id") != message_id:
            continue
        if rec.get("closed"):
            continue
        rec["closed"] = True
        rec["closed_reason"] = reason
        n += 1
    if n:
        _write(records)
    return n


def tick(
    *,
    now_iso: str | None = None,
    composer=None,
    dry_run: bool | None = None,
) -> list[dict[str, Any]]:
    """Walk every open watch record, fold in any engagement signals, and
    fire a nudge for each record that has crossed the dwell window with an
    open and no click.

    ``composer(record) -> (subject, html_body, text_body)`` builds the
    prospect-facing email. With ``composer=None`` :func:`_default_composer`
    is used. ``dry_run=None`` (default) honours the
    ``outreach_open_no_click_nudge_enabled`` flag; pass ``True`` to plan-only
    regardless of the flag, ``False`` to force-fire (operator override).
    """
    from backend.common.config import get_settings

    settings = get_settings()
    dwell_h = int(getattr(settings, "outreach_open_no_click_dwell_hours", 24))
    nudge_enabled = bool(getattr(settings, "outreach_open_no_click_nudge_enabled", False))
    if dry_run is None:
        live_fire = nudge_enabled
    else:
        live_fire = not dry_run

    now_dt = (
        datetime.strptime(now_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if now_iso
        else datetime.now(timezone.utc)
    )
    dwell = timedelta(hours=dwell_h)
    composer = composer or _default_composer

    records = _read()
    dirty = False
    out: list[dict[str, Any]] = []

    for rec in records:
        if rec.get("closed"):
            continue
        try:
            sent_at = datetime.strptime(
                str(rec.get("sent_at") or ""),
                "%Y-%m-%dT%H:%M:%SZ",
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        signals = _summarise_signals(
            prospect_id=str(rec.get("prospect_id") or ""),
            sent_at=sent_at,
        )
        if signals["first_open_at"] and not rec.get("first_open_at"):
            rec["first_open_at"] = signals["first_open_at"]
            dirty = True
        if signals["first_click_at"] and not rec.get("first_click_at"):
            rec["first_click_at"] = signals["first_click_at"]
            rec["closed"] = True
            rec["closed_reason"] = "clicked"
            dirty = True
            out.append({"prospect_id": rec["prospect_id"], "action": "closed_clicked"})
            continue
        if not rec.get("first_open_at"):
            out.append({"prospect_id": rec["prospect_id"], "action": "no_open_yet"})
            continue
        if rec.get("nudged"):
            out.append({"prospect_id": rec["prospect_id"], "action": "already_nudged"})
            continue
        first_open_dt = datetime.strptime(
            rec["first_open_at"],
            "%Y-%m-%dT%H:%M:%SZ",
        ).replace(tzinfo=timezone.utc)
        if now_dt - first_open_dt < dwell:
            out.append(
                {
                    "prospect_id": rec["prospect_id"],
                    "action": "dwell_not_reached",
                    "hours_since_open": round((now_dt - first_open_dt).total_seconds() / 3600, 1),
                }
            )
            continue
        # Crossed the dwell window with an open + no click.
        if not live_fire:
            rec.setdefault("would_nudge_at", now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
            dirty = True
            out.append({"prospect_id": rec["prospect_id"], "action": "would_nudge_flag_off"})
            continue
        try:
            subject, html_body, text_body = composer(rec)
            from backend.common.email_backend import send_email  # noqa: PLC0415

            resp = send_email(
                to=str(rec.get("email") or ""),
                subject=subject,
                body=text_body,
                html_body=html_body,
                custom_args={
                    "prospect_id": str(rec.get("prospect_id") or ""),
                    "campaign_id": str(rec.get("campaign_id") or ""),
                    "touch": "open_no_click_nudge",
                },
            )
            rec["nudged"] = True
            rec["nudged_at"] = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            rec["nudge_message_id"] = str(resp.get("message_id") or "")
            dirty = True
            out.append(
                {
                    "prospect_id": rec["prospect_id"],
                    "action": "nudged",
                    "nudge_message_id": rec["nudge_message_id"],
                }
            )
        except Exception as exc:  # noqa: BLE001 — never let one record break the run
            _LOG.warning("nudge send failed prospect=%s: %s", rec.get("prospect_id"), exc)
            out.append(
                {
                    "prospect_id": rec["prospect_id"],
                    "action": "send_failed",
                    "reason": str(exc),
                }
            )

    if dirty:
        _write(records)
    return out


# ---------------------------------------------------------------------------
# Default soft-nudge composer
# ---------------------------------------------------------------------------


def _default_composer(rec: dict[str, Any]) -> tuple[str, str, str]:
    """Build the nudge email: short, single CTA back to the same buy_url.

    Brand-voice-compliant (no hype words, no fake scarcity, plain ask).
    """
    original_subject = str(rec.get("subject") or "the proposal")
    buy_url = str(rec.get("buy_url") or "").strip()
    subject = f"Re: {original_subject}"

    text_body = (
        "Quick one — I saw the proposal landed. If there's a single question "
        "holding you back, reply here and I'll answer it straight. Otherwise "
        "the link's still live:\n\n"
        f"{buy_url}\n\n"
        "— Alex\nHustleForge\n\n"
        "—\n"
        "HustleForge LLC · 2290 Cheim Boulevard, Marysville, CA 95901-3560\n"
        'Not interested? Just reply "unsubscribe" and you won\'t hear from me again.\n'
    )

    html_body = (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;'
        "font-size:15px;line-height:1.55;color:#1a1a1a;max-width:620px;margin:0 auto;"
        'padding:20px 16px;background:#ffffff">'
        "<p>Quick one — I saw the proposal landed. If there's a single question "
        "holding you back, reply here and I'll answer it straight. Otherwise "
        "the link's still live:</p>"
        '<table cellpadding="0" cellspacing="0" border="0" style="margin:18px auto"><tr>'
        '<td align="center" bgcolor="#2a6df4" style="border-radius:6px">'
        f'<a href="{buy_url}" '
        'style="display:inline-block;padding:14px 32px;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;'
        'font-size:16px;font-weight:700;color:#ffffff;background:#2a6df4;border-radius:6px;text-decoration:none">'
        "Pick up where you left off &rarr;</a></td></tr></table>"
        "<p>— Alex<br>HustleForge</p>"
        '<div style="margin:24px 0 0;padding-top:12px;border-top:1px solid #e2e2e2;'
        'font-size:12px;line-height:1.5;color:#8a8a8a;text-align:center">'
        "HustleForge LLC &middot; 2290 Cheim Boulevard, Marysville, CA 95901-3560<br>"
        "Not interested? Just reply &quot;unsubscribe&quot; and you won't hear from me again."
        "</div></div>"
    )
    return subject, html_body, text_body
