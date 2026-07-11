"""Declines registry loader + cash-distress heuristic.

Pure functions. ``declines.yaml`` is re-read on every call. The cash-distress
flag is computed against a rolling window (default 30 days):

  - any severity=critical in window  ->  status=critical (service lost)
  - severity=high count >= 3 in window -> status=degraded
  - total declines >= 6 in window     ->  status=degraded
  - high count >= 5 OR total >= 10    ->  status=critical
  - otherwise                          ->  status=ok

Date math is whole-day relative to ``today`` so test fixtures can pin the
clock by injecting their own date.
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from datetime import date as _date
from datetime import timedelta
from pathlib import Path

import yaml

from .models import (
    CashDistressStatus,
    DeclineEvent,
    DeclinesRegistry,
    DeclinesSummary,
)


_LOG = logging.getLogger("samus.finance.declines")
_DEFAULT_DECLINES_PATH = Path(__file__).resolve().parent / "declines.yaml"


def declines_path() -> Path:
    """Resolve registry path: ``SAMUS_DECLINES_PATH`` env var or default."""
    override = os.getenv("SAMUS_DECLINES_PATH")
    return Path(override) if override else _DEFAULT_DECLINES_PATH


def load_registry() -> DeclinesRegistry:
    """Read + validate the declines YAML.

    Missing file returns an empty registry (matches the debts/actions/info_gaps/
    hardship pattern) so fresh checkouts + Phase-3-unpopulated hosts don't crash.
    """
    path = declines_path()
    if not path.exists():
        _LOG.info("declines.yaml not present at %s -- returning empty registry", path)
        return DeclinesRegistry()
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return DeclinesRegistry.model_validate(raw)


def filter_recent(events: list[DeclineEvent], *, window_days: int,
                  today: _date | None = None) -> list[DeclineEvent]:
    """Return events whose date >= today - window_days."""
    anchor = today or _date.today()
    cutoff = anchor - timedelta(days=window_days)
    return [e for e in events if e.date >= cutoff]


def evaluate_distress(events: list[DeclineEvent]) -> tuple[CashDistressStatus, list[str]]:
    """Apply the distress heuristic. Returns (status, list of reason strings)."""
    by_sev: Counter = Counter(e.severity for e in events)
    crit = by_sev.get("critical", 0)
    high = by_sev.get("high", 0)
    total = len(events)

    reasons: list[str] = []
    if crit:
        reasons.append(f"{crit} critical decline(s) in window -- service lost")
    if high >= 3:
        reasons.append(f"{high} high-severity decline(s) in window")
    if total >= 6:
        reasons.append(f"{total} total decline events in window")

    if not reasons:
        return "ok", []
    if crit or high >= 5 or total >= 10:
        return "critical", reasons
    return "degraded", reasons


def summarize(registry: DeclinesRegistry, *, window_days: int, ts: str,
              today: _date | None = None) -> DeclinesSummary:
    """Build the declines view over a ``window_days`` rolling window."""
    recent = filter_recent(registry.events, window_days=window_days, today=today)
    status, reasons = evaluate_distress(recent)
    by_sev: Counter = Counter(e.severity for e in recent)
    # Sort recent events by date descending so most-recent-first.
    recent_sorted = sorted(recent, key=lambda e: e.date, reverse=True)
    return DeclinesSummary(
        window_days=window_days,
        recent_total_count=len(recent),
        recent_by_severity=dict(by_sev),
        cash_distress=status,
        distress_reasons=reasons,
        recent_events=recent_sorted,
        ts=ts,
    )
