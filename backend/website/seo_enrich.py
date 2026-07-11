"""SEO + security enrichment for the website deliverable — enforced, not advisory.

Two halves, mirroring the taste subsystem's generate/audit split:

  * **Generate** (:func:`build_seo_package`) — deterministic, zero-LLM. From the
    brief it produces the structured-data (LocalBusiness + Organization +
    optional FAQ JSON-LD via ``backend.seo.schema_builder``) and a per-page SEO
    plan (title <=60, meta description 120-155, keyword coverage) the site needs
    to rank. This is the paste-ready package the operator applies in Wix.

  * **Gate** — fail-closed enforcement so a sub-par site is never marked
    delivered:
      - :func:`check_seo_package` — pre-publish, on the generated package:
        every page has a complete, well-formed title + meta + keyword and the
        JSON-LD is valid. Pure + instant.
      - :func:`evaluate_live` — post-publish, against the real URL: runs the
        existing ``backend.seo.audit_url`` (SEO score) + its passive security
        audit (A-F grade, infra health) and returns the scalars the deliver
        gate enforces (seo_score >= floor, security grade >= floor, zero
        critical findings).

Posture is `off | audit | enforce` (settings ``website_seo_gate`` /
``website_security_gate``), the same shape as ``authz_mode`` — run *audit* to
surface gaps without blocking, flip to *enforce* once trusted.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from backend.seo import schema_builder

from .models import WebsiteBrief

_LOG = logging.getLogger("samus.website.seo_enrich")

_TITLE_MAX = 60
_DESC_MIN = 120
_DESC_MAX = 160  # Google truncates ~155-160; keep a little headroom
_DASH_RE = re.compile("[—–]")

# Security grade ordering for threshold comparison (A best).
_GRADE_ORDER = ("F", "D", "C", "B", "A")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _sanitize(text: str) -> str:
    return _DASH_RE.sub(" - ", " ".join((text or "").split()))


def _clip(text: str, limit: int) -> str:
    text = _sanitize(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip(" ,.;:") + "."


def _meta_description(body: str, name: str, primary: str, city: str, region: str) -> str:
    """Compose an on-brand meta description that lands in [_DESC_MIN, _DESC_MAX].

    Builds up from the page body, then layers value/location/CTA sentences until
    the minimum is met without crossing the maximum. Deterministic, em-dash-free.
    """
    loc = f"{city}, {region}".strip(", ")
    base = _clip(body, _DESC_MAX) or f"{name} provides {primary.lower()}."
    extras = [
        f"{name} provides {primary.lower()}{(' in ' + loc) if loc else ''}.",
        f"Serving {loc} and the surrounding area." if loc else "",
        f"Contact {name} today for a free quote.",
    ]
    desc = base
    for s in extras:
        if len(desc) >= _DESC_MIN:
            break
        s = _sanitize(s)
        if not s:
            continue
        cand = f"{desc} {s}".strip()
        if len(cand) <= _DESC_MAX:
            desc = cand
    # Last-resort floor: pad with a single bounded clause if still short.
    if len(desc) < _DESC_MIN:
        pad = f" {primary} you can rely on."
        if len(desc) + len(pad) <= _DESC_MAX:
            desc = (desc + pad).strip()
    return _clip(desc, _DESC_MAX)


def _parse_address(raw: str) -> dict[str, str]:
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    out = {"street": "", "city": "", "region": "", "postal_code": "", "country": "US"}
    if parts:
        out["street"] = parts[0]
    if len(parts) >= 2:
        out["city"] = parts[1]
    if len(parts) >= 3:
        tail = parts[2].split()
        if tail:
            out["region"] = tail[0]
        # Only accept a real US ZIP (5 or ZIP+4) as the postal code — a
        # parenthetical like "(Rocky Point)" or a community name must not leak
        # into structured data as a bogus postalCode.
        if len(tail) >= 2 and re.fullmatch(r"\d{5}(?:-\d{4})?", tail[1]):
            out["postal_code"] = tail[1]
    return out


def _primary_service(brief: WebsiteBrief) -> str:
    """A short human service label from the industry field (first token)."""
    ind = (brief.industry or "").split(",")[0].strip()
    return ind or "Local Services"


def _keywords(brief: WebsiteBrief, city: str) -> list[str]:
    kws = [k.strip() for k in (brief.industry or "").split(",") if k.strip()]
    if city:
        kws = kws + [f"{kws[0]} {city}" if kws else city]
    # de-dup, keep order, cap
    seen: set[str] = set()
    out: list[str] = []
    for k in kws:
        kl = k.lower()
        if kl not in seen:
            seen.add(kl)
            out.append(k)
    return out[:10]


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------

def build_seo_package(brief: WebsiteBrief, *, public_url: str = "") -> dict[str, Any]:
    """Build the deterministic SEO package (JSON-LD + per-page meta) from a brief."""
    name = brief.business_name
    addr = _parse_address(brief.address)
    city, region = addr["city"], addr["region"]
    primary = _primary_service(brief)
    keywords = _keywords(brief, city)
    desc = _sanitize(brief.business_description) or f"{name} provides {primary.lower()}."

    # --- structured data (JSON-LD) ----------------------------------------
    lb = schema_builder.local_business(
        name=name,
        business_type="LocalBusiness",
        url=public_url,
        telephone=brief.contact_phone,
        street=addr["street"],
        city=city,
        region=region,
        postal_code=addr["postal_code"],
        country=addr["country"],
        image=brief.logo_url,
    )
    # Augment with fields schema_builder.local_business doesn't carry.
    if brief.contact_email:
        lb["email"] = brief.contact_email
    if city:
        lb["areaServed"] = {"@type": "City", "name": city}
    services = _service_list(brief)
    if services:
        lb["makesOffer"] = [
            {"@type": "Offer", "itemOffered": {"@type": "Service", "name": s}}
            for s in services
        ]

    org = schema_builder.organization(
        name=name, url=public_url, logo=brief.logo_url, description=desc,
        telephone=brief.contact_phone, email=brief.contact_email,
    )

    jsonld = [lb, org]
    script_tag = schema_builder.to_script_tag(jsonld)

    # --- per-page meta ----------------------------------------------------
    page_meta: dict[str, dict[str, str]] = {}
    for page in brief.pages:
        slug = page.slug.lower()
        title, raw_desc = _page_title_desc(slug, name, primary, city, region, page, desc)
        meta_desc = _meta_description(raw_desc, name, primary, city, region)
        page_meta[page.slug] = {
            "title": _clip(title, _TITLE_MAX),
            "description": meta_desc,
            "keywords": ", ".join(keywords),
        }

    return {
        "jsonld": jsonld,
        "jsonld_script": script_tag,
        "page_meta": page_meta,
        "keywords": keywords,
        "robots_txt": "User-agent: *\nAllow: /\nSitemap: {}/sitemap.xml".format(
            public_url.rstrip("/")
        ) if public_url else "User-agent: *\nAllow: /",
        "public_url": public_url,
    }


def _service_list(brief: WebsiteBrief) -> list[str]:
    for page in brief.pages:
        if page.slug.lower().startswith("service"):
            raw = page.content.get("list") or ""
            items = [s.strip() for s in re.split(r"[|,]", raw) if s.strip()]
            if items:
                return items[:8]
    return []


def _page_title_desc(slug, name, primary, city, region, page, fallback_desc):
    loc = f"{city}, {region}".strip(", ")
    body = _sanitize(
        page.content.get("subheadline") or page.content.get("intro")
        or page.content.get("body") or fallback_desc
    )
    if slug == "home":
        return f"{primary} in {loc} | {name}" if loc else f"{primary} | {name}", body
    if slug.startswith("about"):
        return f"About {name} | {loc}".rstrip(" |"), body
    if slug.startswith("service"):
        return f"{name} Services | {primary} in {loc}".rstrip(" |"), body
    if slug.startswith("contact"):
        return f"Contact {name} | {primary} {loc}".rstrip(" |"), body
    return f"{page.title} | {name}", body


# ---------------------------------------------------------------------------
# gate 1 — deterministic pre-publish check on the generated package
# ---------------------------------------------------------------------------

def check_seo_package(package: dict[str, Any]) -> list[str]:
    """Return a list of SEO completeness violations (empty == passes)."""
    problems: list[str] = []
    meta = package.get("page_meta") or {}
    if not meta:
        problems.append("no per-page SEO meta generated")
    for slug, m in meta.items():
        title, desc = m.get("title", ""), m.get("description", "")
        if not title:
            problems.append(f"{slug}: missing title")
        elif len(title) > _TITLE_MAX:
            problems.append(f"{slug}: title {len(title)}>{_TITLE_MAX} chars")
        if not desc:
            problems.append(f"{slug}: missing meta description")
        elif not (_DESC_MIN <= len(desc) <= _DESC_MAX):
            problems.append(f"{slug}: meta description {len(desc)} chars (want {_DESC_MIN}-{_DESC_MAX})")
        if _DASH_RE.search(title) or _DASH_RE.search(desc):
            problems.append(f"{slug}: banned dash in SEO copy")

    jsonld = package.get("jsonld") or []
    lb = next((j for j in jsonld if j.get("@type") == "LocalBusiness"), None)
    if lb is None:
        problems.append("missing LocalBusiness JSON-LD")
    else:
        for field in ("name", "telephone"):
            if not lb.get(field):
                problems.append(f"LocalBusiness JSON-LD missing {field}")
        if not lb.get("address"):
            problems.append("LocalBusiness JSON-LD missing address")
    return problems


# ---------------------------------------------------------------------------
# gate 2 — live audit against the published URL
# ---------------------------------------------------------------------------

def _grade_ok(grade: str, floor: str) -> bool:
    try:
        return _GRADE_ORDER.index(grade) >= _GRADE_ORDER.index(floor)
    except ValueError:
        return False


def evaluate_live(public_url: str, *, keywords: list[str] | None = None, industry: str = "") -> dict[str, Any]:
    """Run the live SEO + passive security audit. Returns the gate scalars.

    Never raises — a network/audit failure returns ``ok=False`` with a reason so
    the deliver gate parks (fail-closed) rather than crashing the build.
    """
    from backend.seo.audit import audit_url

    try:
        result = audit_url(public_url, keywords or [], industry)
    except Exception as exc:  # noqa: BLE001 — fail-closed: gate parks
        _LOG.warning("live seo audit failed url=%s err=%s", public_url, exc)
        return {"ok": False, "reason": f"audit_failed:{type(exc).__name__}", "url": public_url}

    sec = (result.findings or {}).get("security") or {}
    sec_findings = sec.get("findings") or []
    critical = [f for f in sec_findings if isinstance(f, dict) and f.get("severity") == "critical"]
    return {
        "ok": True,
        "url": public_url,
        "seo_score": result.seo_score,
        "seo_issue_count": len(result.issues),
        "security_grade": sec.get("grade", ""),
        "infrastructure_health": sec.get("infrastructure_health", {}),
        "critical_findings": [f.get("id") for f in critical],
    }


def live_gate_violations(
    live: dict[str, Any], *, seo_min_score: int, security_min_grade: str,
    seo_gate: str, security_gate: str,
) -> list[str]:
    """Translate a live-audit result into blocking violations for the given posture."""
    problems: list[str] = []
    if not live.get("ok"):
        return [f"live audit unavailable ({live.get('reason','unknown')})"]
    if seo_gate == "enforce" and live.get("seo_score", 0) < seo_min_score:
        problems.append(f"seo_score {live.get('seo_score')}<{seo_min_score}")
    if security_gate == "enforce":
        grade = live.get("security_grade", "")
        if not grade or not _grade_ok(grade, security_min_grade):
            problems.append(f"security grade {grade or '?'}<{security_min_grade}")
        if live.get("critical_findings"):
            problems.append("critical security findings: " + ", ".join(live["critical_findings"]))
    return problems


__all__ = [
    "build_seo_package",
    "check_seo_package",
    "evaluate_live",
    "live_gate_violations",
]
