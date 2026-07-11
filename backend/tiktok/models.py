"""Dataclasses for the TikTok Shop surface (stdlib-only)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class TrendingProduct:
    """A product surfaced by trend research — a candidate to promote/sell."""

    title: str
    price_usd: float = 0.0
    sales: int = 0  # units sold (provider-reported)
    rating: float = 0.0
    category: str = ""
    image_url: str = ""
    source_url: str = ""
    external_id: str = ""  # provider/product id

    @classmethod
    def from_provider(cls, row: dict[str, Any]) -> "TrendingProduct":
        return cls(
            title=str(row.get("title") or row.get("name") or "").strip(),
            price_usd=_to_float(row.get("price") or row.get("price_usd")),
            sales=int(_to_float(row.get("sales") or row.get("sold_count") or row.get("orders"))),
            rating=_to_float(row.get("rating") or row.get("review_rating")),
            category=str(row.get("category") or "").strip(),
            image_url=str(
                row.get("image_url") or row.get("image") or row.get("thumbnail") or ""
            ).strip(),
            source_url=str(row.get("source_url") or row.get("url") or "").strip(),
            external_id=str(row.get("id") or row.get("product_id") or "").strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "price_usd": round(self.price_usd, 2),
            "sales": self.sales,
            "rating": round(self.rating, 2),
            "category": self.category,
            "image_url": self.image_url,
            "source_url": self.source_url,
            "external_id": self.external_id,
        }


@dataclass
class CampaignResult:
    """Outcome of building/posting one product campaign video."""

    ok: bool
    product_title: str = ""
    reel_path: str = ""
    caption: str = ""
    posted: bool = False
    post_id: str = ""
    dry_run: bool = False
    status: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "product_title": self.product_title,
            "reel_path": self.reel_path,
            "caption": self.caption,
            "posted": self.posted,
            "post_id": self.post_id,
            "dry_run": self.dry_run,
            "status": self.status,
            "error": self.error,
        }


def _to_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


__all__ = ["TrendingProduct", "CampaignResult"]
