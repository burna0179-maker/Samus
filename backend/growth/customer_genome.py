"""Customer Genome Engine (CGE).

Builds and persists per-customer behavioral profiles by merging:
  * CustomerStore lifecycle state (current_state, current_state_since)
  * Stripe charges + subscription data (LTV, refund count, MRR)

Produces a segmented genome used by campaign and sales action selection.
Persisted as JSON under $SAMUS_DATA_ROOT/growth/customer_genome.json.

Segmentation tiers (evaluated in priority order):
  ACTIVE    -- has an active retainer or renewal (highest engagement signal)
  VIP       -- LTV >= $1000, zero refunds
  RISKY     -- 2+ refunds or current_state == "churned"
  PROMISING -- engagement_score >= 0.5 and positive LTV
  COLD      -- everything else

Entry point::

    engine = CustomerGenomeEngine(stripe_api_key=settings.stripe_api_key)
    summary = engine.refresh()          # rebuild + persist + return summary
    actions = engine.suggest_sales_actions(max_actions=20)
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_LOG = logging.getLogger("samus.growth.customer_genome")


def _growth_root() -> Path:
    base = Path(os.getenv("SAMUS_DATA_ROOT", "data"))
    p = base / "growth"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Genome shapes
# ---------------------------------------------------------------------------

@dataclass
class CustomerProfile:
    customer_id: str
    email: str
    current_state: str
    lifetime_value_usd: float = 0.0
    mrr_usd: float = 0.0
    refund_count: int = 0
    charge_count: int = 0
    state_age_days: float = 0.0
    engagement_score: float = 0.0   # (charges - refunds) / charges
    segment: str = "COLD"
    last_updated: float = 0.0


@dataclass
class GenomeSummary:
    total: int
    by_segment: dict[str, int]
    refreshed_at: str
    profiles: list[CustomerProfile] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------

def _segment(p: CustomerProfile) -> str:
    if p.current_state in ("active_retainer", "renewed"):
        return "ACTIVE"
    if p.lifetime_value_usd >= 1000.0 and p.refund_count == 0:
        return "VIP"
    if p.refund_count >= 2 or p.current_state == "churned":
        return "RISKY"
    if p.engagement_score >= 0.5 and p.lifetime_value_usd > 0:
        return "PROMISING"
    return "COLD"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class CustomerGenomeEngine:
    """Builds per-customer behavioral profiles from CRM + Stripe data."""

    def __init__(
        self,
        root: Path | None = None,
        stripe_api_key: str = "",
    ) -> None:
        self._root = root if root is not None else _growth_root()
        self._root.mkdir(parents=True, exist_ok=True)
        self._genome_path = self._root / "customer_genome.json"
        self._stripe_key = stripe_api_key

    # --- persistence ---------------------------------------------------------

    def _load_raw(self) -> dict[str, dict[str, Any]]:
        if not self._genome_path.exists():
            return {}
        try:
            return json.loads(self._genome_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("customer_genome load failed, returning empty: %s", exc)
            return {}

    def _save_raw(self, genome: dict[str, dict[str, Any]]) -> None:
        try:
            self._genome_path.write_text(
                json.dumps(genome, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("customer_genome save failed: %s", exc)

    # --- Stripe data enrichment ----------------------------------------------

    def _stripe_summary(self, email: str) -> dict[str, Any]:
        """Fetch LTV + refund count + MRR for one customer email via Stripe."""
        if not self._stripe_key:
            return {}
        try:
            from backend.finance.stripe_client import (
                StripeClient,
                monthly_recurring_revenue_usd,
            )
            client = StripeClient(self._stripe_key)
            stripe_customers = client.fetch_customers_by_email(email, limit=5)
            if not stripe_customers:
                return {}
            cid = stripe_customers[0].id

            charges = client.fetch_charges(limit=100, customer=cid)
            ltv = sum(
                c.amount / 100.0
                for c in charges
                if c.status == "succeeded" and c.paid and c.currency.lower() == "usd"
            )
            # Stripe represents refunds as negative amounts on separate charges;
            # count any succeeded charge with a negative amount as a refund event.
            refunds = sum(1 for c in charges if c.status == "succeeded" and c.amount < 0)
            subs = client.fetch_subscriptions(customer=cid)
            mrr = monthly_recurring_revenue_usd(subs)

            return {
                "stripe_customer_id": cid,
                "lifetime_value_usd": round(ltv, 2),
                "refund_count": refunds,
                "charge_count": len(charges),
                "mrr_usd": mrr,
            }
        except Exception as exc:  # noqa: BLE001 — Stripe unavailable is non-fatal
            _LOG.debug("stripe_summary failed email=%s: %s", email, exc)
            return {}

    # --- public API ----------------------------------------------------------

    def refresh(self, limit: int = 200) -> GenomeSummary:
        """Pull CRM customers + Stripe data, rebuild genome, persist, return summary.

        Stripe calls are best-effort per customer — a Stripe failure enriches
        that customer with zero-valued financial fields rather than aborting.
        Neo4j down: logs a warning, returns an empty summary (never raises).
        """
        from backend.common.dates import iso_now

        genome = self._load_raw()
        profiles: list[CustomerProfile] = []

        try:
            from backend.memory.customers import CustomerStore
            store = CustomerStore()
            customers = store.list_customers(limit=max(1, min(int(limit), 1000)))
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("customer_genome CustomerStore unavailable: %s", exc)
            customers = []

        for cust in customers:
            stripe = self._stripe_summary(cust.email)
            ltv = float(stripe.get("lifetime_value_usd", 0.0))
            refunds = int(stripe.get("refund_count", 0))
            charge_count = int(stripe.get("charge_count", 0))
            mrr = float(stripe.get("mrr_usd", 0.0))

            engagement = (
                (charge_count - refunds) / float(charge_count)
                if charge_count > 0 else 0.0
            )
            state_age = max(0.0, time.time() - cust.current_state_since) / 86400.0

            p = CustomerProfile(
                customer_id=cust.id,
                email=cust.email,
                current_state=cust.current_state,
                lifetime_value_usd=ltv,
                mrr_usd=mrr,
                refund_count=refunds,
                charge_count=charge_count,
                state_age_days=round(state_age, 1),
                engagement_score=round(max(0.0, min(1.0, engagement)), 4),
                last_updated=time.time(),
            )
            p.segment = _segment(p)
            genome[cust.id] = asdict(p)
            profiles.append(p)

        self._save_raw(genome)

        by_segment: dict[str, int] = {}
        for p in profiles:
            by_segment[p.segment] = by_segment.get(p.segment, 0) + 1

        _LOG.info(
            "customer_genome refreshed total=%d segments=%s",
            len(profiles),
            by_segment,
        )
        return GenomeSummary(
            total=len(profiles),
            by_segment=by_segment,
            refreshed_at=iso_now(),
            profiles=profiles,
        )

    def suggest_sales_actions(
        self,
        max_actions: int = 20,
    ) -> list[dict[str, Any]]:
        """Return segment-based sales action dicts from the persisted genome.

        Does NOT refresh — call refresh() upstream when freshness matters.
        Each action has: kind, customer_id, segment, playbook.
        """
        genome = self._load_raw()
        by_segment: dict[str, list[str]] = {}
        for cid, entry in genome.items():
            seg = str(entry.get("segment", "COLD"))
            by_segment.setdefault(seg, []).append(cid)

        slot = max(1, max_actions // 4)
        actions: list[dict[str, Any]] = []

        for cid in by_segment.get("RISKY", [])[:slot]:
            actions.append({
                "kind": "rescue_sequence",
                "customer_id": cid,
                "segment": "RISKY",
                "playbook": "refund_recovery_v1",
            })
        for cid in by_segment.get("VIP", [])[:slot]:
            actions.append({
                "kind": "vip_upsell",
                "customer_id": cid,
                "segment": "VIP",
                "playbook": "vip_appreciation_v1",
            })
        for cid in by_segment.get("PROMISING", [])[:slot]:
            actions.append({
                "kind": "activation_sequence",
                "customer_id": cid,
                "segment": "PROMISING",
                "playbook": "activation_v1",
            })
        for cid in by_segment.get("COLD", [])[:slot]:
            actions.append({
                "kind": "reactivation_probe",
                "customer_id": cid,
                "segment": "COLD",
                "playbook": "cold_reactivation_v1",
            })

        _LOG.info("customer_genome suggested %d sales actions", len(actions))
        return actions

    def load_profiles(self) -> list[CustomerProfile]:
        """Load all persisted profiles as CustomerProfile objects."""
        raw = self._load_raw()
        out: list[CustomerProfile] = []
        for entry in raw.values():
            try:
                out.append(CustomerProfile(**{
                    k: entry[k] for k in CustomerProfile.__dataclass_fields__  # type: ignore[attr-defined]
                    if k in entry
                }))
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("customer_genome bad entry: %s", exc)
        return out


__all__ = ["CustomerGenomeEngine", "CustomerProfile", "GenomeSummary"]
