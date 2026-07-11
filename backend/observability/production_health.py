"""Production-failure monitor - detects when Samus's autonomous production
loop has stalled or a leg has gone dark, so a silent failure surfaces the
next morning instead of days later.

Motivating incident (2026-07-06): the Gmail OAuth access token expired
2026-07-02 and every 10-minute inbox poll failed silently for ~4 days.
Outbound kept sending 19+ emails/day while inbound - including the opt-out
replies the ComplianceGuard depends on - was never ingested. Nothing watched
the poll's exit code or the token's expiry, so the dangerous asymmetry
(outbound live, inbound blind => opted-out recipients still emailed =>
CAN-SPAM exposure) went unnoticed. This module is the watchdog that would
have caught it on day one.

Read-only by construction. Every check only reads state (files, Windows Task
Scheduler results, epoch clocks). It never actuates production (it calls
idle_production's OBSERVER, never its producer / run_idle_drive), never
mutates, and never raises out of :func:`check_production_health` - a check
that faults degrades to an UNKNOWN finding, matching the morning brief's
best-effort, never-break-the-brief contract.

Placement: intended to run on the host (where the morning brief and the
Windows scheduled tasks live). In a container the Windows-task check degrades
to UNKNOWN and the host-path reads degrade to "absent"; the brief that emails
the operator runs on the host, so that is where the checks light up.

Wire: enabled by default; kill-switch ``SAMUS_PRODUCTION_HEALTH_CHECK_ENABLED``
(set 0/false/off to silence). Kept as a self-contained env read rather than a
settings field to stay surgical.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

_LOG = logging.getLogger("samus.observability.production_health")


# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------


class HealthStatus:
    OK = "OK"
    INFO = "INFO"
    UNKNOWN = "UNKNOWN"
    WARN = "WARN"
    FAIL = "FAIL"
    CRITICAL = "CRITICAL"


# Higher rank = more severe. UNKNOWN sits below WARN: an environment where a
# check cannot run (e.g. Windows-task query inside a Linux container) is not an
# alert, just an absence of signal. Only WARN and above are surfaced.
_RANK = {
    HealthStatus.OK: 0,
    HealthStatus.INFO: 1,
    HealthStatus.UNKNOWN: 2,
    HealthStatus.WARN: 3,
    HealthStatus.FAIL: 4,
    HealthStatus.CRITICAL: 5,
}

_ALERT_STATUSES = (HealthStatus.WARN, HealthStatus.FAIL, HealthStatus.CRITICAL)


# ---------------------------------------------------------------------------
# Thresholds / configuration
# ---------------------------------------------------------------------------

# How long PAST access-token expiry before we treat the refresh as broken. The
# Gmail access token is short-lived (~1h) and refreshed on every poll (~10 min),
# so it is ALWAYS near expiry - warning on imminent expiry is pure noise. Only a
# token expired well beyond the poll cadence means the refresh has actually
# stopped (the real outage of 2026-07-02). 2h is comfortably past the cadence.
_OAUTH_STALE_GRACE_S = 2 * 3600.0

# "Outbound is actively producing" window. If the last placed call / sent email
# is within this span we treat outbound as live for the asymmetry check.
_OUTBOUND_RECENT_S = 24 * 3600.0

# Scheduled-task Last Result codes (Windows) that are not failures.
_TASK_RESULT_OK = 0
_TASK_RESULT_RUNNING = 267009  # 0x41301 SCHED_S_TASK_RUNNING
_TASK_RESULT_NOT_RUN = 267011  # 0x41303 SCHED_S_TASK_HAS_NOT_RUN

# Default host scheduled tasks whose exit code gates a production leg. The inbox
# poll is the inbound-ingest leg; a nonzero Last Result means it is failing.
_DEFAULT_WATCHED_TASKS = ("Samus Inbox Poll",)


def _enabled() -> bool:
    return os.getenv("SAMUS_PRODUCTION_HEALTH_CHECK_ENABLED", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _on_windows() -> bool:
    """Platform seam - the scheduled-task check only works on the Windows host."""
    return os.name == "nt"


def _watched_tasks() -> tuple[str, ...]:
    raw = os.getenv("SAMUS_HEALTH_WATCHED_TASKS", "").strip()
    if not raw:
        return _DEFAULT_WATCHED_TASKS
    names = tuple(n.strip() for n in raw.split(",") if n.strip())
    return names or _DEFAULT_WATCHED_TASKS


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass
class HealthCheck:
    """One production-health finding."""

    name: str
    status: str
    detail: str
    remediation: str = ""

    def is_alert(self) -> bool:
        return self.status in _ALERT_STATUSES


@dataclass
class ProductionHealthReport:
    checks: list[HealthCheck] = field(default_factory=list)
    generated_ts: float = 0.0
    enabled: bool = True

    @property
    def worst_status(self) -> str:
        if not self.checks:
            return HealthStatus.OK
        return max((c.status for c in self.checks), key=lambda s: _RANK.get(s, 0))

    def alerts(self) -> list[HealthCheck]:
        """Alert-level findings, most severe first."""
        return sorted(
            (c for c in self.checks if c.is_alert()),
            key=lambda c: _RANK.get(c.status, 0),
            reverse=True,
        )

    def has_alerts(self) -> bool:
        return any(c.is_alert() for c in self.checks)

    def has_failures(self) -> bool:
        return any(c.status in (HealthStatus.FAIL, HealthStatus.CRITICAL) for c in self.checks)

    def summary_line(self) -> str:
        counts: dict[str, int] = {}
        for c in self.checks:
            counts[c.status] = counts.get(c.status, 0) + 1
        parts = [f"{counts[s]} {s.lower()}" for s in _RANK if counts.get(s)]
        return "production health: " + (", ".join(parts) or "no checks")


# ---------------------------------------------------------------------------
# Individual checks - each is defensive and returns a HealthCheck (never raises)
# ---------------------------------------------------------------------------


def _settings():
    from backend.common.settings import get_settings

    return get_settings()


def check_oauth_token(
    *,
    now_ts: Optional[float] = None,
    token_path: Optional[str] = None,
    inbox_configured: Optional[bool] = None,
) -> HealthCheck:
    """Gmail OAuth token freshness - the exact failure of 2026-07-02.

    An expired ``access_token`` that has not been refreshed means the refresh
    exchange is failing (the client rewrites the file on every successful
    refresh), so the inbox poll cannot authenticate. FAIL on expired, WARN when
    expiry is imminent.
    """
    name = "oauth_token"
    now = now_ts if now_ts is not None else time.time()
    try:
        s = _settings()
        if inbox_configured is None:
            inbox_configured = bool(getattr(s, "gmail_inbox_email", ""))
        if not inbox_configured:
            return HealthCheck(
                name,
                HealthStatus.INFO,
                "gmail inbox not configured - inbound leg intentionally off",
            )
        path = Path(token_path or getattr(s, "gmail_oauth_token_path", ""))
    except Exception as exc:  # noqa: BLE001
        return HealthCheck(name, HealthStatus.UNKNOWN, f"settings unreadable: {exc}")

    if not path or not path.exists():
        return HealthCheck(
            name,
            HealthStatus.FAIL,
            f"Gmail OAuth token file missing ({path}) - inbox poll cannot authenticate",
            remediation="run the one-time consent: python -m backend.intake.gmail_oauth",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        expires_at = int(data.get("expires_at") or 0)
    except Exception as exc:  # noqa: BLE001
        return HealthCheck(name, HealthStatus.UNKNOWN, f"token file unreadable: {exc}")

    if expires_at <= 0:
        return HealthCheck(name, HealthStatus.UNKNOWN, "token has no expires_at field")

    delta = expires_at - now
    if delta < -_OAUTH_STALE_GRACE_S:
        return HealthCheck(
            name,
            HealthStatus.FAIL,
            f"Gmail OAuth token expired {abs(delta) / 3600.0:.1f}h ago - refresh has stopped, "
            f"inbox poll is down",
            remediation=(
                "the stored client secret likely no longer matches the OAuth key "
                "(invalid_client) or the refresh token was revoked (invalid_grant): update the "
                "DPAPI GmailOauthClientSecret in the poll's run-as account (it runs as Alex), or "
                "re-run consent (python -m backend.intake.gmail_oauth)"
            ),
        )
    if delta < 0:
        # Lapsed but within grace - the next poll refreshes it. Healthy.
        return HealthCheck(
            name,
            HealthStatus.OK,
            f"Gmail OAuth token lapsed {abs(delta) / 3600.0:.1f}h ago, within refresh grace",
        )
    return HealthCheck(
        name,
        HealthStatus.OK,
        f"Gmail OAuth token valid for {delta / 3600.0:.1f}h (auto-refreshed each poll)",
    )


def _query_scheduled_task(name: str) -> Optional[dict[str, str]]:
    """Return {'last_result','last_run','status'} for a Windows task, or None.

    None means the task could not be queried (not registered, schtasks absent).
    Windows-only; callers guard on platform.
    """
    try:
        proc = subprocess.run(
            ["schtasks", "/query", "/tn", name, "/fo", "LIST", "/v"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception:  # noqa: BLE001 - schtasks missing / blocked
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    out = proc.stdout

    def _grab(label: str) -> str:
        m = re.search(rf"^{re.escape(label)}:\s*(.+?)\s*$", out, re.MULTILINE)
        return m.group(1).strip() if m else ""

    return {
        "last_result": _grab("Last Result"),
        "last_run": _grab("Last Run Time"),
        "status": _grab("Status"),
    }


def check_scheduled_tasks(*, task_names: Optional[tuple[str, ...]] = None) -> list[HealthCheck]:
    """Windows scheduled-task exit codes for the production-critical tasks.

    A nonzero Last Result means the task's last run failed - e.g. the inbox
    poll exiting 1 on a dead OAuth token. On a non-Windows host the check
    degrades to a single UNKNOWN (the tasks live on the Windows host).
    """
    if not _on_windows():
        return [
            HealthCheck(
                "scheduled_tasks",
                HealthStatus.UNKNOWN,
                "scheduled-task exit codes are only visible on the Windows host",
            )
        ]
    names = task_names or _watched_tasks()
    checks: list[HealthCheck] = []
    for name in names:
        info = _query_scheduled_task(name)
        cname = f"task:{name}"
        if info is None:
            checks.append(
                HealthCheck(
                    cname,
                    HealthStatus.UNKNOWN,
                    "task not registered or schtasks unavailable",
                )
            )
            continue
        raw = info.get("last_result", "")
        try:
            code = int(raw)
        except (TypeError, ValueError):
            checks.append(
                HealthCheck(cname, HealthStatus.UNKNOWN, f"unparseable Last Result {raw!r}")
            )
            continue
        last_run = info.get("last_run") or "unknown"
        if code == _TASK_RESULT_OK:
            checks.append(HealthCheck(cname, HealthStatus.OK, f"last run {last_run} succeeded"))
        elif code == _TASK_RESULT_RUNNING:
            checks.append(HealthCheck(cname, HealthStatus.INFO, "currently running"))
        elif code == _TASK_RESULT_NOT_RUN:
            checks.append(HealthCheck(cname, HealthStatus.WARN, "task has never run"))
        else:
            checks.append(
                HealthCheck(
                    cname,
                    HealthStatus.FAIL,
                    f"last run {last_run} failed (exit {code})",
                    remediation="inspect the task action; run it manually to see the error",
                )
            )
    return checks


def check_inbound_freshness(
    *,
    now_ts: Optional[float] = None,
    ledger_path: Optional[str] = None,
) -> HealthCheck:
    """How long since inbound email was last drained. Informational on its own -
    an empty inbox legitimately produces no new rows - but it feeds the
    asymmetry check together with the token/task signals."""
    name = "inbound_freshness"
    now = now_ts if now_ts is not None else time.time()
    try:
        path = Path(ledger_path or getattr(_settings(), "gmail_inbox_ledger_path", ""))
    except Exception as exc:  # noqa: BLE001
        return HealthCheck(name, HealthStatus.UNKNOWN, f"settings unreadable: {exc}")
    if not path or not path.exists():
        return HealthCheck(name, HealthStatus.INFO, "no inbound-drain ledger yet (none ingested)")
    try:
        last_ts = path.stat().st_mtime
    except Exception as exc:  # noqa: BLE001
        return HealthCheck(name, HealthStatus.UNKNOWN, f"ledger stat failed: {exc}")
    age_h = max(0.0, (now - last_ts) / 3600.0)
    return HealthCheck(name, HealthStatus.INFO, f"last inbound-drain write {age_h:.1f}h ago")


def check_outbound_activity(
    *,
    now_ts: Optional[float] = None,
    observer: Optional[Callable[[], object]] = None,
) -> tuple[HealthCheck, bool]:
    """Whether outbound production is live. Returns (check, produced_recently).

    Reuses idle_production's OBSERVER (never its producer) so the "last activity"
    read matches the drive's own bug-fixed date/convention logic exactly.
    """
    name = "outbound_activity"
    now = now_ts if now_ts is not None else time.time()
    try:
        if observer is None:
            from backend.cash_engine.idle_production import _default_observer

            observer = _default_observer
        obs = observer()
        last = getattr(obs, "last_activity_ts", None)
    except Exception as exc:  # noqa: BLE001
        return HealthCheck(name, HealthStatus.UNKNOWN, f"observer failed: {exc}"), False

    if last is None:
        return HealthCheck(
            name, HealthStatus.INFO, "no outbound activity in lookback window"
        ), False
    age = now - float(last)
    produced_recently = age <= _OUTBOUND_RECENT_S
    return (
        HealthCheck(name, HealthStatus.INFO, f"last outbound action {age / 3600.0:.1f}h ago"),
        produced_recently,
    )


def check_checkout_funnel(*, now_ts: Optional[float] = None) -> HealthCheck:
    """Whether the checkout funnel behind outbound marketing can convert.

    Reads the CACHED funnel-health snapshot (backend.catalog.funnel_health) —
    read-only by construction, no network. A degraded funnel while campaigns
    run is the quiet twin of the outbound/inbound asymmetry: sends go out,
    prospects click, the checkout is archived, conversion is zero, and the
    CODB burn (SendGrid + LLM tokens) purchases nothing.
    """
    name = "checkout_funnel"
    try:
        from backend.catalog.funnel_health import refresh_if_stale

        snap = refresh_if_stale(now_ts=now_ts)
    except Exception as exc:  # noqa: BLE001
        return HealthCheck(name, HealthStatus.UNKNOWN, f"snapshot read failed: {exc}")

    refresh_cmd = "python -m backend.catalog.funnel_health --wp"
    if snap.checked_ts <= 0:
        return HealthCheck(
            name,
            HealthStatus.UNKNOWN,
            "no funnel snapshot yet - link health unverified",
            remediation=refresh_cmd,
        )
    if not snap.is_fresh(now_ts=now_ts):
        # refresh_if_stale tried but Stripe was unreachable — still stale.
        return HealthCheck(
            name,
            HealthStatus.WARN,
            f"funnel snapshot stale ({snap.age_s(now_ts) / 3600.0:.1f}h old) - "
            "campaigns are running against unverified checkout links",
            remediation=refresh_cmd,
        )
    if snap.status == "degraded":
        return HealthCheck(
            name,
            HealthStatus.FAIL,
            snap.summary_line() + " - outbound marketing pushing these links converts zero",
            remediation="fix backend/catalog/registry.py / WordPress links, then " + refresh_cmd,
        )
    if snap.status == "ok":
        return HealthCheck(name, HealthStatus.OK, snap.summary_line())
    return HealthCheck(
        name,
        HealthStatus.UNKNOWN,
        snap.summary_line(),
        remediation=refresh_cmd,
    )


def evaluate_asymmetry(*, inbound_down: bool, outbound_recent: bool) -> Optional[HealthCheck]:
    """The crown-jewel check: outbound live while inbound ingest is down.

    This is the shape of the 2026-07-06 incident - it means replies and, most
    dangerously, opt-outs are not being processed while the agent keeps sending.
    """
    if inbound_down and outbound_recent:
        return HealthCheck(
            "outbound_inbound_asymmetry",
            HealthStatus.CRITICAL,
            "outbound is producing but inbound ingest is DOWN - replies and opt-outs are "
            "not processed, so opted-out recipients may still be emailed (CAN-SPAM exposure)",
            remediation="restore the inbox poll / OAuth token, or pause outbound sends until inbound recovers",
        )
    return None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def check_production_health(*, now_ts: Optional[float] = None) -> ProductionHealthReport:
    """Run every production-health check. Never raises; disabled => empty report."""
    now = now_ts if now_ts is not None else time.time()
    report = ProductionHealthReport(generated_ts=now, enabled=_enabled())
    if not report.enabled:
        return report

    token = check_oauth_token(now_ts=now)
    report.checks.append(token)

    task_checks = check_scheduled_tasks()
    report.checks.extend(task_checks)

    report.checks.append(check_inbound_freshness(now_ts=now))

    outbound, produced_recently = check_outbound_activity(now_ts=now)
    report.checks.append(outbound)

    report.checks.append(check_checkout_funnel(now_ts=now))

    # Inbound is "down" if the token failed or any watched inbound task failed.
    inbound_down = token.status == HealthStatus.FAIL or any(
        c.status == HealthStatus.FAIL for c in task_checks
    )
    asym = evaluate_asymmetry(inbound_down=inbound_down, outbound_recent=produced_recently)
    if asym is not None:
        report.checks.append(asym)

    return report


# ---------------------------------------------------------------------------
# CLI - ad-hoc run or a faster-than-daily scheduled cadence
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    """Print the report; exit 1 if any FAIL/CRITICAL so a scheduler can alert."""
    logging.basicConfig(
        level=os.getenv("SAMUS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    report = check_production_health()
    if not report.enabled:
        print("production health: disabled (SAMUS_PRODUCTION_HEALTH_CHECK_ENABLED)")
        return 0
    print(report.summary_line())
    for c in report.checks:
        print(f"  [{c.status:<8}] {c.name}: {c.detail}")
        if c.remediation and c.is_alert():
            print(f"             fix: {c.remediation}")
    return 1 if report.has_failures() else 0


if __name__ == "__main__":
    raise SystemExit(main())
