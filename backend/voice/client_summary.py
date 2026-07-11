"""Periodic call-summary report for AI Digital Receptionist clients.

The hybrid delivery model gives each client a read-only view of their call
activity. The pilot has no web portal, so that view is an emailed summary:
once a week (or monthly) the client gets a digest of every call the AI
receptionist handled — how many came in, how many were answered, voicemails,
appointment + callback requests, and the calls that need their attention.

Source of truth is the per-call ``calls/<call_id>/call.json`` files written
by :mod:`backend.voice.inbound_storage` — NOT ``voice_events.jsonl`` and NOT
CRM. That keeps this report a pure read over the same artifact tree a future
client portal would render from.

Rendering style matches :mod:`backend.retainer.visibility_report` /
``backend/seo/report.py``: ASCII markdown, plain bullets, so it renders
cleanly in plain-text email and a Windows console alike.

Operator/cron entry point::

    python -m backend.voice.client_summary --slug acme_plumbing
    python -m backend.voice.client_summary --slug acme_plumbing --cadence monthly --no-email
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from backend.common import storage
from backend.common.dates import iso_now
from backend.common.email_backend import EmailBackendError, send_email

from .models import InboundCallRecord, ReceptionistConfig
from .receptionist_config import load_config


_LOG = logging.getLogger("samus.voice.client_summary")

_CADENCE_DAYS = {"weekly": 7, "monthly": 30}


# ---------------------------------------------------------------------------
# Call-record loading
# ---------------------------------------------------------------------------


def _parse_ts(raw: str | None) -> datetime | None:
    """Parse an iso_now() (``...Z``) or Vapi ISO-8601 timestamp, or None."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _record_ts(rec: InboundCallRecord) -> datetime | None:
    """Best timestamp for windowing a call — persist time, then call times."""
    return _parse_ts(rec.written_at) or _parse_ts(rec.ended_at) or _parse_ts(rec.started_at)


def load_calls_in_window(
    slug: str,
    *,
    since: datetime,
    until: datetime,
) -> list[InboundCallRecord]:
    """Load every persisted inbound call for ``slug`` within [since, until].

    Skips unreadable / malformed ``call.json`` files (logged, not fatal).
    Returned oldest-first.
    """
    calls_root = storage.root() / "customers" / slug / "calls"
    out: list[InboundCallRecord] = []
    if not calls_root.is_dir():
        return out
    for child in sorted(calls_root.iterdir()):
        cj = child / "call.json"
        if not cj.is_file():
            continue
        try:
            rec = InboundCallRecord.model_validate_json(
                cj.read_text(encoding="utf-8"),
            )
        except Exception as exc:  # noqa: BLE001 — skip a bad file, keep going
            _LOG.warning("skipping unreadable call.json %s: %s", cj, exc)
            continue
        ts = _record_ts(rec)
        if ts is not None and since <= ts <= until:
            out.append(rec)
    out.sort(key=lambda r: _record_ts(r) or since)
    return out


# ---------------------------------------------------------------------------
# Renderer (pure — no I/O)
# ---------------------------------------------------------------------------


def render_call_summary(
    *,
    business_name: str,
    customer_slug: str,
    calls: list[InboundCallRecord],
    since: datetime,
    until: datetime,
    ts: str | None = None,
) -> str:
    """Render the call-activity digest as ASCII markdown. No I/O."""
    ts = ts or iso_now()
    window = f"{since:%Y-%m-%d} to {until:%Y-%m-%d}"
    name = business_name or customer_slug

    total = len(calls)
    answered = sum(1 for c in calls if c.answered)
    voicemails = sum(1 for c in calls if c.voicemail_left)
    appointments = sum(1 for c in calls if c.inbound_summary.appointment_requested)
    callbacks = sum(1 for c in calls if c.inbound_summary.callback_requested)
    urgent = sum(1 for c in calls if c.inbound_summary.urgent)
    total_seconds = sum(c.duration_sec for c in calls)
    answered_pct = round(100.0 * answered / total, 1) if total else 0.0
    total_minutes = round(total_seconds / 60.0, 1)

    lines: list[str] = []
    lines.append(f"# Call Summary - {name}")
    lines.append("")
    lines.append(f"**Period:** {window}")
    lines.append(f"**Generated:** {ts}")
    lines.append("")
    lines.append(f"Hi, here is how your AI receptionist handled calls for {name}.")
    lines.append("")

    # ----- Headline numbers -----
    lines.append("## At a glance")
    lines.append("")
    lines.append(f"- **Calls received:** {total}")
    lines.append(f"- **Answered:** {answered} ({answered_pct}%)")
    lines.append(f"- **Voicemails / messages taken:** {voicemails}")
    lines.append(f"- **Appointment requests:** {appointments}")
    lines.append(f"- **Callback requests:** {callbacks}")
    lines.append(f"- **Urgent calls flagged:** {urgent}")
    lines.append(f"- **Total talk time:** {total_minutes} min")
    lines.append("")

    # ----- Needs attention -----
    follow_ups = [
        c
        for c in calls
        if c.inbound_summary.appointment_requested
        or c.inbound_summary.callback_requested
        or c.inbound_summary.urgent
        or c.voicemail_left
    ]
    lines.append("## Needs your attention")
    lines.append("")
    if not follow_ups:
        lines.append("_Nothing outstanding - every call this period was handled._")
    else:
        for c in follow_ups:
            s = c.inbound_summary
            tags: list[str] = []
            if s.urgent:
                tags.append("URGENT")
            if s.appointment_requested:
                tags.append("appointment")
            if s.callback_requested:
                tags.append(f"callback {s.callback_number or c.caller_number}")
            if c.voicemail_left:
                tags.append("voicemail")
            caller = c.caller_number or "unknown caller"
            reason = s.reason_for_call or c.summary or "(no detail captured)"
            lines.append(f"- **{caller}** [{', '.join(tags)}] - {reason}")
    lines.append("")

    # ----- Full call log -----
    lines.append("## All calls")
    lines.append("")
    if not calls:
        lines.append("_No calls received this period._")
    else:
        lines.append("| When | Caller | Duration | Outcome |")
        lines.append("|---|---|---|---|")
        for c in calls:
            when = (c.ended_at or c.started_at or c.written_at or "")[:16].replace("T", " ")
            caller = c.caller_number or "unknown"
            dur = f"{c.duration_sec}s"
            outcome = _call_outcome_label(c)
            lines.append(f"| {when} | {caller} | {dur} | {outcome} |")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "Want to change your greeting, hours, or where urgent calls are "
        "forwarded? Just reply to this email.",
    )
    lines.append("")
    lines.append("- Morgan, HustleForge")
    lines.append("")
    lines.append(f"_Client: {customer_slug} | Period: {window} | Report: call_summary_v1_")
    return "\n".join(lines)


def _call_outcome_label(c: InboundCallRecord) -> str:
    """Short human label for one call row."""
    s = c.inbound_summary
    if not c.answered:
        return "missed"
    if s.transferred:
        return "transferred"
    if s.appointment_requested:
        return "appointment requested"
    if s.callback_requested:
        return "callback requested"
    if c.voicemail_left:
        return "message taken"
    return "handled"


# ---------------------------------------------------------------------------
# Build + send orchestrator
# ---------------------------------------------------------------------------


def build_and_send_summary(
    slug: str,
    *,
    cadence: str | None = None,
    now: datetime | None = None,
    send: bool = True,
    send_email_fn=None,
) -> dict:
    """Build one client's call summary, write it to disk, optionally email it.

    Returns a result dict (never raises on a config/IO/mail problem):
    ``ok``, ``slug``, ``calls``, ``report_path``, ``emailed``, ``error``.
    """
    result: dict = {
        "ok": False,
        "slug": slug,
        "calls": 0,
        "report_path": "",
        "emailed": False,
        "error": None,
    }
    config: ReceptionistConfig | None = load_config(slug)
    if config is None:
        result["error"] = "no_receptionist_config"
        return result

    cadence = (cadence or config.summary_cadence or "weekly").lower()
    days = _CADENCE_DAYS.get(cadence, 7)
    until = now or datetime.now(timezone.utc)
    since = until - timedelta(days=days)

    calls = load_calls_in_window(slug, since=since, until=until)
    result["calls"] = len(calls)

    body = render_call_summary(
        business_name=config.business_name,
        customer_slug=slug,
        calls=calls,
        since=since,
        until=until,
    )

    # Persist the rendered report alongside the client's config.
    summaries_dir = storage.root() / "customers" / slug / "receptionist" / "summaries"
    period_label = f"{since:%Y-%m-%d}_{until:%Y-%m-%d}"
    try:
        summaries_dir.mkdir(parents=True, exist_ok=True)
        report_path = summaries_dir / f"{period_label}.md"
        report_path.write_text(body, encoding="utf-8")
        result["report_path"] = str(report_path)
    except OSError as exc:
        result["error"] = f"report_write_failed: {exc}"
        return result

    # Email the client (best-effort).
    to = (config.summary_email or "").strip()
    if send and to:
        from functools import partial

        # Periodic call digest to a paying receptionist client = transactional/
        # relationship mail; tag the real-send fallback accordingly.
        sender = send_email_fn or partial(send_email, message_kind="transactional")
        subject = f"[{config.business_name or 'Receptionist'}] Call summary {period_label}"
        try:
            sender(to, subject, body, reply_to=config.summary_email or None)
            result["emailed"] = True
        except (EmailBackendError, NotImplementedError, ValueError, TypeError) as exc:
            result["error"] = f"email_failed: {exc}"
            # Report still written — partial success.
            result["ok"] = True
            return result
    elif send and not to:
        result["error"] = "no_summary_email_configured"

    result["ok"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.voice.client_summary",
        description="Build + send an AI Digital Receptionist call summary.",
    )
    parser.add_argument("--slug", required=True, help="customer slug (customers/<slug>/)")
    parser.add_argument(
        "--cadence",
        choices=("weekly", "monthly"),
        default=None,
        help="reporting window (default: the client's config)",
    )
    parser.add_argument(
        "--no-email", action="store_true", help="render + write the report but do not email it"
    )
    args = parser.parse_args(argv)

    result = build_and_send_summary(
        args.slug,
        cadence=args.cadence,
        send=not args.no_email,
    )
    if result["ok"]:
        print(f"call summary for {args.slug}: {result['calls']} call(s)")
        if result["report_path"]:
            print(f"  written: {result['report_path']}")
        print(f"  emailed: {result['emailed']}")
        if result["error"]:
            print(f"  note: {result['error']}", file=sys.stderr)
        return 0
    print(f"error: {result['error']}", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
