"""Dynamic LLM budget scaler — ties daily $ cap to Samus's revenue.

OpenAI API cost is real CODB. The daily LLM budget is not a fixed
constant — it scales with Samus's own financial progression. More
earned revenue → more inference capacity unlocked.

Formula::

    daily_cap = clamp(FLOOR, MRR × REINVEST_PCT / 30, CEILING)

Where:
  - MRR is the live monthly recurring revenue from Stripe subscriptions
  - REINVEST_PCT is the fraction of MRR allocated to LLM inference (default 5%)
  - FLOOR is the minimum daily budget regardless of revenue (default $0.50)
  - CEILING is the hard max to prevent runaway spend (default $25.00)

At different MRR levels (5% reinvestment):
  $0      → $0.50/day (floor)
  $500    → $0.83/day
  $1,000  → $1.67/day
  $5,000  → $8.33/day
  $15,000 → $25.00/day (ceiling)

The MRR value is cached for ``REFRESH_INTERVAL_SEC`` (default 3600s /
1 hour) to avoid hammering Stripe on every LLM call. On Stripe failure
the last known value is retained; on cold start with no Stripe, the
floor applies.
"""
from __future__ import annotations

import logging
import os
import threading
import time

_LOG = logging.getLogger("samus.common.llm_budget_scaler")

REINVEST_PCT = float(os.getenv("SAMUS_LLM_REINVEST_PCT", "0.05"))
FLOOR_USD = float(os.getenv("SAMUS_LLM_DAILY_FLOOR_USD", "0.50"))
CEILING_USD = float(os.getenv("SAMUS_LLM_DAILY_CEILING_USD", "25.00"))
REFRESH_INTERVAL_SEC = float(os.getenv("SAMUS_LLM_MRR_REFRESH_SEC", "3600"))

_lock = threading.Lock()
_cached_mrr: float | None = None
_cached_at: float = 0.0


def _fetch_mrr() -> float | None:
    """Best-effort: query current MRR from Stripe via the finance module.

    Returns None on any failure (Stripe unreachable, no key configured,
    import error). The caller retains the last known value.
    """
    try:
        from backend.finance.stripe_client import (
            StripeClient,
            monthly_recurring_revenue_usd,
        )
        from backend.common.config import get_settings

        settings = get_settings()
        key = settings.stripe_api_key
        if not key:
            _LOG.debug("llm_budget_scaler: no stripe_api_key; cannot fetch MRR")
            return None

        client = StripeClient(api_key=key)
        subs = client.fetch_subscriptions(status="active", limit=100)
        mrr = monthly_recurring_revenue_usd(subs)
        _LOG.info("llm_budget_scaler: live MRR=%.2f", mrr)
        return mrr
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("llm_budget_scaler: MRR fetch failed: %s", exc)
        return None


def _refresh_if_stale() -> float:
    """Return current MRR, refreshing from Stripe if cache is stale."""
    global _cached_mrr, _cached_at
    now = time.time()
    with _lock:
        if _cached_mrr is not None and (now - _cached_at) < REFRESH_INTERVAL_SEC:
            return _cached_mrr

    mrr = _fetch_mrr()
    with _lock:
        if mrr is not None:
            _cached_mrr = mrr
            _cached_at = now
        return _cached_mrr if _cached_mrr is not None else 0.0


def compute_daily_cap() -> float:
    """Resolve today's LLM daily $ cap from live MRR.

    Returns a value in [FLOOR_USD, CEILING_USD].
    """
    mrr = _refresh_if_stale()
    raw = mrr * REINVEST_PCT / 30.0
    cap = max(FLOOR_USD, min(raw, CEILING_USD))
    return round(cap, 4)


def reset_cache() -> None:
    """Drop cached MRR (tests + rotation tooling)."""
    global _cached_mrr, _cached_at
    with _lock:
        _cached_mrr = None
        _cached_at = 0.0


__all__ = [
    "CEILING_USD",
    "FLOOR_USD",
    "REINVEST_PCT",
    "compute_daily_cap",
    "reset_cache",
]
