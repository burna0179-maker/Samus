"""Voice CODB tracker — aggregates Vapi + Twilio costs for the finance workcell.

Three data sources:

1. **Per-call Vapi cost** — extracted from ``voice_events.jsonl`` where
   ``kind=end_of_call`` carries ``vapi_cost`` (float, USD). The webhook
   receives this from Vapi's ``end-of-call-report`` payload. Aggregated
   daily and rolled into the CODB summary.

2. **Vapi call list polling** — ``GET /call?limit=100`` returns ``cost``
   on each call. Used as a cross-check / back-fill when webhook events
   are missing cost data (early calls before the cost field was wired).

3. **Twilio usage records** — ``GET /2010-04-01/Accounts/{sid}/Usage/Records``
   returns category-level spend for a date range. Polled daily for the
   ``calls``, ``sms``, and ``phonenumbers`` categories.

The tracker exposes a ``voice_codb_snapshot()`` function that returns a
structured summary ready for the finance workcell's CODB pipeline.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

_LOG = logging.getLogger("samus.voice.cost_tracker")

_EVENTS_PATH_DEFAULT = "/opt/samus/data/voice/voice_events.jsonl"
_CACHE_TTL_SEC = float(os.getenv("SAMUS_VOICE_COST_CACHE_TTL", "3600"))

_lock = threading.Lock()
_cached_snapshot: VoiceCostSnapshot | None = None
_cached_at: float = 0.0


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class VapiCostSummary:
    """Aggregated Vapi call costs for a date range."""
    total_usd: float = 0.0
    call_count: int = 0
    avg_cost_per_call: float = 0.0
    avg_duration_sec: float = 0.0
    period_start: str = ""
    period_end: str = ""


@dataclass
class TwilioCostSummary:
    """Aggregated Twilio usage costs for a date range."""
    calls_usd: float = 0.0
    sms_usd: float = 0.0
    phone_numbers_usd: float = 0.0
    total_usd: float = 0.0
    account_balance_usd: float | None = None
    period_start: str = ""
    period_end: str = ""
    reachable: bool = False
    error: str = ""


@dataclass
class VoiceCostSnapshot:
    """Combined Vapi + Twilio cost snapshot for the CODB pipeline."""
    ts: str = ""
    vapi: VapiCostSummary = field(default_factory=VapiCostSummary)
    twilio: TwilioCostSummary = field(default_factory=TwilioCostSummary)
    estimated_monthly_vapi_usd: float = 0.0
    estimated_monthly_twilio_usd: float = 0.0
    estimated_monthly_voice_total_usd: float = 0.0


# ---------------------------------------------------------------------------
# Vapi cost aggregation from local ledger
# ---------------------------------------------------------------------------

def _events_path() -> Path:
    return Path(os.getenv("SAMUS_VOICE_EVENTS_PATH", _EVENTS_PATH_DEFAULT))


def aggregate_vapi_costs(days: int = 30) -> VapiCostSummary:
    """Sum per-call Vapi costs from voice_events.jsonl over the last N days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_str = cutoff.isoformat()
    path = _events_path()
    total = 0.0
    count = 0
    total_duration = 0.0

    if not path.exists():
        _LOG.debug("voice_events.jsonl not found at %s", path)
        return VapiCostSummary(
            period_start=cutoff.strftime("%Y-%m-%d"),
            period_end=date.today().isoformat(),
        )

    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("kind") not in ("end_of_call", "inbound_end_of_call"):
                    continue
                ts = rec.get("ts", "")
                if ts < cutoff_str:
                    continue
                cost = rec.get("vapi_cost")
                if cost is not None:
                    try:
                        total += float(cost)
                        count += 1
                    except (TypeError, ValueError):
                        pass
                dur = rec.get("duration_seconds")
                if dur is not None:
                    try:
                        total_duration += float(dur)
                    except (TypeError, ValueError):
                        pass
    except OSError as exc:
        _LOG.warning("failed to read voice_events.jsonl: %s", exc)

    avg_cost = round(total / count, 4) if count else 0.0
    avg_dur = round(total_duration / count, 1) if count else 0.0

    return VapiCostSummary(
        total_usd=round(total, 4),
        call_count=count,
        avg_cost_per_call=avg_cost,
        avg_duration_sec=avg_dur,
        period_start=cutoff.strftime("%Y-%m-%d"),
        period_end=date.today().isoformat(),
    )


# ---------------------------------------------------------------------------
# Vapi API cost back-fill (cross-check via REST)
# ---------------------------------------------------------------------------

def fetch_vapi_call_costs(api_key: str, *, limit: int = 100) -> VapiCostSummary:
    """Fetch recent calls from Vapi REST API and sum their costs.

    This supplements the local ledger for calls where the webhook may not
    have captured cost data (pre-wiring, webhook failures).
    """
    if not api_key:
        return VapiCostSummary()
    try:
        from .client import VapiClient
        client = VapiClient(api_key=api_key)
        calls = client.list_calls(limit=limit)
    except Exception as exc:
        _LOG.warning("vapi call cost fetch failed: %s", exc)
        return VapiCostSummary()

    total = 0.0
    count = 0
    for c in calls:
        if c.cost is not None:
            total += c.cost
            count += 1

    return VapiCostSummary(
        total_usd=round(total, 4),
        call_count=count,
        avg_cost_per_call=round(total / count, 4) if count else 0.0,
    )


# ---------------------------------------------------------------------------
# Twilio usage + balance
# ---------------------------------------------------------------------------

def fetch_twilio_costs(
    account_sid: str,
    auth_token: str,
    *,
    days: int = 30,
) -> TwilioCostSummary:
    """Query Twilio Usage Records API for call/SMS/number costs.

    Also fetches account balance via ``GET /2010-04-01/Accounts/{sid}.json``.
    """
    if not account_sid or not auth_token:
        return TwilioCostSummary(error="twilio_credentials_not_configured")

    end_date = date.today()
    start_date = end_date - timedelta(days=days)
    base = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}"

    summary = TwilioCostSummary(
        period_start=start_date.isoformat(),
        period_end=end_date.isoformat(),
    )

    try:
        with httpx.Client(timeout=15.0, auth=(account_sid, auth_token)) as client:
            # Usage records
            resp = client.get(
                f"{base}/Usage/Records.json",
                params={
                    "StartDate": start_date.isoformat(),
                    "EndDate": end_date.isoformat(),
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                for rec in data.get("usage_records", []):
                    cat = rec.get("category", "")
                    price_str = rec.get("price", "0")
                    try:
                        price = abs(float(price_str))
                    except (TypeError, ValueError):
                        price = 0.0
                    if cat.startswith("calls"):
                        summary.calls_usd += price
                    elif cat.startswith("sms"):
                        summary.sms_usd += price
                    elif cat.startswith("phonenumbers"):
                        summary.phone_numbers_usd += price
                summary.calls_usd = round(summary.calls_usd, 4)
                summary.sms_usd = round(summary.sms_usd, 4)
                summary.phone_numbers_usd = round(summary.phone_numbers_usd, 4)
                summary.total_usd = round(
                    summary.calls_usd + summary.sms_usd + summary.phone_numbers_usd,
                    4,
                )
                summary.reachable = True
            else:
                summary.error = f"twilio_http_{resp.status_code}"
                _LOG.warning("twilio usage fetch returned %d", resp.status_code)

            # Account balance
            bal_resp = client.get(f"{base}.json")
            if bal_resp.status_code == 200:
                acct = bal_resp.json()
                bal_str = acct.get("balance")
                if bal_str is not None:
                    try:
                        summary.account_balance_usd = round(float(bal_str), 2)
                    except (TypeError, ValueError):
                        pass
    except httpx.HTTPError as exc:
        summary.error = f"twilio_transport_error: {exc}"
        _LOG.warning("twilio API error: %s", exc)

    return summary


# ---------------------------------------------------------------------------
# Composite snapshot
# ---------------------------------------------------------------------------

def voice_codb_snapshot(*, force: bool = False) -> VoiceCostSnapshot:
    """Build a combined Vapi + Twilio cost snapshot.

    Cached for ``SAMUS_VOICE_COST_CACHE_TTL`` seconds (default 1 hour).
    Pass ``force=True`` to bypass the cache.
    """
    global _cached_snapshot, _cached_at
    now = time.time()
    with _lock:
        if not force and _cached_snapshot is not None and (now - _cached_at) < _CACHE_TTL_SEC:
            return _cached_snapshot

    try:
        from backend.common.config import get_settings
        settings = get_settings()
    except Exception:
        settings = None

    vapi_ledger = aggregate_vapi_costs(days=30)

    twilio = TwilioCostSummary(error="twilio_credentials_not_configured")
    if settings:
        sid = getattr(settings, "twilio_account_sid", "") or ""
        tok = getattr(settings, "twilio_auth_token", "") or ""
        if sid and tok:
            twilio = fetch_twilio_costs(sid, tok, days=30)

    days_in_period = 30
    est_monthly_vapi = round(
        (vapi_ledger.total_usd / days_in_period * 30) if vapi_ledger.total_usd else 0.0,
        2,
    )
    est_monthly_twilio = round(
        (twilio.total_usd / days_in_period * 30) if twilio.total_usd else 0.0,
        2,
    )

    snap = VoiceCostSnapshot(
        ts=datetime.now(timezone.utc).isoformat(),
        vapi=vapi_ledger,
        twilio=twilio,
        estimated_monthly_vapi_usd=est_monthly_vapi,
        estimated_monthly_twilio_usd=est_monthly_twilio,
        estimated_monthly_voice_total_usd=round(est_monthly_vapi + est_monthly_twilio, 2),
    )

    with _lock:
        _cached_snapshot = snap
        _cached_at = now

    return snap


def reset_cache() -> None:
    """Drop cached snapshot (tests + manual refresh)."""
    global _cached_snapshot, _cached_at
    with _lock:
        _cached_snapshot = None
        _cached_at = 0.0
