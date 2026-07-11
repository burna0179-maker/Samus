"""Revenue-stream partition — separate income sources, cleanly attributed.

HustleForge earns from multiple streams (client *services*, *product* sales via the
internal store, and *TikTok Shop*). This module lets each stream use its **own
Stripe account** and carry its **own legal-entity label**, so the books separate
without rewriting the finance layer.

Design (the "one LLC now, split later" posture Alex chose):

* Each stream resolves a Stripe key. An **unset** stream key falls back to the
  existing ``stripe_api_key`` — so today's single-account behavior is unchanged
  until an operator arms a stream with its own key.
* Each stream carries an ``entity`` label, defaulting to ``revenue_entity_default``
  ("HustleForge LLC"). Setting a stream's own key + entity **promotes it to its own
  legal entity with no code change**.
* :func:`attribute` stamps ``revenue_stream`` + ``entity`` onto any income / receipt
  / ledger record so downstream accounting can split by source.

Reuses :class:`backend.finance.stripe_client.StripeClient` (which already takes the
key per-construction), so partitioning costs nothing structurally.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# The canonical stream ids. ``services`` is the default/legacy stream and always
# resolves to ``stripe_api_key`` unless explicitly given its own key.
SERVICES = "services"
PRODUCTS = "products"
TIKTOK_SHOP = "tiktok_shop"


@dataclass(frozen=True)
class RevenueStream:
    stream_id: str
    display_name: str
    key_field: str          # Settings attr holding this stream's Stripe key ("" => pooled)
    webhook_field: str      # Settings attr holding this stream's webhook secret
    entity_field: str       # Settings attr holding this stream's entity override


STREAMS: dict[str, RevenueStream] = {
    SERVICES: RevenueStream(SERVICES, "Client Services", "stripe_api_key",
                            "stripe_webhook_secret", ""),
    PRODUCTS: RevenueStream(PRODUCTS, "Product Sales (Store)", "stripe_api_key_products",
                            "stripe_webhook_secret_products", "revenue_entity_products"),
    TIKTOK_SHOP: RevenueStream(TIKTOK_SHOP, "TikTok Shop", "stripe_api_key_tiktok",
                               "stripe_webhook_secret_tiktok", "revenue_entity_tiktok"),
}


def get_stream(stream_id: str) -> RevenueStream:
    if stream_id not in STREAMS:
        raise KeyError(f"unknown revenue stream: {stream_id!r}")
    return STREAMS[stream_id]


def list_streams() -> list[RevenueStream]:
    return list(STREAMS.values())


def resolve_stripe_key(stream_id: str, settings: Any) -> str:
    """Return the stream's Stripe key, falling back to the default account.

    The ``services`` stream IS the default key. Any other stream with an empty
    key pools under ``stripe_api_key`` so unarmed streams still transact (under
    the LLC) instead of failing — partition is opt-in per stream.
    """
    stream = get_stream(stream_id)
    default = (getattr(settings, "stripe_api_key", "") or "").strip()
    if stream.key_field == "stripe_api_key":
        return default
    own = (getattr(settings, stream.key_field, "") or "").strip()
    return own or default


def resolve_webhook_secret(stream_id: str, settings: Any) -> str:
    stream = get_stream(stream_id)
    default = (getattr(settings, "stripe_webhook_secret", "") or "").strip()
    if stream.webhook_field == "stripe_webhook_secret":
        return default
    own = (getattr(settings, stream.webhook_field, "") or "").strip()
    return own or default


def entity_for(stream_id: str, settings: Any) -> str:
    """The legal-entity label money in this stream belongs to."""
    stream = get_stream(stream_id)
    default = (getattr(settings, "revenue_entity_default", "") or "HustleForge LLC").strip()
    if not stream.entity_field:
        return default
    override = (getattr(settings, stream.entity_field, "") or "").strip()
    return override or default


def has_own_account(stream_id: str, settings: Any) -> bool:
    """True when the stream is armed with its OWN Stripe key (truly partitioned),
    not pooled under the default account."""
    stream = get_stream(stream_id)
    if stream.key_field == "stripe_api_key":
        return bool((getattr(settings, "stripe_api_key", "") or "").strip())
    return bool((getattr(settings, stream.key_field, "") or "").strip())


def stripe_client_for(stream_id: str, settings: Any):
    """Construct a :class:`StripeClient` bound to this stream's account.

    Returns ``None`` when no key resolves (neither own nor default) so callers
    fail closed instead of constructing an unusable client.
    """
    key = resolve_stripe_key(stream_id, settings)
    if not key:
        return None
    from backend.finance.stripe_client import StripeClient

    return StripeClient(api_key=key)


def attribute(record: dict[str, Any], stream_id: str, settings: Any | None = None) -> dict[str, Any]:
    """Stamp ``revenue_stream`` + ``entity`` onto an income/receipt/ledger record.

    Returns the same dict (mutated) for convenient chaining. ``entity`` is only
    added when ``settings`` is provided (otherwise just the stream id is stamped).
    """
    record["revenue_stream"] = stream_id
    if settings is not None and stream_id in STREAMS:
        record["entity"] = entity_for(stream_id, settings)
    return record


def stream_summary(settings: Any) -> list[dict[str, Any]]:
    """Operator-facing view: which streams are partitioned vs. pooled."""
    out: list[dict[str, Any]] = []
    for stream in list_streams():
        out.append({
            "stream_id": stream.stream_id,
            "display_name": stream.display_name,
            "entity": entity_for(stream.stream_id, settings),
            "own_account": has_own_account(stream.stream_id, settings),
            "transacts": bool(resolve_stripe_key(stream.stream_id, settings)),
        })
    return out


__all__ = [
    "SERVICES", "PRODUCTS", "TIKTOK_SHOP", "RevenueStream", "STREAMS",
    "get_stream", "list_streams", "resolve_stripe_key", "resolve_webhook_secret",
    "entity_for", "has_own_account", "stripe_client_for", "attribute", "stream_summary",
]
