"""Live service cost & balance monitor for CODB alerting.

Polls vendor billing APIs for real spend and remaining balance/credits.
Surfaces alerts when any service drops below configurable thresholds.

Supported vendors:

  - **OpenAI** — ``GET /v1/dashboard/billing/credit_grants`` for credits,
    ``GET /v1/organization/usage`` for period spend.
  - **Anthropic** — ``GET /v1/organizations/cost_report`` with Admin API key.
  - **Twilio** — ``GET /Accounts/{sid}/Balance.json`` for account balance,
    ``GET /Accounts/{sid}/Usage/Records.json`` for period spend.
  - **Vapi** — no billing API exists; per-call cost tracked via voice
    workcell's ``voice_cost_tracker`` (webhook-driven, not polled here).

All API calls are best-effort: a vendor being unreachable degrades the
snapshot (``reachable=False``, ``error`` populated) but never crashes the
service or blocks the finance workcell.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import httpx

_LOG = logging.getLogger("samus.finance.service_cost_monitor")

_CACHE_TTL_SEC = float(os.getenv("SAMUS_COST_MONITOR_CACHE_TTL", "3600"))
_HTTP_TIMEOUT = 15.0

# Alert thresholds (env-tunable)
_OPENAI_LOW_CREDITS_USD = float(os.getenv("SAMUS_OPENAI_LOW_CREDITS_USD", "5.00"))
_TWILIO_LOW_BALANCE_USD = float(os.getenv("SAMUS_TWILIO_LOW_BALANCE_USD", "10.00"))

_lock = threading.Lock()
_cached: ServiceCostSnapshot | None = None
_cached_at: float = 0.0


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class ServiceStatus:
    """Per-vendor cost/balance status."""
    vendor: str = ""
    reachable: bool = False
    balance_usd: float | None = None
    credits_remaining_usd: float | None = None
    period_spend_usd: float | None = None
    period_start: str = ""
    period_end: str = ""
    alert: bool = False
    alert_reason: str = ""
    error: str = ""


@dataclass
class ServiceCostSnapshot:
    """Composite snapshot of all monitored vendor balances."""
    ts: str = ""
    openai: ServiceStatus = field(default_factory=lambda: ServiceStatus(vendor="openai"))
    anthropic: ServiceStatus = field(default_factory=lambda: ServiceStatus(vendor="anthropic"))
    twilio: ServiceStatus = field(default_factory=lambda: ServiceStatus(vendor="twilio"))
    vapi: ServiceStatus = field(default_factory=lambda: ServiceStatus(vendor="vapi"))
    any_alert: bool = False
    alert_vendors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# OpenAI billing
# ---------------------------------------------------------------------------

def _check_openai(api_key: str) -> ServiceStatus:
    """Poll OpenAI for credit balance and recent usage."""
    st = ServiceStatus(vendor="openai")
    if not api_key:
        st.error = "openai_api_key_not_configured"
        return st

    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            # Credit grants / remaining balance
            resp = client.get(
                "https://api.openai.com/v1/dashboard/billing/credit_grants",
                headers=headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                total_granted = data.get("total_granted", 0)
                total_used = data.get("total_used", 0)
                st.credits_remaining_usd = round(total_granted - total_used, 2)
                st.reachable = True
            elif resp.status_code in (401, 403, 404):
                # Some OpenAI orgs don't have credit grants (post-pay).
                # Try the subscription endpoint instead.
                st.reachable = True
                st.credits_remaining_usd = None
            else:
                st.error = f"openai_credits_http_{resp.status_code}"

            # Usage for current billing period
            end = date.today()
            start = end - timedelta(days=30)
            usage_resp = client.get(
                "https://api.openai.com/v1/dashboard/billing/usage",
                headers=headers,
                params={
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                },
            )
            if usage_resp.status_code == 200:
                usage_data = usage_resp.json()
                # total_usage is in cents
                total_cents = usage_data.get("total_usage", 0)
                st.period_spend_usd = round(total_cents / 100.0, 2)
                st.period_start = start.isoformat()
                st.period_end = end.isoformat()
                st.reachable = True
            elif not st.error:
                st.error = f"openai_usage_http_{usage_resp.status_code}"

    except httpx.HTTPError as exc:
        st.error = f"openai_transport_error: {exc}"
        _LOG.warning("openai billing check failed: %s", exc)

    if st.credits_remaining_usd is not None and st.credits_remaining_usd < _OPENAI_LOW_CREDITS_USD:
        st.alert = True
        st.alert_reason = f"credits_low: ${st.credits_remaining_usd:.2f} remaining (threshold: ${_OPENAI_LOW_CREDITS_USD:.2f})"

    return st


# ---------------------------------------------------------------------------
# Anthropic billing (Admin API key required)
# ---------------------------------------------------------------------------

def _check_anthropic(admin_key: str) -> ServiceStatus:
    """Poll Anthropic for organization cost report."""
    st = ServiceStatus(vendor="anthropic")
    if not admin_key:
        st.error = "anthropic_admin_key_not_configured"
        return st

    headers = {
        "x-api-key": admin_key,
        "anthropic-version": "2023-06-01",
    }
    end = date.today()
    start = end - timedelta(days=30)
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.get(
                "https://api.anthropic.com/v1/organizations/cost_report",
                headers=headers,
                params={
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                # cost_report returns costs in cents
                total_cents = 0
                for item in data.get("data", []):
                    total_cents += item.get("cost_cents", 0)
                st.period_spend_usd = round(total_cents / 100.0, 2)
                st.period_start = start.isoformat()
                st.period_end = end.isoformat()
                st.reachable = True
            else:
                st.error = f"anthropic_http_{resp.status_code}"
                try:
                    err = resp.json()
                    st.error += f": {err.get('error', {}).get('message', resp.text[:200])}"
                except Exception:
                    pass
                _LOG.warning("anthropic cost report returned %d", resp.status_code)

    except httpx.HTTPError as exc:
        st.error = f"anthropic_transport_error: {exc}"
        _LOG.warning("anthropic billing check failed: %s", exc)

    return st


# ---------------------------------------------------------------------------
# Twilio balance
# ---------------------------------------------------------------------------

def _check_twilio(account_sid: str, auth_token: str) -> ServiceStatus:
    """Poll Twilio for account balance and recent usage."""
    st = ServiceStatus(vendor="twilio")
    if not account_sid or not auth_token:
        st.error = "twilio_credentials_not_configured"
        return st

    base = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}"
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT, auth=(account_sid, auth_token)) as client:
            # Account balance
            resp = client.get(f"{base}/Balance.json")
            if resp.status_code == 200:
                data = resp.json()
                bal_str = data.get("balance")
                if bal_str is not None:
                    st.balance_usd = round(float(bal_str), 2)
                st.reachable = True
            else:
                # Fallback: account info endpoint also has balance
                resp2 = client.get(f"{base}.json")
                if resp2.status_code == 200:
                    acct = resp2.json()
                    bal_str = acct.get("balance")
                    if bal_str is not None:
                        st.balance_usd = round(float(bal_str), 2)
                    st.reachable = True
                else:
                    st.error = f"twilio_http_{resp.status_code}"

            # Usage records for last 30 days
            end = date.today()
            start = end - timedelta(days=30)
            usage_resp = client.get(
                f"{base}/Usage/Records.json",
                params={
                    "StartDate": start.isoformat(),
                    "EndDate": end.isoformat(),
                },
            )
            if usage_resp.status_code == 200:
                total = 0.0
                for rec in usage_resp.json().get("usage_records", []):
                    try:
                        total += abs(float(rec.get("price", "0")))
                    except (TypeError, ValueError):
                        pass
                st.period_spend_usd = round(total, 2)
                st.period_start = start.isoformat()
                st.period_end = end.isoformat()

    except (httpx.HTTPError, ValueError) as exc:
        st.error = f"twilio_transport_error: {exc}"
        _LOG.warning("twilio billing check failed: %s", exc)

    if st.balance_usd is not None and st.balance_usd < _TWILIO_LOW_BALANCE_USD:
        st.alert = True
        st.alert_reason = f"balance_low: ${st.balance_usd:.2f} remaining (threshold: ${_TWILIO_LOW_BALANCE_USD:.2f})"

    return st


# ---------------------------------------------------------------------------
# Vapi (no billing API — stub with per-call cost note)
# ---------------------------------------------------------------------------

def _check_vapi() -> ServiceStatus:
    """Vapi has no billing API. Return a stub pointing to the voice tracker."""
    return ServiceStatus(
        vendor="vapi",
        reachable=False,
        error="no_billing_api — per-call cost tracked via GET /voice/cost_snapshot",
    )


# ---------------------------------------------------------------------------
# Composite snapshot
# ---------------------------------------------------------------------------

def service_cost_snapshot(*, force: bool = False) -> ServiceCostSnapshot:
    """Build a combined vendor balance/spend snapshot.

    Cached for ``SAMUS_COST_MONITOR_CACHE_TTL`` seconds (default 1 hour).
    """
    global _cached, _cached_at
    now = time.time()
    with _lock:
        if not force and _cached is not None and (now - _cached_at) < _CACHE_TTL_SEC:
            return _cached

    try:
        from backend.common.config import get_settings
        settings = get_settings()
    except Exception:
        settings = None

    openai_key = os.environ.get("OPENAI_API_KEY", "")
    anthropic_admin = getattr(settings, "anthropic_admin_api_key", "") if settings else ""
    twilio_sid = getattr(settings, "twilio_account_sid", "") if settings else ""
    twilio_tok = getattr(settings, "twilio_auth_token", "") if settings else ""

    openai = _check_openai(openai_key)
    anthropic = _check_anthropic(anthropic_admin)
    twilio = _check_twilio(twilio_sid, twilio_tok)
    vapi = _check_vapi()

    alerts = [s for s in (openai, anthropic, twilio, vapi) if s.alert]

    snap = ServiceCostSnapshot(
        ts=datetime.now(timezone.utc).isoformat(),
        openai=openai,
        anthropic=anthropic,
        twilio=twilio,
        vapi=vapi,
        any_alert=bool(alerts),
        alert_vendors=[s.vendor for s in alerts],
    )

    with _lock:
        _cached = snap
        _cached_at = now

    return snap


def reset_cache() -> None:
    """Drop cached snapshot (tests + manual refresh)."""
    global _cached, _cached_at
    with _lock:
        _cached = None
        _cached_at = 0.0
