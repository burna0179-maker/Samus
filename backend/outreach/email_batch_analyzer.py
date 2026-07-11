"""Email outreach outcome audit — the deliverability/reputation parallel to
:mod:`backend.voice.call_batch_analyzer`.

The call audit watches Morgan's spoken behavior; this watches the email
channel's SENDER REPUTATION. SES was permanently denied largely because an
unwatched complaint/bounce rate is invisible until the provider flags you.
SendGrid is now the sole send path, so it must be guarded the same way: pull the
authoritative daily stats (read-only ``GET /v3/stats``), trend
delivery/bounce/spam/open rates across sends, and ALERT the moment a reputation
threshold is breached — before deliverability craters.

Read-only SendGrid: only ``GET /v3/stats``. Never sends or mutates.

Autonomous: :func:`autonomous_audit` runs from the post-production reconcile
sweep (``backend.voice.reconcile_cli``) alongside the call audit, so the email
channel is trended and reputation-alerted with NO operator invocation. Durable
store ``outreach/email_stats_analyses.jsonl`` under ``storage.root()``.

CLI::

    python -m backend.outreach.email_batch_analyzer [--days N] [--no-persist]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from backend.common import storage
from backend.common.config import get_settings
from backend.common.dates import iso_now

_LOG = logging.getLogger("samus.outreach.email_batch_analyzer")

_STORE_FILE = "outreach/email_stats_analyses.jsonl"

# Reputation guards (industry-standard sender-health thresholds). Breaching any
# of these is what gets a sender throttled or blocklisted — the exact failure
# mode that killed the SES account.
_SPAM_RATE_MAX = 0.001  # 0.1% complaint rate — Gmail/Yahoo danger line
_BOUNCE_RATE_MAX = 0.05  # 5% hard-bounce rate — SendGrid pauses above this
_BLOCK_RATE_MAX = 0.05  # 5% blocks — reputation/authentication trouble
_DELIVERY_RATE_MIN = 0.90  # <90% delivered (once events accrue) is a red flag
# Floor of requested emails before "zero delivered" is treated as a total
# outage rather than a just-sent batch still mid-accrual. Daily batches are ~87.
_ZERO_DELIVERY_MIN_REQUESTS = 20


def _rate(numer: float, denom: float) -> float:
    return round(numer / denom, 4) if denom else 0.0


# ---------------------------------------------------------------------------
# Fetch (read-only) + flatten
# ---------------------------------------------------------------------------
def fetch_stats(
    api_key: str,
    *,
    start_date: str,
    end_date: str | None = None,
    base_url: str = "https://api.sendgrid.com",
    http_client: httpx.Client | None = None,
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    """Return SendGrid's daily aggregated stats list. Read-only. Fail-soft:
    any error logs and returns ``[]`` (a missing audit must never block sends)."""
    url = f"{base_url.rstrip('/')}/v3/stats?start_date={start_date}&aggregated_by=day"
    if end_date:
        url += f"&end_date={end_date}"
    headers = {"Authorization": f"Bearer {api_key}"}
    own = http_client is None
    client = http_client or httpx.Client(timeout=timeout)
    try:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as exc:  # noqa: BLE001 — read-only audit, never raise
        _LOG.warning("email stats fetch failed: %s", exc)
        return []
    finally:
        if own:
            client.close()


_METRIC_KEYS = (
    "requests",
    "processed",
    "delivered",
    "deferred",
    "bounces",
    "bounce_drops",
    "blocks",
    "spam_reports",
    "spam_report_drops",
    "invalid_emails",
    "opens",
    "unique_opens",
    "clicks",
    "unique_clicks",
    "unsubscribes",
)


def _flatten(daily: list[dict[str, Any]]) -> dict[str, int]:
    """Sum every metric across all days/stat-rows into one totals dict."""
    totals: dict[str, int] = {k: 0 for k in _METRIC_KEYS}
    for day in daily or []:
        for row in day.get("stats") or []:
            metrics = row.get("metrics") or {}
            for k in _METRIC_KEYS:
                try:
                    totals[k] += int(metrics.get(k, 0) or 0)
                except (TypeError, ValueError):
                    continue
    return totals


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
@dataclass
class EmailMetrics:
    window_start: str
    window_end: str
    analyzed_ts: str
    requests: int = 0
    delivered: int = 0
    bounces: int = 0
    blocks: int = 0
    spam_reports: int = 0
    invalid_emails: int = 0
    deferred: int = 0
    unique_opens: int = 0
    unique_clicks: int = 0
    unsubscribes: int = 0
    delivery_rate: float = 0.0
    bounce_rate: float = 0.0
    block_rate: float = 0.0
    spam_rate: float = 0.0
    open_rate: float = 0.0
    click_rate: float = 0.0
    unsub_rate: float = 0.0


def aggregate(
    daily: list[dict[str, Any]],
    *,
    window_start: str,
    window_end: str,
    analyzed_ts: str | None = None,
) -> EmailMetrics:
    t = _flatten(daily)
    req = t["requests"] or t["processed"]
    delivered = t["delivered"]
    denom_deliv = delivered or req  # opens/spam are vs delivered; fall back to req
    return EmailMetrics(
        window_start=window_start,
        window_end=window_end,
        analyzed_ts=analyzed_ts or iso_now(),
        requests=req,
        delivered=delivered,
        bounces=t["bounces"],
        blocks=t["blocks"],
        spam_reports=t["spam_reports"],
        invalid_emails=t["invalid_emails"],
        deferred=t["deferred"],
        unique_opens=t["unique_opens"],
        unique_clicks=t["unique_clicks"],
        unsubscribes=t["unsubscribes"],
        delivery_rate=_rate(delivered, req),
        bounce_rate=_rate(t["bounces"], req),
        block_rate=_rate(t["blocks"], req),
        spam_rate=_rate(t["spam_reports"], denom_deliv),
        open_rate=_rate(t["unique_opens"], denom_deliv),
        click_rate=_rate(t["unique_clicks"], denom_deliv),
        unsub_rate=_rate(t["unsubscribes"], denom_deliv),
    )


# ---------------------------------------------------------------------------
# Reputation guards
# ---------------------------------------------------------------------------
@dataclass
class RepAlert:
    severity: str  # "critical" | "warning"
    metric: str
    message: str


def reputation_alerts(m: EmailMetrics) -> list[RepAlert]:
    """Threshold breaches that threaten sender reputation. Empty == healthy."""
    out: list[RepAlert] = []
    if m.spam_reports > 0 or m.spam_rate > _SPAM_RATE_MAX:
        out.append(
            RepAlert(
                "critical",
                "spam_rate",
                f"{m.spam_reports} spam complaint(s) (rate {m.spam_rate:.2%}). Complaints "
                f"are the fastest way to a blocklist — PAUSE sending, review targeting/"
                f"content, and confirm every recipient is genuinely opted-appropriate.",
            )
        )
    if m.requests and m.bounce_rate > _BOUNCE_RATE_MAX:
        out.append(
            RepAlert(
                "warning",
                "bounce_rate",
                f"Bounce rate {m.bounce_rate:.1%} exceeds {_BOUNCE_RATE_MAX:.0%}. Tighten "
                f"email verification before send; SendGrid throttles high-bounce senders.",
            )
        )
    if m.requests and m.block_rate > _BLOCK_RATE_MAX:
        out.append(
            RepAlert(
                "warning",
                "block_rate",
                f"Block rate {m.block_rate:.1%} exceeds {_BLOCK_RATE_MAX:.0%}. Check domain "
                f"authentication (SPF/DKIM/DMARC) and recipient-domain reputation.",
            )
        )
    # Total delivery failure: mail was requested but NONE delivered. This is the
    # worst case — e.g. a SendGrid sending hold (billing pause / account review /
    # IP provisioning) leaves every message stuck in "processing": accepted but
    # never released. The old guard `if m.delivered` SILENTLY SUPPRESSED exactly
    # this case (delivered==0 is falsy), so a total outage read as "healthy".
    # Require a request floor so a tiny just-sent batch mid-accrual doesn't alarm.
    if m.requests >= _ZERO_DELIVERY_MIN_REQUESTS and not m.delivered:
        out.append(
            RepAlert(
                "critical",
                "delivery_rate",
                f"ZERO delivered of {m.requests} requested ({m.window_start}..{m.window_end}). "
                f"Total email outage — messages accepted by SendGrid but not delivered "
                f"(likely a sending hold: billing pause, account review, or IP provisioning). "
                f"PAUSE outbound email and resolve the SendGrid account state before sending more.",
            )
        )
    # Partial degradation: some mail delivered, but rate below the health floor.
    elif m.delivered and m.delivery_rate < _DELIVERY_RATE_MIN:
        out.append(
            RepAlert(
                "warning",
                "delivery_rate",
                f"Delivery rate {m.delivery_rate:.1%} below {_DELIVERY_RATE_MIN:.0%}. "
                f"Investigate bounces/blocks/deferrals.",
            )
        )
    return out


# ---------------------------------------------------------------------------
# Trend + durable store
# ---------------------------------------------------------------------------
@dataclass
class EmailTrend:
    has_prior: bool
    prior_window: str | None = None
    deltas: dict[str, list[float]] = field(default_factory=dict)  # metric -> [prev, cur]


_TRENDED = ("delivery_rate", "bounce_rate", "spam_rate", "open_rate", "click_rate")


def compute_trend(cur: EmailMetrics, prior: EmailMetrics | None) -> EmailTrend:
    if prior is None:
        return EmailTrend(has_prior=False)
    deltas = {k: [getattr(prior, k), getattr(cur, k)] for k in _TRENDED}
    return EmailTrend(has_prior=True, prior_window=prior.window_end, deltas=deltas)


def _store_path() -> Path:
    return storage.root() / _STORE_FILE


def append_metrics(m: EmailMetrics) -> None:
    try:
        p = _store_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(m), ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        _LOG.warning("email stats append failed: %s", exc)


def load_prior() -> EmailMetrics | None:
    p = _store_path()
    if not p.exists():
        return None
    try:
        lines = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        if not lines:
            return None
        return EmailMetrics(**json.loads(lines[-1]))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("email stats load failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Autonomous audit + artifacts
# ---------------------------------------------------------------------------
def _write_report(m: EmailMetrics, alerts: list[RepAlert]) -> None:
    try:
        d = storage.root() / "outreach" / "email_audits"
        d.mkdir(parents=True, exist_ok=True)
        lines = [
            f"=== EMAIL OUTCOME AUDIT {m.window_start}..{m.window_end} ===",
            f"requests={m.requests} delivered={m.delivered} "
            f"delivery={m.delivery_rate:.0%} bounce={m.bounce_rate:.1%} "
            f"spam={m.spam_rate:.2%} open={m.open_rate:.0%} click={m.click_rate:.0%}",
            f"bounces={m.bounces} blocks={m.blocks} spam_reports={m.spam_reports} "
            f"unsub={m.unsubscribes}",
        ]
        if alerts:
            lines.append("-- REPUTATION ALERTS --")
            lines += [f"  [{a.severity}] {a.metric}: {a.message}" for a in alerts]
        else:
            lines.append("reputation: healthy (no thresholds breached)")
        (d / "latest.txt").write_text("\n".join(lines), encoding="utf-8")
    except OSError as exc:
        _LOG.warning("email audit report write failed: %s", exc)


def _write_alert(m: EmailMetrics, alerts: list[RepAlert]) -> None:
    try:
        d = storage.root() / "outreach" / "email_audit_alerts"
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", f"{m.window_start}_{m.window_end}")
        payload = {
            "ts": iso_now(),
            "window": [m.window_start, m.window_end],
            "alerts": [asdict(a) for a in alerts],
            "metrics": asdict(m),
        }
        (d / f"alert_{safe}.json").write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )
    except OSError as exc:
        _LOG.warning("email audit alert write failed: %s", exc)


def autonomous_audit(
    *,
    days_back: int = 7,
    api_key: str | None = None,
    base_url: str | None = None,
    http_client: httpx.Client | None = None,
    persist: bool = True,
) -> EmailMetrics | None:
    """Audit the email channel's outcomes with NO operator in the loop.

    Fetches the last ``days_back`` days of SendGrid stats, computes metrics,
    dedup-appends to the trend store (only when send volume changed), writes the
    report artifact, and on a reputation-threshold breach writes an operator
    alert. Never raises. Returns the metrics (or None on failure / no key)."""
    try:
        settings = get_settings()
        key = api_key if api_key is not None else settings.sendgrid_api_key
        if not key:
            _LOG.warning("no SendGrid API key — email audit skipped")
            return None
        resolved_base = (
            base_url
            if base_url is not None
            else (getattr(settings, "sendgrid_base_url", "") or "https://api.sendgrid.com")
        )
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")
        end = now.strftime("%Y-%m-%d")
        daily = fetch_stats(
            key, start_date=start, end_date=end, base_url=resolved_base, http_client=http_client
        )
        m = aggregate(daily, window_start=start, window_end=end)
        alerts = reputation_alerts(m)

        if persist:
            prior = load_prior()
            if prior is None or prior.requests != m.requests:
                append_metrics(m)
        _write_report(m, alerts)
        if alerts:
            _write_alert(m, alerts)
            worst = "critical" if any(a.severity == "critical" for a in alerts) else "warning"
            _LOG.warning(
                "EMAIL REPUTATION %s: spam=%d bounce=%.1f%% block=%.1f%% "
                "(window %s..%s) — see email_audit_alerts/",
                worst.upper(),
                m.spam_reports,
                m.bounce_rate * 100,
                m.block_rate * 100,
                m.window_start,
                m.window_end,
            )
        return m
    except Exception as exc:  # noqa: BLE001 — never disturb the caller
        _LOG.warning("email autonomous_audit failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.outreach.email_batch_analyzer",
        description="Email outcome audit — deliverability/reputation trend + "
        "alerts. Read-only SendGrid Stats API.",
    )
    parser.add_argument("--days", type=int, default=7, help="Lookback window (default 7).")
    parser.add_argument("--no-persist", action="store_true", help="Do not append to trend store.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    m = autonomous_audit(days_back=args.days, persist=not args.no_persist)
    if m is None:
        print("email audit unavailable (no key / fetch failed).")
        return 2
    alerts = reputation_alerts(m)
    print(f"=== EMAIL OUTCOME AUDIT {m.window_start}..{m.window_end} ===")
    print(
        f"requests={m.requests} delivered={m.delivered} delivery={m.delivery_rate:.0%} "
        f"bounce={m.bounce_rate:.1%} spam={m.spam_rate:.2%} open={m.open_rate:.0%}"
    )
    if alerts:
        for a in alerts:
            print(f"  [{a.severity}] {a.metric}: {a.message}")
        return 3
    print("reputation: healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
