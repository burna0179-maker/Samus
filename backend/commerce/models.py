"""Lean dataclasses for the Medusa commerce surface (stdlib-only)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MedusaProduct:
    id: str
    title: str
    description: str = ""
    status: str = "draft"  # draft | proposed | published | rejected
    price_usd_cents: int = 0
    currency: str = "usd"
    thumbnail: str = ""
    handle: str = ""

    @classmethod
    def from_api(cls, row: dict[str, Any]) -> "MedusaProduct":
        # Medusa nests price under variants[].prices[]; take the first USD price.
        cents, currency = _first_price(row)
        return cls(
            id=str(row.get("id") or ""),
            title=str(row.get("title") or ""),
            description=str(row.get("description") or "") or "",
            status=str(row.get("status") or "draft"),
            price_usd_cents=cents,
            currency=currency,
            thumbnail=str(row.get("thumbnail") or "") or "",
            handle=str(row.get("handle") or "") or "",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "price_usd_cents": self.price_usd_cents,
            "currency": self.currency,
            "thumbnail": self.thumbnail,
            "handle": self.handle,
        }


@dataclass
class MedusaOrder:
    id: str
    display_id: str = ""
    email: str = ""
    status: str = ""
    total_usd_cents: int = 0
    currency: str = "usd"
    created_at: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_api(cls, row: dict[str, Any]) -> "MedusaOrder":
        total = row.get("total")
        # Medusa v2 totals are major-unit floats in some configs; normalize to cents.
        cents = int(round(float(total) * 100)) if isinstance(total, float) else int(total or 0)
        return cls(
            id=str(row.get("id") or ""),
            display_id=str(row.get("display_id") or ""),
            email=str(row.get("email") or "") or "",
            status=str(row.get("status") or row.get("fulfillment_status") or "") or "",
            total_usd_cents=cents,
            currency=str(row.get("currency_code") or "usd"),
            created_at=str(row.get("created_at") or "") or "",
            items=list(row.get("items") or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_id": self.display_id,
            "email": self.email,
            "status": self.status,
            "total_usd_cents": self.total_usd_cents,
            "currency": self.currency,
            "created_at": self.created_at,
            "item_count": len(self.items),
        }


def _first_price(row: dict[str, Any]) -> tuple[int, str]:
    for variant in row.get("variants") or []:
        for price in variant.get("prices") or []:
            amt = price.get("amount")
            if amt is not None:
                return int(amt), str(price.get("currency_code") or "usd")
    return 0, "usd"


__all__ = ["MedusaProduct", "MedusaOrder"]
