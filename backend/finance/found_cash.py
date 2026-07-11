"""Found business-bank cash reconciliation — the REAL cash number for runway +
affordability, replacing Stripe's ``available_balance`` (which auto-sweeps to ~$0
because payouts land in the bank).

Found exports a full transaction activity CSV (Date, Description, Amount, …); the
current balance is the running sum of the Amount column from account opening. The
operator drops the latest export into the tax-docs dir (bind-mounted read-only
into the finance container as ``SAMUS_FOUND_ACTIVITY_DIR``); this reads the NEWEST
``*activity_report*.csv`` and totals it. Fail-soft: returns None on any problem so
the caller falls back to the Stripe figure.

Staleness matters: a real bank balance from a week-old export is still better than
a $0 Stripe artifact, but the caller should down-weight a stale reading — so the
export's file age is reported.
"""
from __future__ import annotations

import csv
import glob
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

_LOG = logging.getLogger("samus.finance.found_cash")

def _candidate_dirs() -> list[str]:
    """Where to look for the Found export, resolved at CALL time. Found cash is
    used ONLY when ``SAMUS_FOUND_ACTIVITY_DIR`` is explicitly set (the finance
    container sets it to the read-only bind mount). Unset => no Found reading =>
    the caller keeps the Stripe figure. This keeps the real host cash from leaking
    into unconfigured processes (e.g. tests, other services)."""
    env = os.environ.get("SAMUS_FOUND_ACTIVITY_DIR", "").strip()
    return [env] if env else []


@dataclass
class FoundCash:
    balance_usd: float
    txn_count: int
    latest_txn_date: str      # ISO date of the newest transaction in the export
    source_file: str
    export_age_days: float    # age of the CSV file (how stale the reading is)


def _parse_mmddyyyy(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime((s or "").strip(), "%m/%d/%Y")
    except Exception:  # noqa: BLE001
        return None


def read_found_cash(activity_dir: Optional[str] = None) -> Optional[FoundCash]:
    """Newest Found activity CSV -> current balance (running sum). None if none
    found or unreadable. Balance is the sum of ALL amounts (the account's running
    balance from opening), including transfers out to personal/primary — i.e. the
    literal cash left in the business account."""
    dirs = [activity_dir] if activity_dir else _candidate_dirs()
    path = ""
    for d in dirs:
        try:
            found = sorted(glob.glob(os.path.join(d, "*activity_report*.csv")),
                           key=os.path.getmtime)
        except Exception:  # noqa: BLE001
            continue
        if found:
            path = found[-1]
            break
    if not path:
        return None
    try:
        with open(path, encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        amounts = [float(r["Amount"]) for r in rows
                   if r.get("Amount") not in (None, "")]
        balance = round(sum(amounts), 2)
        dates = [d for d in (_parse_mmddyyyy(r.get("Date", "")) for r in rows) if d]
        latest = max(dates).date().isoformat() if dates else ""
        mtime = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
        age_days = round((datetime.now(timezone.utc) - mtime).total_seconds() / 86400.0, 1)
        return FoundCash(
            balance_usd=balance, txn_count=len(amounts), latest_txn_date=latest,
            source_file=os.path.basename(path), export_age_days=age_days,
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("found cash parse failed for %s: %s", path, exc)
        return None


def best_available_cash_usd(stripe_available_usd: float) -> tuple[float, str]:
    """The cash figure runway + affordability should use, in priority order:
      1. ``mercury`` — the LIVE Mercury available balance (API), once payouts land
         there and SAMUS_MERCURY_ENABLED is on;
      2. ``found_bank`` — the Found activity-CSV balance (transitional);
      3. ``stripe_available`` — Stripe's available balance (auto-swept ~$0).
    Returns ``(usd, source)``. Each tier fails soft to the next."""
    try:
        from .mercury_client import total_available_cash_usd
        m = total_available_cash_usd()
        if m is not None:
            return m, "mercury"
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("mercury cash tier failed, falling back: %s", exc)
    fc = read_found_cash()
    if fc is not None:
        return fc.balance_usd, "found_bank"
    return float(stripe_available_usd or 0.0), "stripe_available"
