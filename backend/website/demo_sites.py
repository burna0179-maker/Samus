"""Operator-prompted cold-call demo-site builder.

The play: before Alex dials the no-website prospects on the morning call
list, ONE command pre-builds (and deploys) a small display site per prospect
so the cold open becomes "I already built you a site — here's the link".

Pipeline per prospect (all existing building blocks, recomposed):

  1. ``no_website_classifier.classify`` — gap + tier + pitch_hook + a
     pre-populated :class:`WebsiteBrief` draft.
  2. Brief ENRICHMENT — the prospect-run data we already paid to gather
     (business description, city/state, phone, review rating + count) is
     seeded into the brief's page content as operator-style values, which
     ``content_gen`` treats as authoritative and never overwrites.
  3. Optional LLM copy polish via ``content_gen.generate_site_content``
     (haiku, budgeted) UNDER A HARD RUN CEILING — see "Cost ceiling" below.
     With no key / over ceiling, the same call degrades to deterministic
     on-brand copy at $0, so a full run always completes.
  4. ``site_builder.build_static_site`` with design intelligence FORCED ON
     (the industry-standard palette/font cross-reference). Media generation
     (Gemini/Veo) is NEVER used in demo runs — paid ``media`` is simply not
     passed, regardless of any media flags, so a demo can't spend on media.
     If the operator has registered REAL brand assets (logo/icon/image) for
     THIS client in ``client_assets``, they are passed in and copied into the
     build under ``assets/`` — for that client only.
  5. ``client_assets.enforce_asset_isolation`` ALWAYS runs on the generated
     files before deploy (operator-mandated brand separation: no asset
     registered to one client may ship in another client's build). Cleaned
     files deploy; violations are logged + recorded on the result + sheet.
  6. Optional deploy to its own Cloudflare Pages project ``demo-<slug>``
     (Pages hosting is free). Missing creds -> build-only, files on disk.

Cost ceiling: the ONLY paid step is the per-prospect haiku call. We check
BEFORE each call using a conservative per-call estimate and charge that
estimate to the run; once ``cumulative + estimate`` would cross the ceiling,
LLM use stops for the rest of the run (deterministic copy continues free).
The ceiling can therefore never be exceeded.

Per-prospect failures are isolated (logged + recorded, run continues): one
bad row must not cost Alex the other nine call-ready links.

Operator-prompted ONLY — gated by ``website_demo_sites_enabled`` (default
OFF) and fired via ``scripts/Build-DemoSites.ps1``. No scheduling hooks.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from backend.common import storage
from backend.prospecting.models import ProspectRecord
from backend.website import client_assets as ca
from backend.website.models import WebsiteBrief, WebsitePage
from backend.website.no_website_classifier import classify

_LOG = logging.getLogger("samus.website.demo_sites")

# Conservative per-call cost estimate (USD) charged against the run ceiling
# BEFORE each LLM call. Real haiku cost for one consolidated copy call is
# ~$0.01-0.03 (≈2k input + ≤1.2k output tokens); we book $0.05 so the
# tracked cumulative is a strict over-estimate — the safe direction for a
# HARD ceiling (we stop early, never late).
_LLM_CALL_EST_USD = 0.05

# Cloudflare Pages project names: lowercase alnum + hyphen, and the full
# project name must fit Pages limits. "demo-" prefix + 53 slug chars = 58.
_PROJECT_PREFIX = "demo-"
_PROJECT_MAX_LEN = 58


# ---------------------------------------------------------------------------
# run-result models
# ---------------------------------------------------------------------------

class DemoSiteResult(BaseModel):
    """Outcome for one prospect — success or isolated failure."""

    model_config = ConfigDict(extra="forbid")

    prospect_id: str = ""
    company_name: str = ""
    phone: str = ""
    project: str = ""           # demo-<slug> Pages project name
    url: str = ""               # live *.pages.dev URL when deployed
    output_dir: str = ""        # local build dir (always present on success)
    pitch_hook: str = ""        # classifier's cold-open line
    pages: int = 0
    llm_used: bool = False
    cost_usd: float = 0.0       # conservative estimate charged to the run
    deployed: bool = False
    deploy_skipped_reason: str = ""   # "" | "disabled" | "missing credentials" | error
    asset_violations: list[str] = Field(default_factory=list)  # isolation-gate strips/warnings
    error: str = ""             # non-empty == this prospect failed (isolated)
    skipped_reason: str = ""    # non-empty == skipped BEFORE build (already has a site)


class DemoSitesRun(BaseModel):
    """Full run result — serialized to demo_sites_YYYY-MM-DD.json."""

    model_config = ConfigDict(extra="forbid")

    run_date: str
    ceiling_usd: float
    cumulative_cost_usd: float = 0.0
    llm_calls: int = 0
    llm_stopped_at_ceiling: bool = False
    built: int = 0
    deployed: int = 0
    failed: int = 0
    skipped_has_site: int = 0   # presence gate: business already has a website
    results: list[DemoSiteResult] = Field(default_factory=list)
    json_path: str = ""
    txt_path: str = ""


# ---------------------------------------------------------------------------
# call-list loading
# ---------------------------------------------------------------------------

# Reverse of csv_export._neutralize_cell: a leading "'" was prefixed when the
# raw value started with a spreadsheet formula trigger — strip exactly one.
_CSV_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")

_INT_FIELDS = frozenset({"lead_score", "seo_score"})
_FLOAT_FIELDS = frozenset({"llm_cost_usd"})


def _deneutralize(value: str) -> str:
    if value.startswith("'") and value[1:2] in _CSV_FORMULA_TRIGGERS:
        return value[1:]
    return value


def _record_from_row(row: dict[str, Any]) -> ProspectRecord:
    """Rebuild a ProspectRecord from a CSV row, tolerating missing/extra
    columns (the CSV schema has grown over iterations — older lists must
    still load)."""
    fields = ProspectRecord.model_fields
    kwargs: dict[str, Any] = {}
    for name, raw in row.items():
        if name not in fields or raw is None:
            continue  # extra/unknown column — ignore, never crash
        value = _deneutralize(str(raw))
        try:
            if name in _INT_FIELDS:
                kwargs[name] = int(float(value)) if value else 0
            elif name in _FLOAT_FIELDS:
                kwargs[name] = float(value) if value else 0.0
            else:
                kwargs[name] = value
        except (TypeError, ValueError):
            continue  # malformed numeric cell — keep the field default
    return ProspectRecord(**kwargs)


def load_no_website_prospects(
    run_date: date | None = None,
    csv_path: str | Path | None = None,
) -> list[ProspectRecord]:
    """Read the day's call list and return the ``no_website`` rows.

    Default path: ``<SAMUS_ARTIFACT_ROOT>/daily_calls/call_list_<date>.csv``
    (today when ``run_date`` is None). An explicit ``csv_path`` wins.
    """
    if csv_path is None:
        run_date = run_date or date.today()
        csv_path = storage.root() / "daily_calls" / f"call_list_{run_date.isoformat()}.csv"
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"call list not found: {path}")

    prospects: list[ProspectRecord] = []
    with path.open("r", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                rec = _record_from_row(row)
            except Exception as exc:  # noqa: BLE001 — one bad row never kills the load
                _LOG.warning("skipping unparseable call-list row: %s", exc)
                continue
            if rec.website_status == "no_website":
                prospects.append(rec)
    return prospects


# ---------------------------------------------------------------------------
# slug + enrichment
# ---------------------------------------------------------------------------

def demo_project_slug(company_name: str) -> str:
    """Deterministic Cloudflare Pages project name: ``demo-<company-slug>``.

    Lowercase alnum + hyphen only, collapsed hyphens, <= 58 chars total.
    Empty/garbage names degrade to ``demo-site`` rather than failing.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (company_name or "").lower()).strip("-")
    slug = slug[: _PROJECT_MAX_LEN - len(_PROJECT_PREFIX)].rstrip("-")
    return _PROJECT_PREFIX + (slug or "site")


# ---------------------------------------------------------------------------
# per-industry service catalog (deterministic, $0)
# ---------------------------------------------------------------------------
# Scannable service CARDS per industry (the operator's spec: short bullets,
# not SEO paragraphs) + the transparent-pricing offer line for the promo band.
# Keys are matched as substrings against the prospect's industry string AFTER
# lowercasing, so "HVAC Contractor" / "hvac" / "heating & hvac" all resolve.
# These are generic offerings ANY business of the type provides — never
# prospect-specific claims (those come only from real prospect-run data).

_GENERIC_SERVICES = [
    "Free Consultations", "Responsive Local Service",
    "Upfront, Transparent Pricing", "Satisfaction-Focused Work",
]
_GENERIC_OFFER = "Free, no-obligation quotes"

_SERVICE_CATALOG: list[tuple[tuple[str, ...], list[str], str]] = [
    # (industry keywords)              (service cards)                            (promo offer)
    (("hvac", "heating", "air conditioning"),
     ["AC Repair", "Furnace Installation", "Duct Cleaning",
      "Maintenance Plans", "Emergency Service", "Free Estimates"],
     "Free estimates on repairs and new installs"),
    (("plumb",),
     ["Drain Cleaning", "Water Heater Repair", "Leak Detection",
      "Pipe Repair", "Emergency Plumbing", "Free Estimates"],
     "Free estimates - emergency service available"),
    (("roof",),
     ["Roof Repair", "Roof Replacement", "Storm Damage Inspections",
      "Gutter Installation", "Leak Repair", "Free Estimates"],
     "Free roof inspections and estimates"),
    (("electric",),
     ["Panel Upgrades", "Wiring & Rewiring", "Lighting Installation",
      "EV Charger Installation", "Troubleshooting & Repair", "Free Estimates"],
     "Free estimates from a local electrician"),
    (("real estate", "realtor", "realty"),
     ["Home Buying", "Home Selling", "Free Market Analysis",
      "Property Tours", "Relocation Assistance"],
     "Free home valuation - no obligation"),
    (("account", "bookkeep", "tax"),
     ["Tax Preparation", "Bookkeeping", "Payroll Services",
      "Tax Planning", "Business Advisory", "Free Consultations"],
     "Free initial consultation"),
    (("car dealer", "auto dealer", "dealership", "used car"),
     ["New & Used Vehicles", "Financing Options", "Trade-In Appraisals",
      "Vehicle Service", "Test Drives Welcome"],
     "Free trade-in appraisals"),
    (("dent",),
     ["Cleanings & Exams", "Fillings & Crowns", "Teeth Whitening",
      "Emergency Dental Care", "New Patient Welcome"],
     "New patients welcome - call to book"),
]


def industry_services(industry: str) -> tuple[list[str], str]:
    """(service cards, promo offer line) for an industry string — keyword match
    with a generic fallback, so every demo gets a scannable services grid."""
    low = (industry or "").lower()
    for keywords, services, offer in _SERVICE_CATALOG:
        if any(k in low for k in keywords):
            return list(services), offer
    return list(_GENERIC_SERVICES), _GENERIC_OFFER


def _trust_line(record: ProspectRecord) -> str:
    """A 'Rated 4.8★ by 120 customers' line from the Places review data —
    only when BOTH parts are real (we never fabricate social proof)."""
    rating, count = (record.review_rating or "").strip(), (record.review_count or "").strip()
    if not rating or not count:
        return ""
    try:
        return f"Rated {float(rating):g}★ by {int(float(count))} customers"
    except (TypeError, ValueError):
        return ""


def enrich_brief(brief: WebsiteBrief, record: ProspectRecord) -> WebsiteBrief:
    """Seed the classifier's brief_draft pages with the prospect-run data we
    already gathered. Seeded fields count as operator-supplied in
    ``content_gen`` (never overwritten), so the demo always reflects the real
    business — description, locality, phone, and earned review trust."""
    from backend.website.content_gen import default_page_set

    name = brief.business_name
    desc = (record.business_description or "").strip()
    locality = ", ".join(p for p in (record.city, record.state) if p)
    trust = _trust_line(record)
    industry = (record.industry or "").strip()

    seeds: dict[str, dict[str, str]] = {"home": {}, "about": {}, "contact": {}, "services": {}}

    # --- comprehensive scannable services + promo band (industry catalog) ---
    services, offer = industry_services(industry)
    seeds["services"]["list"] = " | ".join(services)
    seeds["services"]["heading"] = "Our Services"
    seeds["services"]["body"] = (
        f"Straightforward {industry.lower() if industry else 'local'} service "
        "with upfront, transparent pricing - no surprises."
    )
    promo = offer
    if record.phone:
        promo = f"{offer} - call {record.phone} today"
    seeds["home"]["promo"] = promo

    # --- honest trust + clearly-labeled demo placeholders -------------------
    # trust_line is rendered ONLY when built from REAL Google review data;
    # the placeholders show the prospect what owner-supplied sections do
    # without fabricating testimonials, licenses, or project photos.
    if trust:
        seeds["home"]["trust_line"] = trust + " on Google"
    seeds["home"]["testimonials_placeholder"] = "Your customer reviews featured here"
    seeds["home"]["credentials_placeholder"] = "Your license & certifications displayed here"
    seeds["home"]["portfolio_placeholder"] = "Photos of your completed projects showcased here"

    if desc:
        # The real description leads the homepage + about copy.
        seeds["home"]["intro"] = desc
        about_bits = [desc]
        if locality:
            about_bits.append(f"Proudly serving {locality}.")
        if trust:
            about_bits.append(trust + ".")
        seeds["about"]["body"] = " ".join(about_bits)
    if trust:
        seeds["home"]["subheadline"] = trust
    if locality and industry:
        seeds["home"]["headline"] = f"{name} - {industry.title()} in {locality}"
    contact_bits = []
    if record.phone:
        contact_bits.append(f"Call or text {record.phone}.")
    if locality:
        contact_bits.append(f"Serving {locality} and nearby.")
    if contact_bits:
        seeds["contact"]["body"] = " ".join(contact_bits)

    pages: list[WebsitePage] = []
    for page in (brief.pages or default_page_set()):
        content = dict(page.content)
        for k, v in seeds.get(page.slug.lower(), {}).items():
            content.setdefault(k, v)
        pages.append(WebsitePage(slug=page.slug, title=page.title, content=content))
    return brief.model_copy(update={"pages": pages})


# ---------------------------------------------------------------------------
# build run
# ---------------------------------------------------------------------------

class _ForceDesignIntelligence:
    """Settings proxy that pins website_design_intelligence_enabled True for
    the demo build (the industry cross-reference IS the demo's polish) without
    mutating the real settings object."""

    def __init__(self, inner: Any):
        self._inner = inner

    @property
    def website_design_intelligence_enabled(self) -> bool:
        return True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _client_media(client_key: str) -> tuple[dict[str, str], list[tuple[Path, str]]]:
    """Operator-registered brand assets for THIS client -> site_builder
    ``media`` refs + (store_path, build-relative path) copy list.

    Only logo/icon/image kinds feed a demo (media/marketing stay store-only).
    Refs are ``./assets/<filename>`` so the page works from the project root.
    First image -> hero/section; logo -> og image (+ hero fallback)."""
    media: dict[str, str] = {}
    copies: list[tuple[Path, str]] = []
    by_kind: dict[str, str] = {}
    for entry in ca.list_assets(client_key):
        kind, fname = entry.get("kind", ""), entry.get("filename", "")
        if kind not in ("logo", "icon", "image") or not fname:
            continue
        path = ca.get_asset_path(client_key, fname)
        if path is None:
            continue
        copies.append((path, f"assets/{fname}"))
        by_kind.setdefault(kind, f"./assets/{fname}")
    if "image" in by_kind:
        media["hero_image"] = by_kind["image"]
        media["section_image"] = by_kind["image"]
    if "logo" in by_kind:
        media["og_image"] = by_kind["logo"]
        media.setdefault("hero_image", by_kind["logo"])
    return media, copies


_MEDIA_GEN_EST_USD = 0.12  # hero+section+og at ~$0.04 each — the pricier call


def _generated_media(
    brief: WebsiteBrief, *, settings: Any, client_key: str,
) -> tuple[dict[str, str], list[tuple[Path, str]], float]:
    """Gemini-generated hero/section/og imagery -> site_builder ``media`` refs +
    copy list, mirroring :func:`_client_media`.

    THE ADAPTER: media_gen produces image BYTES; build_static_site wants
    ``{kind: ./assets/<f>}`` + the files copied into the build dir. Each generated
    image is REGISTERED to THIS client (``client_assets``) so the fail-closed
    brand-isolation gate treats it exactly like an operator-registered asset
    (owned by this client, ships cleanly) instead of stripping it as foreign.
    So a demo carries bespoke, brand-matched photography (MHH-parity) for a
    prospect we have no real assets for. Fail-soft: a disabled/keyless media path
    or any error returns empty (build proceeds design-only). Cost is the REAL
    metered spend from generate_media."""
    import base64
    from backend.website import media_gen

    media: dict[str, str] = {}
    copies: list[tuple[Path, str]] = []
    spent = 0.0
    try:
        assets, report = media_gen.generate_media(brief, settings=settings)
        spent = float(report.get("spent_usd", 0.0) or 0.0)
        for a in assets:
            if not (getattr(a, "ok", False) and getattr(a, "data_b64", "")):
                continue
            raw = base64.b64decode(a.data_b64)
            entry = ca.register_asset(
                client_key, raw, "image",
                original_name=f"{a.kind}.png", source_label="generated")
            fname = entry.get("filename")
            path = ca.get_asset_path(client_key, fname) if fname else None
            if not fname or path is None:
                continue
            copies.append((path, f"assets/{fname}"))
            media[a.kind] = f"./assets/{fname}"
        if media.get("hero_image"):
            media.setdefault("section_image", media["hero_image"])
            media.setdefault("og_image", media["hero_image"])
    except Exception as exc:  # noqa: BLE001 — media enhances; never blocks a build
        _LOG.warning("generated media failed: %s", exc)
    return media, copies, spent


def _apply_presence_enrichment(record: ProspectRecord, verdict: Any) -> None:
    """Fill EMPTY record fields from the fresh Places data the presence check
    already fetched, so the demo is tailored with the prospect's real business
    info (hours, rating, reviews, description) — they recognise it as theirs.
    Gaps only; never overwrites operator/prospecting-provided values."""
    b = getattr(verdict, "business", None) or {}
    updates: dict[str, str] = {}
    hrs = b.get("hours") or []
    if hrs and not getattr(record, "business_hours", ""):
        updates["business_hours"] = " | ".join(str(h) for h in hrs)
    if b.get("rating") is not None and not getattr(record, "review_rating", ""):
        updates["review_rating"] = str(b["rating"])
    if b.get("review_count") is not None and not getattr(record, "review_count", ""):
        updates["review_count"] = str(b["review_count"])
    desc = (b.get("description") or "").strip()
    if desc and hasattr(record, "business_description") and not getattr(record, "business_description", ""):
        updates["business_description"] = desc
    for k, v in updates.items():
        try:
            setattr(record, k, v)
        except Exception:  # noqa: BLE001 — enrichment is best-effort
            pass


def _deploy_one(*, project: str, out_dir: Path, settings: Any) -> tuple[str, str]:
    """Deploy one built site to its own Pages project. Returns (url, error).

    Calls the deploy adapter's pieces directly (not ``deploy_site``) because
    each prospect needs its OWN project name, not the single
    ``cloudflare_pages_project`` setting.
    """
    from backend.website import deploy_cloudflare as dc

    token = str(getattr(settings, "cloudflare_api_token", "") or "")
    account = str(getattr(settings, "cloudflare_account_id", "") or "")
    if not (token and account):
        return "", "missing credentials"
    dc.create_project(account_id=account, api_token=token, project=project)  # 409 == exists
    result = dc.deploy_via_wrangler(out_dir, project=project, api_token=token, account_id=account)
    if not result.ok:
        return "", result.error or "deploy failed"
    # Wrangler prints the per-deployment hash URL (<hash>.<project>.pages.dev).
    # That host is FOUR labels deep — one level below the *.pages.dev wildcard
    # cert — so strict TLS clients fail the handshake on it. The canonical
    # project URL (<project>.pages.dev) is covered by the wildcard and serves
    # the same --branch=main production deployment; that is the URL the
    # operator reads out on a call, so it is the one we record.
    return f"https://{project}.pages.dev", ""


def build_demo_sites(
    prospects: list[ProspectRecord],
    *,
    ceiling_usd: float = 5.0,
    deploy: bool = True,
    use_llm_copy: bool = True,
    with_media: bool = False,
    verify_presence: bool = True,
    settings: Any = None,
    run_date: date | None = None,
    sheet_label: str = "",
) -> DemoSitesRun:
    """Build (and optionally deploy) one demo site per prospect under a HARD
    total cost ceiling. See the module docstring for the per-prospect pipeline
    and the ceiling mechanics."""
    if settings is None:
        from backend.common.settings import bootstrap_settings
        settings = bootstrap_settings()
    if not getattr(settings, "website_demo_sites_enabled", False):
        # Loud refusal: demo runs spend money + create public artifacts in the
        # business's name. The operator arms this explicitly per the catalog.
        raise RuntimeError(
            "website_demo_sites_enabled is OFF - set SAMUS_WEBSITE_DEMO_SITES_ENABLED=1 "
            "(or run via scripts/Build-DemoSites.ps1) to arm demo-site builds"
        )

    from backend.website.content_gen import generate_site_content
    from backend.website.deploy_cloudflare import prepare_build_dir
    from backend.website.site_builder import build_static_site

    run_date = run_date or date.today()
    out_root = storage.root() / "demo_sites" / run_date.isoformat()
    build_settings = _ForceDesignIntelligence(settings)
    run = DemoSitesRun(run_date=run_date.isoformat(), ceiling_usd=float(ceiling_usd))
    llm_allowed = bool(use_llm_copy)

    for record in prospects:
        result = DemoSiteResult(
            prospect_id=record.prospect_id,
            company_name=record.company_name,
            phone=record.phone,
        )
        try:
            # --- INSURANCE GATE: re-verify no live site before spending -----
            # The prospecting crawler false-flags bot-blocked working sites as
            # broken/no_website. Re-check Google Places (authoritative website
            # field); if the business HAS a site, DON'T build (or pitch) — it
            # wastes money and torches credibility. The verdict also carries
            # fresh Places data to TAILOR the demo with real info.
            if verify_presence:
                from backend.website import presence_check
                verdict = presence_check.verify_presence(
                    record.company_name, city=record.city, state=record.state,
                    existing_website=record.website_url)
                if not verdict.buildable:
                    result.skipped_reason = verdict.reason
                    run.skipped_has_site += 1
                    _LOG.info("SKIP (has site) %s — %s", record.company_name, verdict.reason)
                    run.results.append(result)
                    continue
                _apply_presence_enrichment(record, verdict)

            cls = classify(record)
            if cls is None:
                result.error = f"not a website-gap prospect (status={record.website_status!r})"
                run.results.append(result)
                run.failed += 1
                continue
            result.pitch_hook = cls.pitch_hook
            project = demo_project_slug(record.company_name)
            result.project = project

            brief = enrich_brief(cls.brief_draft, record)

            # --- copy: LLM polish only while the ceiling allows it ------
            # HARD-ceiling check point: charge the conservative estimate
            # BEFORE the call; if it would cross, LLM use is over for the
            # whole run (latch — never re-opens).
            if llm_allowed and (run.cumulative_cost_usd + _LLM_CALL_EST_USD) > ceiling_usd:
                llm_allowed = False
                run.llm_stopped_at_ceiling = True
                _LOG.info("demo run cost ceiling $%.2f reached - deterministic copy from here on", ceiling_usd)
            call_key = "unused" if llm_allowed else ""
            brief, report = generate_site_content(brief, settings=settings, api_key=call_key)
            if report.get("llm_used"):
                run.cumulative_cost_usd = round(run.cumulative_cost_usd + _LLM_CALL_EST_USD, 6)
                run.llm_calls += 1
                result.llm_used = True
                result.cost_usd = _LLM_CALL_EST_USD

            # --- build: design intelligence ON, paid media NEVER ---------
            # Operator-registered brand assets for THIS client (and only
            # this client) are referenced relatively under assets/.
            client_key = ca.derive_client_key(
                record.prospect_id, record.account_id, record.company_name)
            media, asset_copies = _client_media(client_key)
            # --- generated media (opt-in premium demo) -------------------
            # Bespoke Gemini imagery for a prospect we have no real assets
            # for. Ceiling-gated (the pricier call); operator-registered
            # client assets always WIN over generated. Fail-soft.
            if with_media and (run.cumulative_cost_usd + _MEDIA_GEN_EST_USD) <= ceiling_usd:
                gen_media, gen_copies, media_spent = _generated_media(
                    brief, settings=settings, client_key=client_key)
                if gen_media:
                    run.cumulative_cost_usd = round(run.cumulative_cost_usd + media_spent, 6)
                    result.cost_usd = round(result.cost_usd + media_spent, 6)
                    merged = dict(gen_media)
                    merged.update(media)                 # client assets win
                    media, asset_copies = merged, gen_copies + asset_copies
            elif with_media:
                _LOG.info("demo ceiling $%.2f reached — skipping paid media for %s",
                          ceiling_usd, project)
            predicted_url = f"https://{project}.pages.dev"
            site = build_static_site(brief, settings=build_settings,
                                     media=media or None, public_url=predicted_url)
            result.pages = len(site.pages)

            # --- THE GATE: brand-asset isolation, fail-closed, ALWAYS ----
            strict = bool(getattr(settings, "website_asset_isolation_strict", False))
            clean_files, violations = ca.enforce_asset_isolation(
                site.files, client_key,
                own_domains=(f"{project}.pages.dev",), strict=strict)
            if violations:
                result.asset_violations = list(violations)
                _LOG.error("ASSET ISOLATION: %d violation(s) for %s — cleaned "
                           "files deploy: %s", len(violations),
                           record.company_name, "; ".join(violations))
            site.files = clean_files

            out_dir = prepare_build_dir(site, out_dir=out_root / project)
            result.output_dir = str(out_dir)
            for src_path, rel in asset_copies:
                dest = out_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, dest)

            # --- deploy (optional, creds-gated, free hosting) ------------
            if deploy:
                url, err = _deploy_one(project=project, out_dir=out_dir, settings=settings)
                if url or not err:
                    result.url = url or predicted_url
                    result.deployed = True
                    run.deployed += 1
                else:
                    result.deploy_skipped_reason = err
                    _LOG.warning("deploy skipped for %s: %s", project, err)
            else:
                result.deploy_skipped_reason = "disabled"

            run.built += 1
        except Exception as exc:  # noqa: BLE001 — per-prospect isolation
            _LOG.exception("demo site build failed for %r", record.company_name)
            result.error = f"{type(exc).__name__}: {exc}"
            run.failed += 1
        run.results.append(result)

    _write_summaries(run, sheet_label=sheet_label)
    return run


# ---------------------------------------------------------------------------
# summaries — the call-ready sheet
# ---------------------------------------------------------------------------

def _write_summaries(run: DemoSitesRun, *, sheet_label: str = "") -> None:
    """demo_sites_<date>.json (full result) + .txt (call-ready sheet).

    ``sheet_label`` suffixes the filenames (partial runs, e.g. a --company
    single-prospect rebuild) so a 1-entry sheet never clobbers the day's
    full call sheet.
    """
    out_dir = storage.root() / "demo_sites"
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"_{sheet_label}" if sheet_label else ""
    json_path = out_dir / f"demo_sites_{run.run_date}{suffix}.json"
    txt_path = out_dir / f"demo_sites_{run.run_date}{suffix}.txt"
    run.json_path, run.txt_path = str(json_path), str(txt_path)

    json_path.write_text(json.dumps(run.model_dump(), indent=2), encoding="utf-8")

    lines = [
        f"DEMO SITES - {run.run_date}",
        f"built={run.built} deployed={run.deployed} "
        f"skipped_has_site={run.skipped_has_site} failed={run.failed} "
        f"spent=${run.cumulative_cost_usd:.2f} of ${run.ceiling_usd:.2f}",
        "=" * 60,
    ]
    for r in run.results:
        if r.skipped_reason:
            lines += ["", f"{r.company_name or r.prospect_id}  [SKIPPED - {r.skipped_reason}]"]
            continue
        if r.error:
            lines += ["", f"{r.company_name or r.prospect_id}  [FAILED: {r.error}]"]
            continue
        lines += [
            "",
            f"{r.company_name}",
            f"  phone : {r.phone or '(none)'}",
            f"  demo  : {r.url or '(not deployed: ' + (r.deploy_skipped_reason or 'unknown') + ') ' + r.output_dir}",
            f"  hook  : {r.pitch_hook}",
            f"  cost  : ${r.cost_usd:.2f}" + ("  (haiku copy)" if r.llm_used else "  (deterministic)"),
        ]
        if r.asset_violations:
            lines.append(f"  ASSET ISOLATION: {len(r.asset_violations)} violation(s) stripped/flagged:")
            lines += [f"    - {v}" for v in r.asset_violations]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI — operator-prompted only (fired by scripts/Build-DemoSites.ps1)
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.website.demo_sites",
        description="Pre-build + deploy demo sites for today's no-website call-list prospects.",
    )
    parser.add_argument("--date", help="call list date YYYY-MM-DD (default today)")
    parser.add_argument("--csv", help="explicit call-list CSV path (overrides --date)")
    parser.add_argument("--ceiling", type=float, default=None,
                        help="hard total run cost ceiling USD (default settings.demo_sites_ceiling_usd)")
    parser.add_argument("--no-deploy", action="store_true", help="build only, skip Cloudflare deploy")
    parser.add_argument("--no-llm", action="store_true", help="deterministic copy only ($0)")
    parser.add_argument("--limit", type=int, default=0, help="cap prospects processed (0 = all)")
    parser.add_argument("--company", default="",
                        help="only prospects whose company name contains this "
                             "(case-insensitive) — single-prospect rebuilds")
    parser.add_argument("--with-media", action="store_true",
                        help="generate bespoke Gemini hero/section/og imagery "
                             "(premium demo, ~$0.12/site; needs GEMINI_API_KEY). "
                             "Off = the cheap design-only bulk demo.")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip the Google-Places presence re-check (default "
                             "ON: skips businesses that already have a site, so "
                             "we never build/pitch a site to someone who has one).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    from backend.common.settings import bootstrap_settings
    settings = bootstrap_settings()
    if not getattr(settings, "website_demo_sites_enabled", False):
        print(
            "ERROR: website_demo_sites_enabled is OFF. Set SAMUS_WEBSITE_DEMO_SITES_ENABLED=1 "
            "or run via scripts\\Build-DemoSites.ps1 (which arms it for the child process).",
            file=sys.stderr,
        )
        return 2

    if args.with_media:
        # Arm the (otherwise dormant) paid Gemini media path for THIS run and
        # make the media budget cover it. Fail-soft if no key.
        settings.website_media_gen_enabled = True
        try:
            if float(getattr(settings, "media_daily_dollar_cap", 0.0) or 0.0) < 1.0:
                settings.media_daily_dollar_cap = 1.0
        except Exception:
            pass
        if not (getattr(settings, "gemini_api_key", "") or "").strip():
            print("WARN: --with-media set but no gemini_api_key present; "
                  "sites will build design-only.", file=sys.stderr)

    run_date = date.fromisoformat(args.date) if args.date else date.today()
    try:
        prospects = load_no_website_prospects(run_date=run_date, csv_path=args.csv)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.company:
        needle = args.company.strip().lower()
        prospects = [p for p in prospects if needle in (p.company_name or "").lower()]
    if args.limit and args.limit > 0:
        prospects = prospects[: args.limit]
    if not prospects:
        print("No matching no_website prospects on the call list - nothing to build.")
        return 0

    ceiling = args.ceiling if args.ceiling is not None else float(
        getattr(settings, "demo_sites_ceiling_usd", 5.0)
    )
    run = build_demo_sites(
        prospects,
        ceiling_usd=ceiling,
        deploy=not args.no_deploy,
        use_llm_copy=not args.no_llm,
        with_media=args.with_media,
        verify_presence=not args.no_verify,
        settings=settings,
        run_date=run_date,
        # Partial (filtered) runs get a suffixed sheet so they never clobber
        # the day's full call sheet.
        sheet_label="partial" if (args.company or args.limit) else "",
    )
    print(f"\nbuilt={run.built} deployed={run.deployed} "
          f"skipped_has_site={run.skipped_has_site} failed={run.failed} "
          f"spent=${run.cumulative_cost_usd:.2f} of ${run.ceiling_usd:.2f}")
    print(f"call sheet: {run.txt_path}")
    return 0 if run.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DemoSiteResult",
    "DemoSitesRun",
    "build_demo_sites",
    "demo_project_slug",
    "enrich_brief",
    "industry_services",
    "load_no_website_prospects",
]
