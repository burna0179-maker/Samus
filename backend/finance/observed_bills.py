"""Observed bills — bridge classified Gmail bill signals into CODB reasoning.

The intake pipeline (backend/intake/gmail_poller.py + email_classifier.py)
classifies every inbound email and, when a bill is detected, records the
vendor + amount + signal_kind into the inbound_email ledger. This module
is the read-side: it aggregates those observed bill signals per registry_id
over a window, computes variance vs. the declarative codb_registry.yaml
estimates, and exposes the result for consumption by:

  - :mod:`backend.cognitive.intelligence_cycle` (pre-shift briefing,
    end-of-day review) — so Samus's reasoning sees ACTUAL vendor charges,
    not just human-authored estimates.
  - :mod:`backend.cognitive.codb_reasoner` (investment recommender) —
    so a runway/headroom decision reflects the true burn.

Read-only. Never writes to codb_registry.yaml; a large observed-vs-estimated
variance surfaces as a signal for operator + LLM reasoning, not an auto-edit.

Fail-safe: a missing ledger, unreadable rows, or a corrupt entry degrades
to an empty / partial result — never raises. This module runs inside the
gateway's pre-shift briefing path, which must not crash on a bad row.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_LOG = logging.getLogger("samus.finance.observed_bills")

# The intake ledger the poller writes classified rows into. Env override
# matches the existing SAMUS_GMAIL_INBOX_LEDGER_PATH pattern the poller
# uses, so an operator can point both at the same path in one place.
_LEDGER_PATH_DEFAULT = "/opt/samus/data/intake/inbound_email.jsonl"

# Window over which "observed monthly" is extrapolated. 30 days is the
# natural billing cycle; 90 days smooths noise from vendors that bill
# quarterly. Kept conservative — the reasoning downstream should treat
# observed-vs-estimated variance as a signal to investigate, not a
# hair-trigger for autonomous action.
DEFAULT_WINDOW_DAYS = 30

# When a vendor has an observed amount but no registry match, we still
# want it visible for reasoning — it's a "vendor we're paying that we
# haven't budgeted for", which is exactly the kind of thing a business
# operating system should surface.
_UNMATCHED_VENDOR = "unmatched_new_vendor"


def _ledger_path() -> Path:
    p = os.getenv("SAMUS_GMAIL_INBOX_LEDGER_PATH") or _LEDGER_PATH_DEFAULT
    return Path(p)


@dataclass
class VendorObservation:
    """Observed bill signals for one vendor over the window."""

    registry_id: str
    vendor_domain: str  # from ledger row (may be "")
    signal_count: int  # bill entries in the window
    total_observed_usd: float  # sum of all observed amounts
    latest_amount_usd: float  # most-recent non-zero amount
    payment_declined_count: int  # payment_declined signal_kind
    receipt_count: int  # receipt signal_kind
    invoice_count: int  # invoice signal_kind
    registry_estimate_usd: float | None  # from codb_registry.yaml, if matched
    variance_usd: float | None  # observed_monthly - estimate

    def to_dict(self) -> dict[str, Any]:
        return {
            "registry_id": self.registry_id,
            "vendor_domain": self.vendor_domain,
            "signal_count": self.signal_count,
            "total_observed_usd": round(self.total_observed_usd, 2),
            "latest_amount_usd": round(self.latest_amount_usd, 2),
            "payment_declined_count": self.payment_declined_count,
            "receipt_count": self.receipt_count,
            "invoice_count": self.invoice_count,
            "registry_estimate_usd": (
                None if self.registry_estimate_usd is None else round(self.registry_estimate_usd, 2)
            ),
            "variance_usd": (None if self.variance_usd is None else round(self.variance_usd, 2)),
        }


@dataclass
class ObservedBillsSummary:
    """Roll-up of observed bill signals with cross-reference to CODB registry."""

    window_days: int
    signals_scanned: int
    total_observed_usd: float
    total_registry_estimate_usd: float
    total_variance_usd: float
    payment_declined_active: int  # count of vendors with recent decline
    unmatched_vendors_count: int  # observed but not in registry
    vendors: list[VendorObservation] = field(default_factory=list)
    # Bank activity — populated when bank_activity ledger has rows in window.
    bank_revenue_usd: float = 0.0  # sum of category=revenue in window
    bank_transfer_usd: float = 0.0  # owner draws / tax withholding
    bank_personal_usd: float = 0.0  # personal / passthrough
    bank_txn_count: int = 0  # total bank transactions in window
    bank_net_usd: float = 0.0  # revenue + transfers + spend
    # Founder cash-health — signals from the personal Cash App account.
    # Not CODB, but a material input to reasoning about urgency + risk
    # tolerance (heavy borrow = shorter operator runway).
    founder_borrow_usd: float = 0.0
    founder_deposits_usd: float = 0.0
    founder_cash_card_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_days": self.window_days,
            "signals_scanned": self.signals_scanned,
            "total_observed_usd": round(self.total_observed_usd, 2),
            "total_registry_estimate_usd": round(self.total_registry_estimate_usd, 2),
            "total_variance_usd": round(self.total_variance_usd, 2),
            "payment_declined_active": self.payment_declined_active,
            "unmatched_vendors_count": self.unmatched_vendors_count,
            "vendors": [v.to_dict() for v in self.vendors],
            "bank_revenue_usd": round(self.bank_revenue_usd, 2),
            "bank_transfer_usd": round(self.bank_transfer_usd, 2),
            "bank_personal_usd": round(self.bank_personal_usd, 2),
            "bank_txn_count": self.bank_txn_count,
            "bank_net_usd": round(self.bank_net_usd, 2),
            "founder_borrow_usd": round(self.founder_borrow_usd, 2),
            "founder_deposits_usd": round(self.founder_deposits_usd, 2),
            "founder_cash_card_usd": round(self.founder_cash_card_usd, 2),
        }


def _iter_ledger_rows(path: Path, since: datetime) -> list[dict[str, Any]]:
    """Read ledger rows with ts >= since. Never raises."""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_raw = rec.get("ts", "")
                try:
                    row_ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    if row_ts < since:
                        continue
                except (ValueError, AttributeError):
                    # Missing/invalid ts — keep row (defensive; older entries
                    # without normalized timestamps still count).
                    pass
                rows.append(rec)
    except OSError as exc:
        _LOG.warning("observed_bills: ledger read failed: %s", exc)
    return rows


def _load_registry_estimates() -> dict[str, float]:
    """Load {registry_id: estimated_monthly_usd} from the CODB registry.

    Best-effort — a missing/corrupt registry yields {} so callers still get
    variance=None instead of a crash.
    """
    try:
        from backend.finance.codb import load_registry

        reg = load_registry()
        estimates: dict[str, float] = {}
        for item in reg.costs:
            estimates[item.id] = float(item.estimated_monthly_usd or 0.0)
        return estimates
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("observed_bills: registry load failed: %s", exc)
        return {}


def _classify_ledger_row(row: dict[str, Any]) -> tuple[str, str, float | None, str]:
    """Classify a historical ledger row from its minimal fields.

    Historical entries (from the 50-day cred-gap replay) were written before
    the classifier was in the poller, so they lack the category/vendor/amount
    fields. We can still classify them by running the same vendor-matching
    heuristics against ``subject_head`` + ``from_addr_tail`` — the same
    signals the live classifier uses for the bill category.

    Returns (category, vendor_registry_id, amount_hint, signal_kind).
    """
    from_addr_tail = row.get("from_addr_tail", "") or ""
    subject = row.get("subject_head", "") or ""
    # from_addr_tail is stored as the last 12 chars of the sender; a bare
    # domain works for vendor matching (match_vendor tolerates it).
    from_addr = from_addr_tail if "@" in from_addr_tail else f"x@{from_addr_tail}"

    try:
        from backend.finance.gmail_bill_scan import (
            classify_signal_kind,
            extract_amount,
            match_vendor,
            _pick_registry_id,
        )

        vendor_domain, candidates = match_vendor(from_addr, subject)
        if not vendor_domain and not any(
            kw in subject.lower()
            for kw in (
                "invoice",
                "receipt",
                "payment",
                "billing",
                "declined",
                "charge",
                "renewal",
                "subscription",
                "statement",
            )
        ):
            return "", "", None, ""
        amount = extract_amount(subject)  # only subject available here
        kind = classify_signal_kind(subject, "")
        vendor = _pick_registry_id(vendor_domain, candidates, subject, amount) if candidates else ""
        return "bill", vendor, amount, kind
    except Exception:  # noqa: BLE001
        return "", "", None, ""


def summarize_observed_bills(
    window_days: int = DEFAULT_WINDOW_DAYS,
    *,
    ledger_path: Path | None = None,
    now: datetime | None = None,
) -> ObservedBillsSummary:
    """Aggregate observed bill signals per registry_id over the window.

    Reads the intake ledger (:func:`_ledger_path`), filters to entries with
    ``category == "bill"``, groups by ``vendor`` (matched registry_id from
    the classifier), and computes per-vendor total + latest + signal kind
    counts. Cross-references against the CODB registry's estimated_monthly
    for variance.

    ``window_days`` is the observation horizon. Amounts are NOT annualized
    or extrapolated — the totals are the raw sum of observed bill amounts
    in the window. Callers that want a "monthly-equivalent" divide by
    (window_days / 30).
    """
    path = ledger_path or _ledger_path()
    _now = now or datetime.now(timezone.utc)
    since = _now - timedelta(days=window_days)
    rows = _iter_ledger_rows(path, since=since)

    # Merge in bank-activity rows — Cash App CSV / (future) Mercury API.
    # Bank rows are AUTHORITATIVE: they represent actual charges that hit
    # the account, vs. Gmail rows which represent what the vendor CLAIMED
    # to bill. Where the same vendor appears in both streams within the
    # window, the bank amount supersedes the gmail estimate for variance
    # math (implemented via prefer_bank_amount below).
    bank_revenue = 0.0
    bank_transfer = 0.0
    bank_personal = 0.0
    bank_txn_count = 0
    # Founder cash-health signals — the personal Cash App shows Alex's
    # borrow history + P2P activity + everyday spend. Not CODB, but a
    # material input to reasoning: heavy personal borrow = higher urgency
    # to convert prospects, lower tolerance for speculative spend.
    founder_borrow_usd = 0.0
    founder_deposits_usd = 0.0
    founder_cash_card_usd = 0.0
    try:
        from backend.finance.bank_activity import load_transactions

        bank_txns = load_transactions(since=since)
        bank_txn_count = len(bank_txns)
        for txn in bank_txns:
            # Personal-CSV rows carry Transaction Type in bank_category —
            # bucket them into founder-cash-health signals before falling
            # through to the CODB-vs-Gmail merge below.
            if txn.source == "cash_app_personal_csv":
                bcat = (txn.bank_category or "").lower()
                if "borrow" in bcat:
                    founder_borrow_usd += txn.amount_usd
                elif "deposit" in bcat or "add cash" in bcat:
                    founder_deposits_usd += txn.amount_usd
                elif "cash card" in bcat and txn.category == "personal":
                    founder_cash_card_usd += txn.amount_usd

            if txn.category == "revenue":
                bank_revenue += txn.amount_usd
                continue
            if txn.category == "transfer":
                bank_transfer += txn.amount_usd
                continue
            if txn.category == "personal":
                bank_personal += txn.amount_usd
                continue
            if txn.category == "business_transfer":
                # LLC funding / distributions between the operator and the
                # LLC bank account. Track as transfer for cash-flow math;
                # not a CODB-side vendor cost.
                bank_transfer += txn.amount_usd
                continue
            if txn.category != "bill":
                continue
            rows.append(
                {
                    "ts": txn.ts,
                    "category": "bill",
                    "vendor": txn.vendor_registry_id or "",
                    "amount_usd": abs(txn.amount_usd),  # bank stores as negative
                    "bill_signal_kind": txn.bill_signal_kind,
                    "from_addr_tail": txn.raw_description[:12],
                    "subject_head": txn.raw_description,
                    "_source": "bank_activity",  # marker for dedup / preference
                }
            )
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("observed_bills: bank_activity load failed: %s", exc)

    # Aggregate per vendor
    per_vendor: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "count": 0,
            "total": 0.0,
            "latest": 0.0,
            "domain": "",
            "declined": 0,
            "receipt": 0,
            "invoice": 0,
            "bank_total": 0.0,
            "bank_count": 0,
        }
    )
    signals_scanned = 0

    for row in rows:
        # Category may be missing on historical entries written before the
        # classifier was wired into the poller. Fall back to a lightweight
        # on-the-fly re-classification from the minimal fields the ledger
        # captured (from_addr_tail + subject_head) so the 760 replayed
        # historical entries still contribute to observed bills.
        category = row.get("category")
        vendor = row.get("vendor")
        amt = row.get("amount_usd")
        signal_kind = row.get("bill_signal_kind", "")
        if not category:
            try:
                cat, vend, amount_hint, sk = _classify_ledger_row(row)
                category = cat
                vendor = vendor or vend
                if amt is None and amount_hint is not None:
                    amt = amount_hint
                signal_kind = signal_kind or sk
            except Exception:  # noqa: BLE001
                pass
        if category != "bill":
            continue
        signals_scanned += 1
        vendor = vendor or _UNMATCHED_VENDOR
        try:
            amt_f = float(amt) if amt is not None else 0.0
        except (TypeError, ValueError):
            amt_f = 0.0

        v = per_vendor[vendor]
        v["count"] += 1
        v["total"] += amt_f
        # Track bank-side aggregation separately so we can prefer it in the
        # observed total below when both sources have data for the same vendor.
        if row.get("_source") == "bank_activity":
            v["bank_total"] += amt_f
            v["bank_count"] += 1
        if amt_f > 0:
            v["latest"] = amt_f  # rows are chronological in the ledger
        # Domain from from_addr_tail (best-effort)
        tail = row.get("from_addr_tail", "")
        if "@" in tail and not v["domain"]:
            v["domain"] = tail.rsplit("@", 1)[-1]

        # Signal kind from the classification embedded in the ledger row.
        # The classifier stashes bill_signal_kind in the ledger via
        # the poller's ledger_row extension, when present.
        kind = row.get("bill_signal_kind", "")
        if kind == "payment_declined":
            v["declined"] += 1
        elif kind == "receipt":
            v["receipt"] += 1
        elif kind == "invoice":
            v["invoice"] += 1

    estimates = _load_registry_estimates()

    # Monthly-equivalent factor: total_observed / (window_days / 30).
    # Applied only for variance vs. estimated_monthly.
    month_factor = 30.0 / max(1.0, float(window_days))

    vendors: list[VendorObservation] = []
    total_observed = 0.0
    total_estimate = 0.0
    declined_active = 0

    for registry_id, agg in per_vendor.items():
        est = estimates.get(registry_id) if registry_id != _UNMATCHED_VENDOR else None
        # Prefer bank-observed total when available — the account is
        # authoritative; Gmail may double-count a "receipt for X" plus
        # "you spent X at vendor" for the same underlying charge.
        preferred_total = agg["bank_total"] if agg["bank_count"] > 0 else agg["total"]
        observed_monthly_equiv = preferred_total * month_factor
        variance = round(observed_monthly_equiv - est, 2) if est is not None else None
        vendors.append(
            VendorObservation(
                registry_id=registry_id,
                vendor_domain=agg["domain"],
                signal_count=agg["count"],
                total_observed_usd=preferred_total,
                latest_amount_usd=agg["latest"],
                payment_declined_count=agg["declined"],
                receipt_count=agg["receipt"],
                invoice_count=agg["invoice"],
                registry_estimate_usd=est,
                variance_usd=variance,
            )
        )
        total_observed += preferred_total
        if est is not None:
            total_estimate += est
        if agg["declined"] > 0:
            declined_active += 1

    # Sort by observed spend desc so the largest actual costs land first.
    vendors.sort(key=lambda v: v.total_observed_usd, reverse=True)

    unmatched = sum(1 for v in vendors if v.registry_id == _UNMATCHED_VENDOR)
    total_observed_monthly = round(total_observed * month_factor, 2)
    total_variance = round(total_observed_monthly - total_estimate, 2)

    # Net cash flow: revenue arrives positive; spend (bill total) is a cost
    # so subtract; transfers/personal are cash movement, positive or negative
    # as recorded. The reasoner reads this alongside runway.
    bank_net = round(bank_revenue + bank_transfer + bank_personal - total_observed, 2)

    return ObservedBillsSummary(
        window_days=window_days,
        signals_scanned=signals_scanned,
        total_observed_usd=round(total_observed, 2),
        total_registry_estimate_usd=round(total_estimate, 2),
        total_variance_usd=total_variance,
        payment_declined_active=declined_active,
        unmatched_vendors_count=unmatched,
        vendors=vendors,
        bank_revenue_usd=round(bank_revenue, 2),
        bank_transfer_usd=round(bank_transfer, 2),
        bank_personal_usd=round(bank_personal, 2),
        bank_txn_count=bank_txn_count,
        bank_net_usd=bank_net,
        founder_borrow_usd=round(founder_borrow_usd, 2),
        founder_deposits_usd=round(founder_deposits_usd, 2),
        founder_cash_card_usd=round(founder_cash_card_usd, 2),
    )


def observed_bills_briefing_lines(summary: ObservedBillsSummary, *, top_n: int = 10) -> list[str]:
    """Render an ObservedBillsSummary as markdown lines for the pre-shift briefing.

    Non-empty, actionable format — the top-N vendors by observed spend +
    variance vs. registry, with payment-declined vendors called out first
    (they're the highest-urgency signal).
    """
    if summary.signals_scanned == 0:
        return [
            "## OBSERVED BILLS (Gmail-derived)",
            "- No bill signals in window. Poller may be dark, or window too short.",
        ]

    lines: list[str] = [
        "## OBSERVED BILLS (Gmail + Bank)",
        (f"- Window: {summary.window_days} days | Signals scanned: {summary.signals_scanned}"),
        (
            f"- Observed monthly: ${summary.total_observed_usd:,.2f}    "
            f"Registry estimate: ${summary.total_registry_estimate_usd:,.2f}    "
            f"Variance: ${summary.total_variance_usd:+,.2f}"
        ),
    ]
    if summary.bank_txn_count > 0:
        lines.append(
            f"- Bank activity ({summary.bank_txn_count} txns): "
            f"revenue ${summary.bank_revenue_usd:+,.2f}, "
            f"transfers ${summary.bank_transfer_usd:+,.2f}, "
            f"personal ${summary.bank_personal_usd:+,.2f}, "
            f"NET **${summary.bank_net_usd:+,.2f}**"
        )
    # Founder cash-health (personal Cash App): borrowing signals urgency;
    # deposits + cash-card spend show what the operator is drawing on
    # outside the LLC accounts. Only render when there's actual data.
    if summary.founder_borrow_usd or summary.founder_deposits_usd or summary.founder_cash_card_usd:
        borrow_flag = "  [!] operator is borrowing" if summary.founder_borrow_usd < -50 else ""
        lines.append(
            f"- Founder cash-health: "
            f"borrow ${summary.founder_borrow_usd:+,.2f}, "
            f"deposits ${summary.founder_deposits_usd:+,.2f}, "
            f"personal card ${summary.founder_cash_card_usd:+,.2f}"
            f"{borrow_flag}"
        )
    if summary.payment_declined_active > 0:
        lines.append(
            f"- ⚠ **{summary.payment_declined_active} vendor(s) with recent payment declines** — see list below."
        )
    if summary.unmatched_vendors_count > 0:
        lines.append(
            f"- {summary.unmatched_vendors_count} unmatched vendor(s) — spend not in codb_registry.yaml."
        )

    lines.append("")
    lines.append("| Vendor | Observed | Estimate | Variance | Declines | Signals |")
    lines.append("|---|---|---|---|---|---|")
    # Payment-declined vendors first, then by observed spend
    ordered = sorted(
        summary.vendors,
        key=lambda v: (v.payment_declined_count > 0, v.total_observed_usd),
        reverse=True,
    )[:top_n]
    for v in ordered:
        est_s = f"${v.registry_estimate_usd:,.2f}" if v.registry_estimate_usd is not None else "—"
        var_s = f"${v.variance_usd:+,.2f}" if v.variance_usd is not None else "—"
        decl_s = f"⚠ {v.payment_declined_count}" if v.payment_declined_count else "—"
        label = (
            v.registry_id
            if v.registry_id != _UNMATCHED_VENDOR
            else f"[NEW] {v.vendor_domain or 'unknown'}"
        )
        lines.append(
            f"| {label} | ${v.total_observed_usd:,.2f} | {est_s} | {var_s} | {decl_s} | {v.signal_count} |"
        )
    return lines


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "ObservedBillsSummary",
    "VendorObservation",
    "summarize_observed_bills",
    "observed_bills_briefing_lines",
]
