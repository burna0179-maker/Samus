"""CODB registry loader + runway math.

Pure functions — no httpx, no settings dependency. The registry YAML is
re-read on every call so an operator can edit ``codb_registry.yaml`` and
have changes reflected immediately without restarting the workcell.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from pathlib import Path

import yaml

from .models import (
    CodbCategoryBurn,
    CodbItem,
    CodbRegistry,
    CodbSummary,
    RunwayCalc,
)


_LOG = logging.getLogger("samus.finance.codb")
_DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parent / "codb_registry.yaml"

# Criticality ordering for sort + cut-first lists.
_CRITICALITY_RANK: dict[str, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}


def registry_path() -> Path:
    """Resolve registry path: ``SAMUS_CODB_REGISTRY_PATH`` env var or default."""
    override = os.getenv("SAMUS_CODB_REGISTRY_PATH")
    return Path(override) if override else _DEFAULT_REGISTRY_PATH


def load_registry() -> CodbRegistry:
    """Read + validate the CODB registry YAML.

    Missing file returns an empty registry (matches the debts/actions/info_gaps/
    hardship pattern) so fresh checkouts don't crash before the operator
    populates ``codb_registry.yaml``.
    """
    path = registry_path()
    if not path.exists():
        _LOG.info("codb_registry.yaml not present at %s -- returning empty registry", path)
        return CodbRegistry()
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return CodbRegistry.model_validate(raw)


def total_monthly_burn(items: list[CodbItem]) -> float:
    """Sum of estimated_monthly_usd across all items."""
    return round(sum(i.estimated_monthly_usd for i in items), 2)


def burn_by_criticality(items: list[CodbItem]) -> dict[str, float]:
    """Group spend by criticality: returns {'critical': X, 'high': Y, ...}."""
    buckets: dict[str, float] = defaultdict(float)
    for item in items:
        buckets[item.criticality] += item.estimated_monthly_usd
    return {k: round(v, 2) for k, v in buckets.items()}


def burn_by_category(items: list[CodbItem]) -> list[CodbCategoryBurn]:
    """Group spend by category, sorted desc by total."""
    by_cat: dict[str, list[CodbItem]] = defaultdict(list)
    for item in items:
        by_cat[item.category].append(item)
    out = [
        CodbCategoryBurn(
            category=cat,
            total_monthly_usd=round(sum(i.estimated_monthly_usd for i in xs), 2),
            item_count=len(xs),
        )
        for cat, xs in by_cat.items()
    ]
    out.sort(key=lambda b: b.total_monthly_usd, reverse=True)
    return out


def cuttable_low_first(items: list[CodbItem]) -> list[CodbItem]:
    """Items ordered cut-first: lowest criticality first, then largest $ first.

    The intent: when runway is short, this is the eat-order. Cut a $30 medium
    before a $5 critical; cut a $30 medium before a $10 medium.
    """
    return sorted(
        items,
        key=lambda i: (-_CRITICALITY_RANK.get(i.criticality, 99), -i.estimated_monthly_usd),
    )


def summarize(registry: CodbRegistry, ts: str) -> CodbSummary:
    """Build the aggregate CODB view from the validated registry."""
    items = registry.costs
    return CodbSummary(
        total_monthly_burn_usd=total_monthly_burn(items),
        by_criticality=burn_by_criticality(items),
        by_category=burn_by_category(items),
        cuttable_low_first=cuttable_low_first(items),
        ts=ts,
    )


def compute_runway(
    available_balance_usd: float,
    total_monthly_burn_usd: float,
    runway_alert_days: int,
) -> RunwayCalc:
    """Days-of-runway = balance / (monthly_burn / 30).

    Returns ``days_of_runway=None`` when burn is zero (infinite runway).
    Alert is triggered when days < runway_alert_days (and runway is finite).
    """
    daily_burn = round(total_monthly_burn_usd / 30.0, 4) if total_monthly_burn_usd else 0.0
    if daily_burn <= 0:
        days = None
        alert = False
    else:
        days = round(available_balance_usd / daily_burn, 1)
        alert = days < runway_alert_days
    return RunwayCalc(
        available_balance_usd=round(available_balance_usd, 2),
        total_monthly_burn_usd=round(total_monthly_burn_usd, 2),
        daily_burn_usd=daily_burn,
        days_of_runway=days,
        runway_alert_days=runway_alert_days,
        alert_triggered=alert,
    )
