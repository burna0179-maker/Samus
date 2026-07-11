"""Capability marketplace — published-capability registry with weighted selection.

Source: `Samus/recovery/agent_civilization_blueprint.md`, "Capability marketplace"
section (chat 44). Selection criteria called out in the source paper:
reliability score + cost + latency + capability fit.

This module is pure substrate. It holds listings, returns them, and ranks
them by a configurable weighted composite. It does NOT call providers,
spend credits, or enforce SLAs — those belong upstream.

Keying
------
A listing is identified by ``(provider_agent, capability_id)``. The same
capability can have many providers, and the same provider can offer many
capabilities. Republishing the same pair replaces the prior listing.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CapabilityListing:
    """A single advertised capability offer."""

    capability_id: str
    provider_agent: str
    cost: int  # credits per invocation
    performance_score: float  # 0.0 .. 1.0; treated as reliability proxy
    latency_ms: int  # mean latency in milliseconds
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class SelectionWeights:
    """Weights for `compose_score`. Must sum to exactly 1.0."""

    performance: float = 0.5
    cost: float = 0.25  # lower is better → inverted before weighting
    latency: float = 0.15  # lower is better → inverted before weighting
    tag_fit: float = 0.10  # fraction of required_tags present

    def __post_init__(self) -> None:
        total = self.performance + self.cost + self.latency + self.tag_fit
        # Tolerate the usual float jitter from constants like 0.1 + 0.2.
        if abs(total - 1.0) > 1e-9:
            raise ValueError(
                f"SelectionWeights must sum to 1.0, got {total!r} "
                f"(performance={self.performance}, cost={self.cost}, "
                f"latency={self.latency}, tag_fit={self.tag_fit})"
            )


def compose_score(
    listing: CapabilityListing,
    required_tags: tuple[str, ...],
    weights: SelectionWeights,
    cost_norm: int,
    latency_norm: int,
) -> float:
    """Weighted composite score in [0.0, 1.0].

    Formula::

        performance * w.performance
        + (1 - cost / cost_norm)        * w.cost
        + (1 - latency_ms / latency_norm) * w.latency
        + (matching_tags / len(required_tags)) * w.tag_fit

    Edge rules
    ----------
    * If ``cost_norm == 0`` (everything is free) the cost term collapses
      to ``w.cost`` — every candidate is "best on cost".
    * Same for ``latency_norm == 0``.
    * If ``required_tags == ()`` the tag-fit term is the full
      ``w.tag_fit`` — "no requirement" counts as fully satisfied.
    * `performance_score` is clamped to ``[0.0, 1.0]`` defensively;
      higher-than-1.0 inputs would otherwise let a listing exceed the
      [0, 1] output contract.
    """
    perf = max(0.0, min(1.0, listing.performance_score))

    if cost_norm <= 0:
        cost_term = 1.0
    else:
        cost_term = 1.0 - (listing.cost / cost_norm)
        cost_term = max(0.0, min(1.0, cost_term))

    if latency_norm <= 0:
        latency_term = 1.0
    else:
        latency_term = 1.0 - (listing.latency_ms / latency_norm)
        latency_term = max(0.0, min(1.0, latency_term))

    if not required_tags:
        tag_term = 1.0
    else:
        matched = sum(1 for tag in required_tags if tag in listing.tags)
        tag_term = matched / len(required_tags)

    return (
        perf * weights.performance
        + cost_term * weights.cost
        + latency_term * weights.latency
        + tag_term * weights.tag_fit
    )


class CapabilityMarketplace:
    """In-memory registry of capability listings."""

    def __init__(self) -> None:
        # Keyed by (provider_agent, capability_id) so the same provider
        # can offer many capabilities and the same capability can have
        # many providers.
        self._listings: dict[tuple[str, str], CapabilityListing] = {}

    # ------------------------------------------------------------------
    # Mutation API
    # ------------------------------------------------------------------
    def publish(self, listing: CapabilityListing) -> None:
        """Register / replace a listing.

        Republishing the same (provider_agent, capability_id) pair
        overwrites the previous record — agents update their own
        listings by republishing.
        """
        key = (listing.provider_agent, listing.capability_id)
        self._listings[key] = listing

    def withdraw(self, capability_id: str, provider_agent: str) -> bool:
        """Remove a listing.

        Returns True if a listing existed and was removed, False if no
        matching pair was found (idempotent).
        """
        key = (provider_agent, capability_id)
        return self._listings.pop(key, None) is not None

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------
    def list_providers(self, capability_id: str) -> list[CapabilityListing]:
        """All listings advertising ``capability_id``."""
        return [
            listing for listing in self._listings.values() if listing.capability_id == capability_id
        ]

    def all_capabilities(self) -> list[str]:
        """Sorted, de-duplicated list of every advertised capability_id."""
        return sorted({listing.capability_id for listing in self._listings.values()})

    def select_best(
        self,
        capability_id: str,
        required_tags: tuple[str, ...] = (),
        weights: SelectionWeights | None = None,
        max_cost: int | None = None,
        max_latency_ms: int | None = None,
    ) -> CapabilityListing | None:
        """Pick the highest-composite-score listing matching constraints.

        Filters
        -------
        * ``max_cost``       — if set, listings with ``cost > max_cost`` drop out.
        * ``max_latency_ms`` — if set, listings with ``latency_ms >
          max_latency_ms`` drop out.

        ``required_tags`` is NOT a filter — it feeds the tag-fit score
        term, so a listing missing the tags is still considered, just
        ranked lower.

        Returns ``None`` if no listing survives the constraint filters.
        """
        if weights is None:
            weights = SelectionWeights()

        candidates = self.list_providers(capability_id)
        if max_cost is not None:
            candidates = [c for c in candidates if c.cost <= max_cost]
        if max_latency_ms is not None:
            candidates = [c for c in candidates if c.latency_ms <= max_latency_ms]

        if not candidates:
            return None

        cost_norm = max((c.cost for c in candidates), default=0)
        latency_norm = max((c.latency_ms for c in candidates), default=0)

        # Stable tie-break: prefer the listing seen first under the
        # same score (Python's max returns the first-seen on ties).
        return max(
            candidates,
            key=lambda c: compose_score(c, required_tags, weights, cost_norm, latency_norm),
        )
