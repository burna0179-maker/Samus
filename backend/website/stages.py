"""Stage handlers — the leaves of the website-build walk.

Each handler is ``(StageContext) -> StageResult``. The handlers run the leaves
that are *self-contained or account-generic* for real (assemble the brief,
provision the site, set business info, insert CMS content, read it back for QA,
record the settlement). The leaves whose exact shape depends on an operator
choice not yet made — a CMS collection that only exists once a template is
picked, the live publish mechanism, the live flag — *park* honestly (the
established cash_engine idiom: a parked stage is resumable, it does not fake a
result). The walk-through is where those parks get wired against the real,
chosen template.

Every stage re-validates through the Codex (:func:`backend.website.gate.codex_gate`,
fail-closed). The outward stages (publish, deliver) also honour the live flag.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from backend.common.dates import iso_now

from .gate import codex_gate
from .models import WebsiteOrder
from .state import WebsiteBuildState
from .wix_client import WixClient, WixError

_LOG = logging.getLogger("samus.website.stages")


@dataclass
class StageContext:
    """Everything a stage handler needs."""

    state: WebsiteBuildState
    order: WebsiteOrder
    settings: Any
    # The Wix transport, or None when credentials are unseeded (-> stages that
    # need it park). Injectable so tests pass a fake.
    wix: WixClient | None = None
    crm: Any = None
    # Whether the live, outward action is permitted this run (publish/deliver).
    live_publish_enabled: bool = False


@dataclass
class StageResult:
    """Outcome of one stage."""

    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)
    parked: bool = False
    park_reason: str = ""
    codex_blocked: bool = False
    violated_rule_id: str | None = None
    reason: str | None = None
    drafted_adr_path: str | None = None


def _blocked(stage: str, verdict: Any) -> StageResult:
    _LOG.info("website stage %s BLOCKED by Codex rule=%s", stage, verdict.violated_rule_id)
    return StageResult(
        ok=False,
        codex_blocked=True,
        violated_rule_id=verdict.violated_rule_id,
        reason=verdict.reason,
        drafted_adr_path=getattr(verdict, "drafted_adr_path", None),
    )


def _brief_copy(order: WebsiteOrder) -> str:
    """Concatenate the brief's public copy for the banned-phrase (G2) scan."""
    parts = [order.brief.business_name, order.brief.business_description]
    for page in order.brief.pages:
        parts.append(page.title)
        parts.extend(str(v) for v in page.content.values())
    return "\n".join(p for p in parts if p)


# --------------------------------------------------------------------------
# Stage handlers
# --------------------------------------------------------------------------


def _brief_stage(ctx: StageContext) -> StageResult:
    """Confirm/assemble the build spec. Parks if there is nothing to build."""
    brief = ctx.order.brief
    if not brief.pages:
        # With content generation on, a brief that names a business but no pages
        # is enough: seed a standard page skeleton (the `generate` stage fills
        # the copy). Without generation we refuse to publish an empty shell.
        if getattr(ctx.settings, "website_content_generation_enabled", False):
            from .content_gen import default_page_set

            brief.pages = default_page_set()
            ctx.state.log("brief_seeded_default_pages", page_count=len(brief.pages))
        else:
            # Nothing to populate. The operator (supervised) or the intake
            # questionnaire (autonomous) fills this in.
            return StageResult(ok=False, parked=True, park_reason="no_pages_in_brief")

    # G2 banned-phrase scan over everything that will become public copy.
    verdict = codex_gate(
        capability="website_brief",
        payload={"copy": _brief_copy(ctx.order), "business_name": brief.business_name},
    )
    if not verdict.allowed:
        return _blocked("brief", verdict)

    return StageResult(
        ok=True,
        detail={
            "page_count": len(brief.pages),
            "settlement_kind": ctx.order.settlement_kind,
        },
    )


def _generate_stage(ctx: StageContext) -> StageResult:
    """Taste-governed copy + SEO generation — the "frontend codegen".

    Resolves a TasteProfile from the brief and fills every page's copy
    (operator-supplied fields win). Prefers the budgeted Anthropic path; falls
    back to a deterministic on-brand template when there is no key / the per-day
    cap is hit / the call fails — so a site is buildable on a $0 budget. The
    generated copy is taste-audited fail-closed (no em-dashes, no AI tells reach
    the customer) and then Codex-gated. With generation disabled the stage is a
    pass-through (the operator-authored content stands).
    """
    if not getattr(ctx.settings, "website_content_generation_enabled", False):
        return StageResult(ok=True, detail={"content_generation": "disabled"})

    from .content_gen import generate_site_content

    enriched, report = generate_site_content(ctx.order.brief, settings=ctx.settings)
    # Persist the enriched copy onto the order so the content stage inserts it.
    ctx.order.brief = enriched

    # Codex-gate the freshly generated public copy (G2 banned-phrase scan).
    verdict = codex_gate(
        capability="website_generate",
        payload={"copy": _brief_copy(ctx.order), "business_name": enriched.business_name},
    )
    if not verdict.allowed:
        return _blocked("generate", verdict)

    audit = report.get("taste_audit", {})
    return StageResult(
        ok=True,
        detail={
            "generation_report": report,
            "llm_used": report.get("llm_used", False),
            "taste_grade": audit.get("grade"),
            "taste_passed": audit.get("passed"),
            "pages_generated": report.get("pages_generated", []),
        },
    )


def _provision_stage(ctx: StageContext) -> StageResult:
    """Create (or adopt) the Wix site; capture its metaSiteId."""
    brief = ctx.order.brief
    # Adopt an operator-created site if one was supplied — no API call needed.
    if brief.existing_site_id:
        return StageResult(ok=True, detail={"site_id": brief.existing_site_id, "adopted": True})

    if ctx.wix is None:
        # No credentials seeded -> cannot provision. Resumable once the
        # operator seeds wix_api_key + wix_account_id.
        return StageResult(ok=False, parked=True, park_reason="wix_credentials_unset")

    verdict = codex_gate(
        capability="website_provision",
        payload={"business_name": brief.business_name, "template_id": brief.template_id},
    )
    if not verdict.allowed:
        return _blocked("provision", verdict)

    try:
        resp = ctx.wix.create_site(
            brief.business_name,
            template_id=brief.template_id or None,
        )
    except WixError as exc:
        # Auth / transport failure is resumable (fix the key, retry) — not a
        # silent success. Park with the Wix status for the operator.
        return StageResult(
            ok=False,
            parked=True,
            park_reason=f"wix_provision_failed:{exc.status_code or 'transport'}",
        )

    site_id = str(resp.get("metaSiteId") or resp.get("siteId") or "")
    if not site_id:
        return StageResult(ok=False, parked=True, park_reason="wix_provision_no_site_id")
    return StageResult(ok=True, detail={"site_id": site_id, "adopted": False})


def _parse_us_address(raw: str) -> dict[str, Any]:
    """Best-effort parse of "123 Street, City, ST zip" into Wix's address shape.

    Wix v4 address fields: street, streetNumber, city, state, zip, country.
    Leading numeric token of the first part becomes ``streetNumber``; the rest
    is ``street``. Falls back to ``{street, country}`` when the format doesn't
    match, so we never emit malformed structured data.
    """
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    addr: dict[str, Any] = {"country": "US"}
    if parts:
        first = parts[0].split()
        if first and first[0].isdigit():
            addr["streetNumber"] = first[0]
            addr["street"] = " ".join(first[1:]) or parts[0]
        else:
            addr["street"] = parts[0]
    if len(parts) >= 2:
        addr["city"] = parts[1]
    if len(parts) >= 3:
        tail = parts[2].split()  # "OR 97624"
        if tail:
            addr["state"] = tail[0]
        if len(tail) >= 2:
            addr["zip"] = tail[1]
    if len(parts) >= 4 and "zip" not in addr:
        addr["zip"] = parts[3]
    return addr


def _business_info_stage(ctx: StageContext) -> StageResult:
    """Set the site's business identity via the Site Properties v4 API.

    Two purpose-specific POST sub-actions (no PATCH, no fieldMask): the profile
    (display name + description) and the contact card (name, phone, email,
    address). On an unpublished site these are safe drafts — nothing is public
    until the publish stage (which is separately flag-gated).
    Docs: dev.wix.com/.../business-management/site-properties/properties
    """
    if not ctx.state.site_id:
        return StageResult(ok=False, parked=True, park_reason="no_site_id")
    if ctx.wix is None:
        return StageResult(ok=False, parked=True, park_reason="wix_credentials_unset")

    brief = ctx.order.brief
    # Verified-settable v4 fields only. businessName + siteDisplayName are NOT
    # writable via these endpoints ("Fields ... are not allowed") — they are
    # dashboard-only, so the business name is surfaced to the operator as a
    # manual step, never silently dropped.
    profile: dict[str, Any] = {}
    if brief.business_description:
        profile["description"] = brief.business_description

    contact: dict[str, Any] = {}
    if brief.contact_phone:
        contact["phone"] = brief.contact_phone
    if brief.contact_email:
        contact["email"] = brief.contact_email
    if brief.address:
        contact["address"] = _parse_us_address(brief.address)

    verdict = codex_gate(
        capability="website_business_info",
        payload={
            "profile": profile,
            "contact": {k: v for k, v in contact.items() if k != "address"},
        },
    )
    if not verdict.allowed:
        return _blocked("business_info", verdict)

    site_id = ctx.state.site_id
    done: list[str] = []
    try:
        if profile:
            ctx.wix.update_business_profile(site_id=site_id, business_profile=profile)
            done.append("profile")
        if contact:
            ctx.wix.update_business_contact(site_id=site_id, business_contact=contact)
            done.append("contact")
    except WixError as exc:
        # Partial progress (e.g. profile set, contact failed) is durable + safe
        # to retry — the sub-actions are last-writer-wins. Park with the detail.
        return StageResult(
            ok=False,
            parked=True,
            park_reason=f"wix_business_info_failed:{exc.status_code or 'transport'}",
            detail={"business_info_applied": done},
        )
    return StageResult(
        ok=True,
        detail={
            "business_info_applied": done,
            # The business/site name is not settable via the v4 API — flag it for
            # the operator to set in the Wix dashboard (Settings -> Business Info).
            "manual_followup": f"set site/business name to '{brief.business_name}' in dashboard",
        },
    )


def _content_row(order: WebsiteOrder) -> dict[str, str]:
    """Flatten the brief's pages into one CMS row (the template's bind source).

    Convention (matches the SiteContent template schema): each page's content
    key becomes a flat field ``{slug}{KeyCapitalised}`` — e.g. the ``home``
    page's ``headline`` -> ``homeHeadline``, ``services``/``list`` ->
    ``servicesList``. Plus identity: ``ref`` (stable row key) + ``businessName``
    + ``tagline`` (the description). One row drives every bound element on the
    site, so an API update refreshes the whole site's copy.
    """
    brief = order.brief
    row: dict[str, str] = {"ref": "main", "businessName": brief.business_name}
    if brief.business_description:
        row["tagline"] = brief.business_description
    for page in brief.pages:
        for key, val in page.content.items():
            row[f"{page.slug}{key[:1].upper()}{key[1:]}"] = str(val)
    return row


def _content_stage(ctx: StageContext) -> StageResult:
    """Populate the site's CMS row that the template's elements bind to.

    Inserts ONE flat row (see :func:`_content_row`) into the operator-set
    collection (``website_content_collection_id``; the SiteContent template).
    Absent a collection mapping the stage parks — it will not invent one. The
    element/dataset binding to this collection is a one-time Editor step done at
    template setup, not here.
    """
    if not ctx.state.site_id:
        return StageResult(ok=False, parked=True, park_reason="no_site_id")
    if ctx.wix is None:
        return StageResult(ok=False, parked=True, park_reason="wix_credentials_unset")

    collection_id = _content_collection_id(ctx)
    if not collection_id:
        return StageResult(ok=False, parked=True, park_reason="cms_collection_unmapped")

    verdict = codex_gate(
        capability="website_content",
        payload={"copy": _brief_copy(ctx.order), "collection_id": collection_id},
    )
    if not verdict.allowed:
        return _blocked("content", verdict)

    row = _content_row(ctx.order)
    site_id = ctx.state.site_id
    # Idempotent upsert: refresh the existing row in place if one is present
    # (a re-run, or a row seeded at template setup) rather than duplicating —
    # the bound dataset expects a single source row.
    existing_id = _existing_row_id(ctx.wix, site_id, collection_id)
    try:
        if existing_id:
            ctx.wix.update_data_item(
                site_id=site_id, collection_id=collection_id, item_id=existing_id, data=row
            )
            item_id = existing_id
        else:
            resp = ctx.wix.insert_data_item(site_id=site_id, collection_id=collection_id, data=row)
            item = resp.get("dataItem") or {}
            item_id = str(item.get("id") or (item.get("data") or {}).get("_id") or "")
    except WixError as exc:
        return StageResult(
            ok=False,
            parked=True,
            park_reason=f"wix_content_failed:{exc.status_code or 'transport'}",
        )
    return StageResult(
        ok=True,
        detail={
            "content_item_ids": [item_id],
            "content_collection_id": collection_id,
            "content_upserted": "updated" if existing_id else "inserted",
        },
    )


def _existing_row_id(wix: WixClient, site_id: str, collection_id: str) -> str:
    """Return the id of the first row in ``collection_id``, or "" — best-effort."""
    try:
        resp = wix.query_data_items(site_id=site_id, collection_id=collection_id)
    except WixError:
        return ""
    items = resp.get("dataItems") or resp.get("items") or []
    if not items:
        return ""
    first = items[0]
    data = first.get("data") if isinstance(first.get("data"), dict) else first
    return str(first.get("id") or data.get("_id") or data.get("id") or "")


def _content_collection_id(ctx: StageContext) -> str:
    """Resolve the CMS collection the content row goes into (settings-driven)."""
    return str(getattr(ctx.settings, "website_content_collection_id", "") or "")


def _media_stage(ctx: StageContext) -> StageResult:
    """AI visuals (Gemini image / Veo video) -> Wix Media -> CMS refs.

    Best-effort ENHANCEMENT — never blocks delivery (a failed image gen must not
    sink a site). Dormant unless ``website_media_gen_enabled`` AND a
    ``gemini_api_key`` AND budget (paid, metered by media_budget, separate from
    the Anthropic cap). Generates taste-governed visuals, uploads them to the
    site's Media Manager, and safe-merges the wix media refs into the SiteContent
    row so the Velo/CMS binding can place them. Veo video stays gated behind its
    own flag + per-video operator approval inside generate_media.
    """
    if not getattr(ctx.settings, "website_media_gen_enabled", False):
        return StageResult(ok=True, detail={"media": "disabled"})

    from . import media_gen, media_publish

    assets, report = media_gen.generate_media(ctx.order.brief, settings=ctx.settings)
    if not assets:
        # disabled / no key / budget-denied -> nothing generated, nothing spent.
        return StageResult(ok=True, detail={"media_report": report})

    # Same Codex discipline as every other stage (benign generated prompts).
    verdict = codex_gate(
        capability="website_media",
        payload={"prompts": [a.prompt for a in assets]},
    )
    if not verdict.allowed:
        return _blocked("media", verdict)

    # Upload to Wix Media + merge refs into the CMS row (best-effort).
    if ctx.wix is not None and ctx.state.site_id:
        collection_id = _content_collection_id(ctx)
        try:
            report["publish"] = media_publish.publish_assets(
                assets,
                wix=ctx.wix,
                site_id=ctx.state.site_id,
                collection_id=collection_id,
                update_cms=bool(collection_id),
            )
        except Exception as exc:  # noqa: BLE001 — enhancement, never blocks
            _LOG.warning("media publish failed: %s", exc)
            report["publish"] = {"error": str(exc)}
    else:
        report["publish"] = {"skipped": "no_wix_or_site_id"}

    return StageResult(ok=True, detail={"media_report": report})


def _seo_stage(ctx: StageContext) -> StageResult:
    """Build + enforce the SEO package (structured data + per-page meta).

    Generates LocalBusiness/Organization JSON-LD + a per-page title/meta plan
    (``backend.website.seo_enrich``). Under the ``enforce`` posture an
    incomplete/ill-formed package PARKS the build (cannot reach publish) until
    it passes; ``audit`` records the gaps without blocking; ``off`` skips the
    checks. The package is stored for the operator to apply in Wix (titles +
    meta in the SEO panel, JSON-LD via custom code). The live SEO + security
    audit is enforced later, at the deliver gate, against the published URL.
    """
    if not ctx.state.site_id:
        return StageResult(ok=False, parked=True, park_reason="no_site_id")

    brief = ctx.order.brief
    if not getattr(ctx.settings, "website_seo_enrich_enabled", False):
        return StageResult(ok=True, detail={"seo_enrich": "disabled"})

    from . import seo_enrich

    package = seo_enrich.build_seo_package(brief, public_url=getattr(brief, "public_url", "") or "")

    # Codex-gates the generated SEO copy (G2 banned-phrase scan).
    seo_copy = "\n".join(
        f"{m.get('title', '')} {m.get('description', '')}" for m in package["page_meta"].values()
    )
    verdict = codex_gate(capability="website_seo", payload={"copy": seo_copy})
    if not verdict.allowed:
        return _blocked("seo", verdict)

    gate = str(getattr(ctx.settings, "website_seo_gate", "off") or "off").lower()
    problems = seo_enrich.check_seo_package(package)
    if problems and gate == "enforce":
        return StageResult(
            ok=False,
            parked=True,
            park_reason="seo_incomplete:" + "; ".join(problems[:4]),
            detail={"seo_package": package, "seo_problems": problems},
        )

    return StageResult(
        ok=True,
        detail={
            "seo_package": package,
            "seo_pages": len(package["page_meta"]),
            "seo_problems": problems,  # empty on a clean pass; surfaced under `audit`
            "seo_gate": gate,
        },
    )


def _qa_stage(ctx: StageContext) -> StageResult:
    """Read the populated content back + compile a preview checklist to sign off."""
    if not ctx.state.site_id:
        return StageResult(ok=False, parked=True, park_reason="no_site_id")

    items_seen = 0
    collection_id = _content_collection_id(ctx)
    if ctx.wix is not None and collection_id:
        try:
            resp = ctx.wix.query_data_items(site_id=ctx.state.site_id, collection_id=collection_id)
            items_seen = len(resp.get("dataItems") or resp.get("items") or [])
        except WixError as exc:
            _LOG.warning("website qa readback failed: %s", exc)

    checklist = {
        "site_id": ctx.state.site_id,
        "expected_pages": len(ctx.order.brief.pages),
        "content_items_written": len(ctx.state.content_item_ids),
        "content_items_read_back": items_seen,
        "business_info_set": "business_info" in ctx.state.completed_stages,
        "preview_url": f"https://manage.wix.com/dashboard/{ctx.state.site_id}",
    }
    return StageResult(ok=True, detail={"preview_checklist": checklist})


def _publish_stage(ctx: StageContext) -> StageResult:
    """Publish the site live. OUTWARD: gated by website_live_publish_enabled.

    With the live flag off this parks (resumable the moment the operator allows
    it) rather than publishing — the whole build runs, the site is just left
    unpublished. The live publish mechanism is confirmed during the walk-through.
    """
    if not ctx.state.site_id:
        return StageResult(ok=False, parked=True, park_reason="no_site_id")
    if not ctx.live_publish_enabled:
        return StageResult(ok=False, parked=True, park_reason="live_publish_disabled")

    if ctx.wix is None:
        return StageResult(ok=False, parked=True, park_reason="wix_credentials_unset")

    verdict = codex_gate(capability="website_publish", payload={"site_id": ctx.state.site_id})
    if not verdict.allowed:
        return _blocked("publish", verdict)

    try:
        ctx.wix.publish_site(site_id=ctx.state.site_id)
    except WixError as exc:
        return StageResult(
            ok=False,
            parked=True,
            park_reason=f"wix_publish_failed:{exc.status_code or 'transport'}",
        )
    return StageResult(
        ok=True,
        detail={
            "published": True,
            "site_url": f"https://manage.wix.com/dashboard/{ctx.state.site_id}",
        },
    )


def _deliver_stage(ctx: StageContext) -> StageResult:
    """Hand the live URL to the customer + record delivery. OUTWARD."""
    if not ctx.state.site_url and not ctx.state.site_id:
        return StageResult(ok=False, parked=True, park_reason="no_site")
    if not ctx.live_publish_enabled:
        return StageResult(ok=False, parked=True, park_reason="live_publish_disabled")

    url = ctx.state.site_url or f"https://manage.wix.com/dashboard/{ctx.state.site_id}"
    verdict = codex_gate(
        capability="website_deliver",
        payload={"customer_name": ctx.order.customer_name, "url": url},
    )
    if not verdict.allowed:
        return _blocked("deliver", verdict)

    # --- live SEO + security gate (enforced before hand-off) --------------
    seo_gate = str(getattr(ctx.settings, "website_seo_gate", "off") or "off").lower()
    sec_gate = str(getattr(ctx.settings, "website_security_gate", "off") or "off").lower()
    public_url = (getattr(ctx.order.brief, "public_url", "") or "").strip()
    live: dict[str, Any] = {}
    if seo_gate != "off" or sec_gate != "off":
        if not public_url:
            # Cannot verify the live site without its public URL — fail-closed
            # under enforce, advisory otherwise.
            if "enforce" in (seo_gate, sec_gate):
                return StageResult(
                    ok=False,
                    parked=True,
                    park_reason="public_url_unknown",
                    detail={"hint": "set brief.public_url to the published site URL"},
                )
        else:
            from . import seo_enrich

            live = seo_enrich.evaluate_live(
                public_url,
                keywords=(ctx.state.seo_package or {}).get("keywords")
                if ctx.state.seo_package
                else None,
                industry=ctx.order.brief.industry,
            )
            problems = seo_enrich.live_gate_violations(
                live,
                seo_min_score=int(getattr(ctx.settings, "website_seo_min_score", 70)),
                security_min_grade=str(getattr(ctx.settings, "website_security_min_grade", "B")),
                seo_gate=seo_gate,
                security_gate=sec_gate,
            )
            if problems and ("enforce" in (seo_gate, sec_gate)):
                return StageResult(
                    ok=False,
                    parked=True,
                    park_reason="quality_gate_failed:" + "; ".join(problems[:3]),
                    detail={"live_audit": live, "gate_problems": problems},
                )

    # The actual hand-off channel (email the customer) reuses the outreach
    # workcell and is wired during the walk-through; record delivery durably now.
    return StageResult(
        ok=True,
        detail={
            "delivered_url": url,
            "delivered_at": iso_now(),
            "live_audit": live,
        },
    )


def _settle_stage(ctx: StageContext) -> StageResult:
    """Record the financial event. Never mutates the operator-curated ledger.

    The liabilities ledger is operator-curated ("append a repayment whenever a
    loan is paid back"). So a *barter* settlement does NOT auto-write
    ``liabilities.yaml`` — it produces a settlement marker + an operator task to
    append the repayment, keeping the human as the ledger's author. An *invoice*
    settlement records the intent for the finance workcell; *gratis* is a no-op.
    """
    order = ctx.order
    if order.settlement_kind == "barter":
        if not order.settlement_lender_id:
            return StageResult(ok=False, parked=True, park_reason="barter_lender_unset")
        marker = (
            f"barter_repayment:{order.settlement_lender_id}:"
            f"${order.settlement_amount_usd:.2f}:website:{ctx.state.site_id or 'pending'}"
        )
        # Best-effort operator task so the human appends the repayment row.
        if ctx.crm is not None:
            try:
                from backend.crm.models import CreateOperatorTaskRequest

                ctx.crm.create_operator_task(
                    CreateOperatorTaskRequest(
                        kind="other",
                        title=(
                            f"Append ${order.settlement_amount_usd:.2f} repayment-in-kind "
                            f"for {order.settlement_lender_id} (website delivered)"
                        ),
                        description=(
                            "Website build delivered as work-for-debt. Append a "
                            f"repayments[] entry for lender_id={order.settlement_lender_id} "
                            f"amount_usd={order.settlement_amount_usd:.2f} in "
                            "backend/finance/liabilities.yaml (operator-curated)."
                        ),
                        source="website_settle",
                        source_ref=marker,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — marker is the deliverable
                _LOG.warning("website settle: operator task creation failed: %s", exc)
        return StageResult(ok=True, detail={"settlement_ref": marker})

    if order.settlement_kind == "invoice":
        marker = f"invoice_pending:{order.customer_name}:${order.settlement_amount_usd:.2f}"
        return StageResult(ok=True, detail={"settlement_ref": marker})

    return StageResult(ok=True, detail={"settlement_ref": "gratis"})


# Stage name -> handler. Injectable wholesale in tests.
DEFAULT_HANDLERS: dict[str, Callable[[StageContext], StageResult]] = {
    "brief": _brief_stage,
    "generate": _generate_stage,
    "provision": _provision_stage,
    "business_info": _business_info_stage,
    "content": _content_stage,
    "media": _media_stage,
    "seo": _seo_stage,
    "qa": _qa_stage,
    "publish": _publish_stage,
    "deliver": _deliver_stage,
    "settle": _settle_stage,
}


__all__ = ["StageContext", "StageResult", "DEFAULT_HANDLERS"]
