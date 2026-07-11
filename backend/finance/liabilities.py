"""Liabilities registry loader + per-lender balance math.

Pure functions — no Stripe, no settings. ``liabilities.yaml`` is re-read on
every call so operator edits land without a service restart.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from pathlib import Path

import yaml

from .models import (
    Lender,
    LenderBalance,
    LiabilitiesRegistry,
    LiabilitiesSummary,
)


_LOG = logging.getLogger("samus.finance.liabilities")
_DEFAULT_LIABILITIES_PATH = Path(__file__).resolve().parent / "liabilities.yaml"


def liabilities_path() -> Path:
    """Resolve registry path: ``SAMUS_LIABILITIES_PATH`` env var or default."""
    override = os.getenv("SAMUS_LIABILITIES_PATH")
    return Path(override) if override else _DEFAULT_LIABILITIES_PATH


def load_registry() -> LiabilitiesRegistry:
    """Read + validate the liabilities YAML.

    Missing file returns an empty registry (matches the debts/actions/info_gaps/
    hardship pattern) so fresh checkouts don't crash before the operator
    populates ``liabilities.yaml``.
    """
    path = liabilities_path()
    if not path.exists():
        _LOG.info("liabilities.yaml not present at %s -- returning empty registry", path)
        return LiabilitiesRegistry()
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return LiabilitiesRegistry.model_validate(raw)


def _lenders_by_id(lenders: list[Lender]) -> dict[str, Lender]:
    return {l.id: l for l in lenders}


def per_lender_balances(registry: LiabilitiesRegistry) -> list[LenderBalance]:
    """Compute outstanding balance per lender (loans - repayments).

    Lenders with zero loans AND zero repayments are excluded from the result.
    Sort: largest outstanding first.
    """
    loans_by: dict[str, list[float]] = defaultdict(list)
    repays_by: dict[str, list[float]] = defaultdict(list)
    for loan in registry.loans:
        loans_by[loan.lender_id].append(loan.amount_usd)
    for rep in registry.repayments:
        repays_by[rep.lender_id].append(rep.amount_usd)

    lookup = _lenders_by_id(registry.lenders)
    out: list[LenderBalance] = []
    # Iterate the union of touched lender ids so we include lenders who
    # appear only in repayments (overpayment scenario flagged via negative
    # outstanding) and skip lenders who were registered but never used.
    touched = sorted(set(loans_by) | set(repays_by))
    for lid in touched:
        lender = lookup.get(lid)
        if lender is None:
            # Unknown lender_id referenced — surface as best-effort row so
            # operator notices the typo in their yaml.
            name = f"unknown:{lid}"
            relationship = "other"
        else:
            name = lender.name
            relationship = lender.relationship
        loans_sum = round(sum(loans_by.get(lid, [])), 2)
        repays_sum = round(sum(repays_by.get(lid, [])), 2)
        out.append(
            LenderBalance(
                lender_id=lid,
                lender_name=name,
                relationship=relationship,
                loans_total_usd=loans_sum,
                repayments_total_usd=repays_sum,
                outstanding_usd=round(loans_sum - repays_sum, 2),
                loan_count=len(loans_by.get(lid, [])),
                repayment_count=len(repays_by.get(lid, [])),
            )
        )

    out.sort(key=lambda b: b.outstanding_usd, reverse=True)
    return out


def summarize(registry: LiabilitiesRegistry, ts: str) -> LiabilitiesSummary:
    """Build the aggregate liabilities view."""
    balances = per_lender_balances(registry)
    total_loans = round(sum(b.loans_total_usd for b in balances), 2)
    total_repays = round(sum(b.repayments_total_usd for b in balances), 2)
    total_outstanding = round(total_loans - total_repays, 2)
    return LiabilitiesSummary(
        total_outstanding_usd=total_outstanding,
        total_loans_received_usd=total_loans,
        total_repayments_made_usd=total_repays,
        by_lender=balances,
        ts=ts,
    )
