"""Debts registry loader + portfolio summary math.

Pure functions. ``debts.yaml`` is operator-curated PII (gitignored); when
absent the loader returns an empty registry with ``registry_loaded=False``
so the workcell still serves /snapshot without raising.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from pathlib import Path

import yaml

from .models import (
    Debt,
    DebtPortfolioSummary,
    DebtsRegistry,
    DebtTierTotal,
    ResolutionPath,
)


_LOG = logging.getLogger("samus.finance.debts")
_DEFAULT_DEBTS_PATH = Path(__file__).resolve().parent / "debts.yaml"


def debts_path() -> Path:
    """Resolve registry path: ``SAMUS_DEBTS_PATH`` env var or default."""
    override = os.getenv("SAMUS_DEBTS_PATH")
    return Path(override) if override else _DEFAULT_DEBTS_PATH


def load_registry() -> tuple[DebtsRegistry, bool]:
    """Read + validate debts.yaml. Returns (registry, registry_loaded).

    Missing file is a normal Phase 3 state (operator hasn't populated yet) —
    returns an empty registry with registry_loaded=False, no exception raised.
    """
    path = debts_path()
    if not path.exists():
        _LOG.info("debts.yaml not present at %s -- returning empty registry", path)
        return DebtsRegistry(), False
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return DebtsRegistry.model_validate(raw), True


def recommended_path(debt: Debt) -> ResolutionPath | None:
    """Return the first resolution path marked recommended, else None."""
    for path in debt.resolution_paths:
        if path.recommended:
            return path
    return None


def _confirmed_total(debts: list[Debt]) -> float:
    return round(sum(d.balance_usd or 0.0 for d in debts if not d.balance_unknown), 2)


def by_tier(debts: list[Debt]) -> list[DebtTierTotal]:
    """Group debts by tier with per-tier totals + counts."""
    buckets: dict[int, list[Debt]] = defaultdict(list)
    for d in debts:
        buckets[int(d.tier)].append(d)
    out: list[DebtTierTotal] = []
    for tier in sorted(buckets):
        bucket = buckets[tier]
        out.append(
            DebtTierTotal(
                tier=tier,
                debt_count=len(bucket),
                confirmed_total_usd=_confirmed_total(bucket),
                unknown_balance_count=sum(1 for d in bucket if d.balance_unknown),
            )
        )
    return out


def summarize(registry: DebtsRegistry, registry_loaded: bool, ts: str) -> DebtPortfolioSummary:
    """Build the aggregate portfolio view."""
    debts = registry.debts
    recommendations: dict[str, str] = {}
    for d in debts:
        rec = recommended_path(d)
        if rec is not None:
            recommendations[d.id] = rec.label
    return DebtPortfolioSummary(
        debt_count=len(debts),
        confirmed_total_usd=_confirmed_total(debts),
        unknown_balance_count=sum(1 for d in debts if d.balance_unknown),
        by_tier=by_tier(debts),
        recommended_path_per_debt=recommendations,
        ts=ts,
        registry_loaded=registry_loaded,
    )
