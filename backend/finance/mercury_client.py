"""Mercury business-bank READ-ONLY client — the live real-cash source.

Once Stripe payouts land in Mercury, this replaces the manual Found CSV export:
``total_available_cash_usd()`` sums the available balance across Mercury accounts
via the first-party API, so runway + affordability read real cash in real time.

STRICTLY READ-ONLY. This module only ever GETs balances/transactions — it never
touches Mercury's payment endpoints. Use a Mercury READ-ONLY token
(mercury.com/settings/tokens), which structurally cannot move money. Seed it into
svc_Samus DPAPI (``_oneshot_seed_agent_secret.ps1 -Agent Samus -Name
MercuryApiToken``); the compose passes it to the finance container as
``MERCURY_API_TOKEN``.

Wire-not-arm: gated by ``SAMUS_MERCURY_ENABLED`` (default OFF). Off, or missing
token, or any API error => the caller falls back (Found CSV → Stripe). The
balance is cached (~15 min) so the 30-min tick never hammers the API.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

_LOG = logging.getLogger("samus.finance.mercury")

_BASE_URL = os.environ.get("SAMUS_MERCURY_BASE_URL", "https://api.mercury.com/api/v1")
_CACHE_TTL_SEC = 900.0
# Never use 0.0 as "never fetched" (monotonic-time house rule): start a TTL in the past.
_cache: dict[str, Any] = {"cash": None, "at": time.monotonic() - _CACHE_TTL_SEC}


def _enabled() -> bool:
    return os.environ.get("SAMUS_MERCURY_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _token() -> str:
    return os.environ.get("MERCURY_API_TOKEN", "").strip()


def mercury_available() -> bool:
    """True only when armed AND a token is present. Read-only, so this never
    implies any write capability."""
    return _enabled() and bool(_token())


def _get(path: str) -> Optional[dict[str, Any]]:
    import httpx

    token = _token()
    if not token:
        return None
    resp = httpx.get(
        f"{_BASE_URL}{path}",
        headers={"Authorization": f"Bearer {token}", "accept": "application/json"},
        timeout=20.0,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_accounts() -> list[dict[str, Any]]:
    """GET /accounts — each has availableBalance + currentBalance (USD dollars),
    id, accountNumber, status. [] on any problem."""
    try:
        data = _get("/accounts") or {}
        return list(data.get("accounts") or data.get("data") or [])
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("mercury fetch_accounts failed: %s", exc)
        return []


def fetch_transactions(account_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    """GET /account/{id}/transactions — deposits/withdrawals (for CODB
    reconciliation, later). [] on any problem. Read-only."""
    try:
        data = _get(f"/account/{account_id}/transactions?limit={int(limit)}") or {}
        return list(data.get("transactions") or data.get("data") or [])
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("mercury fetch_transactions failed: %s", exc)
        return []


def total_available_cash_usd(*, _now: Optional[float] = None) -> Optional[float]:
    """Sum of availableBalance across all Mercury accounts = the live real cash.
    None when disarmed, tokenless, or unreachable (caller then falls back).
    Cached for ``_CACHE_TTL_SEC``."""
    if not mercury_available():
        return None
    now = _now if _now is not None else time.monotonic()
    if (now - _cache["at"]) < _CACHE_TTL_SEC and _cache["cash"] is not None:
        return _cache["cash"]
    accounts = fetch_accounts()
    if not accounts:
        return None  # don't cache a failure as $0
    try:
        total = round(sum(float(a.get("availableBalance") or 0.0) for a in accounts), 2)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("mercury balance sum failed: %s", exc)
        return None
    _cache["cash"], _cache["at"] = total, now
    return total
