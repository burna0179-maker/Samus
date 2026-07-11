"""Deterministic prospect-signal scoring for the signal_filter workcell.

Maps the raw enrichment output (:func:`backend.signal_filter.enrichment.enrich`)
onto a :class:`ProspectSignal` — seven float axes, each clamped to
``[0.0, 1.0]``. Every axis is a pure deterministic function of the enrichment
dict: identical input always yields identical output, and there are no LLM
calls or randomness anywhere in this module.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


def _clamp(value: float) -> float:
    """Clamp ``value`` into the closed unit interval ``[0.0, 1.0]``."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return float(value)


@dataclass
class ProspectSignal:
    """Seven scored signal axes for one prospect, each in ``[0.0, 1.0]``.

    - ``domain_health``           — DNS resolves, MX present, valid TLS cert.
    - ``seo_score``               — homepage on-page SEO heuristic (0-100 → unit).
    - ``review_velocity``         — Maps review count + rating strength.
    - ``contactability``          — phone / email / contact-surface present.
    - ``social_activity``         — discoverable social profiles.
    - ``revenue_estimate``        — firmographic revenue proxy (neutral if no key).
    - ``infrastructure_maturity`` — site reachable, HTTPS, real (not parked).
    """

    domain_health: float = 0.0
    seo_score: float = 0.0
    review_velocity: float = 0.0
    contactability: float = 0.0
    social_activity: float = 0.0
    revenue_estimate: float = 0.0
    infrastructure_maturity: float = 0.0

    def __post_init__(self) -> None:
        # Clamp every field on construction so no downstream consumer ever
        # sees an out-of-range axis, regardless of how the dataclass was built.
        self.domain_health = _clamp(self.domain_health)
        self.seo_score = _clamp(self.seo_score)
        self.review_velocity = _clamp(self.review_velocity)
        self.contactability = _clamp(self.contactability)
        self.social_activity = _clamp(self.social_activity)
        self.revenue_estimate = _clamp(self.revenue_estimate)
        self.infrastructure_maturity = _clamp(self.infrastructure_maturity)

    def as_dict(self) -> dict[str, float]:
        """Return the seven axes as a plain ``dict[str, float]``."""
        return asdict(self)


def _score_domain_health(dns: dict[str, Any], ssl_info: dict[str, Any]) -> float:
    """DNS resolution + MX presence + TLS validity → domain_health."""
    score = 0.0
    if dns.get("resolves"):
        score += 0.45
    if dns.get("has_mx"):
        score += 0.25
    if ssl_info.get("ssl_valid"):
        score += 0.30
    elif ssl_info.get("has_cert"):
        # Cert present but did not verify (self-signed / expired) — partial.
        score += 0.10
    return _clamp(score)


def _score_seo(site: dict[str, Any]) -> float:
    """Prospecting SEO heuristic (0-100 int) → unit float."""
    raw = site.get("seo_score")
    try:
        return _clamp(float(raw) / 100.0)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: Any) -> int:
    """Best-effort int parse for Places-style string numerics."""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def _score_review_velocity(enrichment: dict[str, Any]) -> float:
    """Google Maps review count + rating → review_velocity.

    Review count is the dominant term (a business with sustained reviews is
    an operating business); rating quality is a secondary modifier. The
    count is mapped log-style via fixed tiers so a handful of reviews still
    registers without one viral business saturating the axis.
    """
    count = _to_int(enrichment.get("review_count"))
    rating = _to_float(enrichment.get("review_rating"))

    if count >= 100:
        count_score = 0.80
    elif count >= 40:
        count_score = 0.60
    elif count >= 15:
        count_score = 0.40
    elif count >= 5:
        count_score = 0.25
    elif count >= 1:
        count_score = 0.10
    else:
        count_score = 0.0

    # Rating contributes up to 0.20: a 4.0+ star average is the bar.
    rating_score = 0.0
    if rating >= 4.5:
        rating_score = 0.20
    elif rating >= 4.0:
        rating_score = 0.15
    elif rating >= 3.0:
        rating_score = 0.08

    return _clamp(count_score + rating_score)


def _score_contactability(enrichment: dict[str, Any]) -> float:
    """Phone + email / contact-surface presence → contactability."""
    score = 0.0
    if str(enrichment.get("phone") or "").strip():
        score += 0.40

    owner = (enrichment.get("site") or {}).get("owner_signals") or {}
    if str(owner.get("owner_email") or "").strip():
        score += 0.40
    elif str(owner.get("contact_emails") or "").strip():
        score += 0.25
    return _clamp(score)


def _score_social_activity(enrichment: dict[str, Any]) -> float:
    """Discoverable social profiles → social_activity."""
    owner = (enrichment.get("site") or {}).get("owner_signals") or {}
    score = 0.0
    if str(owner.get("social_facebook") or "").strip():
        score += 0.40
    if str(owner.get("social_instagram") or "").strip():
        score += 0.30
    if str(owner.get("social_linkedin") or "").strip():
        score += 0.30

    firmographics = enrichment.get("firmographics") or {}
    if firmographics.get("has_linkedin"):
        score += 0.20
    return _clamp(score)


def _score_revenue_estimate(enrichment: dict[str, Any]) -> float:
    """Firmographic revenue proxy → revenue_estimate.

    Firmographics are an *optional* paid-API enrichment. When unavailable
    (the local-first default) this returns a neutral ``0.5`` so the axis
    neither rewards nor penalizes a prospect for missing paid data.
    """
    firmographics = enrichment.get("firmographics") or {}
    if not firmographics.get("available"):
        return 0.5

    revenue = _to_float(firmographics.get("estimated_revenue"))
    employees = _to_int(firmographics.get("employee_count"))

    revenue_score = 0.0
    if revenue >= 1_000_000:
        revenue_score = 0.60
    elif revenue >= 250_000:
        revenue_score = 0.45
    elif revenue >= 50_000:
        revenue_score = 0.30
    elif revenue > 0:
        revenue_score = 0.15

    employee_score = 0.0
    if employees >= 20:
        employee_score = 0.40
    elif employees >= 5:
        employee_score = 0.25
    elif employees >= 1:
        employee_score = 0.10

    return _clamp(revenue_score + employee_score)


def _score_infrastructure_maturity(site: dict[str, Any], ssl_info: dict[str, Any]) -> float:
    """Site reachable + HTTPS + real (not parked) → infrastructure_maturity."""
    score = 0.0
    if site.get("reachable"):
        score += 0.45
    if not site.get("dead_or_junk", True):
        score += 0.30
    if ssl_info.get("ssl_valid"):
        score += 0.25
    elif ssl_info.get("has_cert"):
        score += 0.10
    return _clamp(score)


def signals_from_enrichment(enrichment: dict[str, Any]) -> ProspectSignal:
    """Deterministically map a raw enrichment dict to a :class:`ProspectSignal`.

    Pure function — no I/O, no randomness. ``enrichment`` is the output of
    :func:`backend.signal_filter.enrichment.enrich`. Missing sub-dicts
    default to empty so a sparse enrichment never raises.
    """
    dns = enrichment.get("dns") or {}
    ssl_info = enrichment.get("ssl") or {}
    site = enrichment.get("site") or {}

    return ProspectSignal(
        domain_health=_score_domain_health(dns, ssl_info),
        seo_score=_score_seo(site),
        review_velocity=_score_review_velocity(enrichment),
        contactability=_score_contactability(enrichment),
        social_activity=_score_social_activity(enrichment),
        revenue_estimate=_score_revenue_estimate(enrichment),
        infrastructure_maturity=_score_infrastructure_maturity(site, ssl_info),
    )


__all__ = ["ProspectSignal", "signals_from_enrichment"]
