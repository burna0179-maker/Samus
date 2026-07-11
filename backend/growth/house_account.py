"""Hustleforge as Client Zero — the house account + its canonical Brand Brain.

Strategic intent (operator): run Hustleforge itself as Samus's flagship internal
client so the growth machinery (website, social, seo, content, visibility) is
pointed inward to continuously improve HF's own web + media presence — growing
traffic -> conversions -> MRR — while every improvement doubles as a live case
study that proves the same service to external clients.

This module is the ADDITIVE seam, not a parallel system:

  * Identity reuses the ONE canonical client-key function
    (``backend.website.client_assets.derive_client_key``) with a fixed
    ``account_id="hustleforge"`` -> key ``"hustleforge"``. The house account
    therefore flows through every account-scoped surface (asset store, growth
    dispatch, scorecards) exactly like an external client — no special path.
  * The Brand Brain — the centralized origin / ICP / offer-ladder / voice /
    pillars the audit found scattered across the codebase — lives in ONE seeded
    data file (``house_account.yaml``) loaded into a typed ``BrandProfile``
    here, so every content/SEO/social surface pulls the same inputs ("right
    inputs beat raw power").

Graceful: a missing / malformed profile file degrades to a minimal hardcoded
default + a logged warning; this module never raises on load, so the loop still
runs against the live domain.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

_LOG = logging.getLogger("samus.growth.house_account")

HOUSE_ACCOUNT_ID: str = "hustleforge"
_PROFILE_PATH: Path = Path(__file__).parent / "house_account.yaml"

# Essentials the loop needs even if the seeded YAML is absent/corrupt. Kept in
# sync with house_account.yaml; the YAML is the rich source, this is the
# fail-safe floor (never a placeholder — these are real, usable values).
_DEFAULT_DOMAIN: str = "https://www.hustleforge.tech"
_DEFAULT_WORDPRESS_SITE: str = "hustleforge.tech"
_DEFAULT_REVENUE_GOAL_MONTHLY_USD: float = 50000.0


@dataclass(frozen=True)
class OfferRung:
    """One rung of the value ladder (mirrors the live catalog/registry.py)."""

    name: str
    price_usd: float
    recurring: bool
    role: str  # tripwire | core | ascension


@dataclass(frozen=True)
class BrandProfile:
    """The centralized Brand Brain for the Hustleforge house account."""

    account_id: str
    display_name: str
    domain: str
    wordpress_site: str
    positioning: str
    origin_story: str
    icp: str
    offer_ladder: tuple[OfferRung, ...]
    content_pillars: tuple[str, ...]
    target_keywords: tuple[str, ...]
    brand_voice: str
    channels: tuple[str, ...]
    revenue_goal_monthly_usd: float
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def offer_ladder_lines(self) -> list[str]:
        """Human-readable value-ladder lines (for briefs / case studies)."""
        lines: list[str] = []
        for rung in self.offer_ladder:
            suffix = "/mo" if rung.recurring else ""
            lines.append(f"{rung.name} - ${rung.price_usd:,.0f}{suffix} ({rung.role})")
        return lines

    def recurring_rungs(self) -> tuple[OfferRung, ...]:
        """The MRR-bearing rungs — the path to the monthly revenue goal."""
        return tuple(r for r in self.offer_ladder if r.recurring)


def house_client_key() -> str:
    """The canonical client key for Hustleforge (Client Zero).

    Reuses the single identity function so the house account resolves to the
    same owner key everywhere external clients do. Deferred import keeps the
    growth import graph free of a hard growth->website coupling.
    """
    from backend.website.client_assets import derive_client_key

    return derive_client_key(account_id=HOUSE_ACCOUNT_ID)


def is_house_account(account_id: str) -> bool:
    """True iff ``account_id`` is the Hustleforge house account (case-insensitive)."""
    return (account_id or "").strip().lower() == HOUSE_ACCOUNT_ID


def _offer_ladder_from(raw: Any) -> tuple[OfferRung, ...]:
    rungs: list[OfferRung] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                rungs.append(
                    OfferRung(
                        name=str(item.get("name", "")).strip(),
                        price_usd=float(item.get("price_usd", 0) or 0),
                        recurring=bool(item.get("recurring", False)),
                        role=str(item.get("role", "core")).strip() or "core",
                    )
                )
            except (TypeError, ValueError):
                continue
    return tuple(r for r in rungs if r.name)


def _str_tuple(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, (list, tuple)):
        return tuple(str(x).strip() for x in raw if str(x).strip())
    return ()


def _profile_from_dict(data: dict[str, Any]) -> BrandProfile:
    """Build a BrandProfile from the parsed YAML, tolerant of missing keys."""
    return BrandProfile(
        account_id=str(data.get("account_id", HOUSE_ACCOUNT_ID)) or HOUSE_ACCOUNT_ID,
        display_name=str(data.get("display_name", "HustleForge")) or "HustleForge",
        domain=str(data.get("domain", _DEFAULT_DOMAIN)) or _DEFAULT_DOMAIN,
        wordpress_site=str(data.get("wordpress_site", _DEFAULT_WORDPRESS_SITE)) or _DEFAULT_WORDPRESS_SITE,
        positioning=str(data.get("positioning", "")).strip(),
        origin_story=str(data.get("origin_story", "")).strip(),
        icp=str(data.get("icp", "")).strip(),
        offer_ladder=_offer_ladder_from(data.get("offer_ladder")),
        content_pillars=_str_tuple(data.get("content_pillars")),
        target_keywords=_str_tuple(data.get("target_keywords")),
        brand_voice=str(data.get("brand_voice", "")).strip(),
        channels=_str_tuple(data.get("channels")),
        revenue_goal_monthly_usd=float(
            data.get("revenue_goal_monthly_usd", _DEFAULT_REVENUE_GOAL_MONTHLY_USD)
            or _DEFAULT_REVENUE_GOAL_MONTHLY_USD
        ),
        raw=dict(data),
    )


def _minimal_default() -> BrandProfile:
    """Fail-safe profile when the seeded YAML is missing/corrupt."""
    return BrandProfile(
        account_id=HOUSE_ACCOUNT_ID,
        display_name="HustleForge",
        domain=_DEFAULT_DOMAIN,
        wordpress_site=_DEFAULT_WORDPRESS_SITE,
        positioning="Local-first, operator-configurable AI operations for small businesses.",
        origin_story="",
        icp="Small and local businesses with a broken/absent web presence or fragmented, manual workflows.",
        offer_ladder=(
            OfferRung("SEO Audit", 149.0, False, "tripwire"),
            OfferRung("48-Hour Workflow Rescue", 500.0, False, "tripwire"),
            OfferRung("Workflow System Buildout", 2500.0, False, "core"),
            OfferRung("AI Ops Partner", 2000.0, True, "ascension"),
            OfferRung("AI Ops Partner - Premium", 5000.0, True, "ascension"),
        ),
        content_pillars=(),
        target_keywords=(),
        brand_voice="Blunt, transparent, operator-grade. Cause-and-effect, no hype.",
        channels=("website", "wordpress_blog", "instagram", "facebook", "tiktok", "email"),
        revenue_goal_monthly_usd=_DEFAULT_REVENUE_GOAL_MONTHLY_USD,
        raw={},
    )


@lru_cache(maxsize=1)
def load_brand_profile() -> BrandProfile:
    """Load the seeded Brand Brain from ``house_account.yaml`` (cached).

    Never raises: a missing/malformed file degrades to a minimal hardcoded
    default so the loop still targets the live domain. Call
    ``load_brand_profile.cache_clear()`` to force a reload (tests / after edit).
    """
    try:
        import yaml

        data = yaml.safe_load(_PROFILE_PATH.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("house_account.yaml did not parse to a mapping")
        return _profile_from_dict(data)
    except FileNotFoundError:
        _LOG.warning("house_account.yaml not found at %s; using minimal default", _PROFILE_PATH)
        return _minimal_default()
    except Exception as exc:  # noqa: BLE001 — config read, never blocks the loop
        _LOG.warning("house_account.yaml load failed (%s); using minimal default", exc)
        return _minimal_default()


__all__ = [
    "HOUSE_ACCOUNT_ID",
    "OfferRung",
    "BrandProfile",
    "house_client_key",
    "is_house_account",
    "load_brand_profile",
]
