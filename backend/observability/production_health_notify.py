"""State-change email alerting for the production-failure monitor.

Runs :func:`check_production_health` and emails the operator's brief recipient
ONLY when the alert state CHANGES - a new or cleared failure - so a persistent
outage does not spam the inbox every 15 minutes. A recovery (alerts -> clean)
sends one "resolved" note, then silence. Detection stays in production_health;
this module is purely the fast-cadence email tripwire.

No parallel sender: reuses the morning brief's own channel
(:func:`backend.common.email_backend.send_email`, transactional kind) to the
same ``SAMUS_BRIEF_EMAIL_TO`` recipient. Discord/Telegram are intentionally out
of scope - the daily brief already carries those.

Fingerprint = the sorted (check-name, status) of alert-level findings, hashed.
Persisted under ``<artifact_root>/observability/production_health_alert_state.json``.
On a state read/write fault the module fails TOWARD alerting (sends this cycle)
rather than going silent - a monitor that hides its own faults is worse than a
duplicate email.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, Optional

from backend.observability.production_health import (
    HealthStatus,
    ProductionHealthReport,
    check_production_health,
)

_LOG = logging.getLogger("samus.observability.production_health_notify")

_STATE_BASENAME = "production_health_alert_state.json"


# ---------------------------------------------------------------------------
# State (last-sent fingerprint)
# ---------------------------------------------------------------------------

def _state_path() -> Optional[Path]:
    try:
        from backend.common import storage
        return storage.root() / "observability" / _STATE_BASENAME
    except Exception:  # noqa: BLE001
        return None


def _load_state(path: Optional[Path]) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_state(path: Optional[Path], state: dict) -> bool:
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
        return True
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("alert-state write failed: %s", exc)
        return False


def _fingerprint(report: ProductionHealthReport) -> str:
    """Stable hash of the alert set. Empty string when there are no alerts."""
    alerts = report.alerts()
    if not alerts:
        return ""
    key = "|".join(sorted(f"{c.name}:{c.status}" for c in alerts))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Email composition
# ---------------------------------------------------------------------------

def _counts_phrase(report: ProductionHealthReport) -> str:
    counts: dict[str, int] = {}
    for c in report.alerts():
        counts[c.status] = counts.get(c.status, 0) + 1
    order = (HealthStatus.CRITICAL, HealthStatus.FAIL, HealthStatus.WARN)
    parts = [f"{counts[s]} {s.lower()}" for s in order if counts.get(s)]
    return ", ".join(parts) or "0 alerts"


def _compose(report: ProductionHealthReport, *, recovered: bool, now_ts: float) -> tuple[str, str, str]:
    """Return (subject, text_body, html_body)."""
    stamp = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(now_ts))
    if recovered:
        subject = "Samus production RECOVERED - all checks green"
        lines = [
            f"Production health recovered as of {stamp}.",
            "",
            "All monitored legs are healthy again (OAuth token, inbox poll, "
            "outbound/inbound balance).",
        ]
    else:
        subject = f"Samus PRODUCTION ALERT: {_counts_phrase(report)}"
        lines = [f"Production health alert as of {stamp}.", "", report.summary_line(), ""]
        for c in report.alerts():
            lines.append(f"[{c.status}] {c.name}: {c.detail}")
            if c.remediation:
                lines.append(f"    fix: {c.remediation}")
    lines += [
        "",
        "-- fast-cadence health tripwire (backend.observability.production_health_notify).",
        "Full situational context is in the daily morning brief.",
    ]
    text = "\n".join(lines)
    html = (
        "<html><body><pre style=\"font-family: Consolas, monospace; font-size: 13px;\">"
        + text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        + "</pre></body></html>"
    )
    return subject, text, html


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def dispatch(
    *,
    report: Optional[ProductionHealthReport] = None,
    checker: Optional[Callable[[], ProductionHealthReport]] = None,
    sender: Optional[Callable[..., dict]] = None,
    recipient: Optional[str] = None,
    state_path: Optional[Path] = None,
    now_ts: Optional[float] = None,
) -> dict:
    """Send an email iff the alert state changed since the last send.

    Returns a small dict describing the action taken:
    ``disabled`` | ``no_recipient`` | ``unchanged`` | ``alerted`` |
    ``recovered`` | ``send_failed``. Never raises.
    """
    now = now_ts if now_ts is not None else time.time()
    if report is None:
        report = (checker or check_production_health)()
    if not report.enabled:
        return {"action": "disabled"}

    recipient = recipient if recipient is not None else (os.getenv("SAMUS_BRIEF_EMAIL_TO") or "").strip()
    path = state_path if state_path is not None else _state_path()

    fp = _fingerprint(report)
    prev = _load_state(path)
    prev_fp = str(prev.get("fingerprint", ""))

    if fp == prev_fp:
        return {"action": "unchanged", "fingerprint": fp}

    # State changed. Alert on new/changed failures; a transition to empty is a
    # recovery (only worth an email if we had previously alerted).
    recovered = (fp == "" and prev_fp != "")
    if fp == "" and prev_fp == "":
        return {"action": "unchanged", "fingerprint": fp}

    if not recipient:
        # Nothing we can do but record the state so we do not re-alert forever.
        _save_state(path, {"fingerprint": fp, "sent_ts": now, "worst": report.worst_status})
        return {"action": "no_recipient", "fingerprint": fp}

    subject, text, html = _compose(report, recovered=recovered, now_ts=now)
    send = sender
    if send is None:
        from backend.common.email_backend import send_email
        send = send_email
    try:
        send(
            to=recipient,
            subject=subject,
            body=text,
            html_body=html,
            message_kind="transactional",
        )
    except Exception as exc:  # noqa: BLE001 - a send fault must not crash the scheduler
        _LOG.warning("production-health alert send failed: %s", exc)
        # Do NOT persist state on failure, so the next cycle retries the send.
        return {"action": "send_failed", "fingerprint": fp, "error": str(exc)}

    _save_state(path, {"fingerprint": fp, "sent_ts": now, "worst": report.worst_status})
    return {"action": "recovered" if recovered else "alerted", "fingerprint": fp, "to": recipient}


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=os.getenv("SAMUS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    result = dispatch()
    print(f"production-health notify: {result.get('action')} ({result.get('fingerprint', '')})")
    return 1 if result.get("action") == "send_failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
