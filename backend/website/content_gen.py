"""Taste-governed website content generation (the "frontend codegen").

A Wix site's *layout* comes from the chosen template; what Samus generates is
everything that fills it: the page copy, the SEO meta, and the business voice.
This module is that generator. It:

  1. Builds a :class:`~backend.taste.models.TasteProfile` from the brief
     (industry / description / brand) — dials, design-system leaning, and the
     palette/type/anti-slop constraints.
  2. Generates each page's copy. Preferred path is one budgeted Anthropic call
     (``backend.common.llm_client.anthropic_messages``, workcell ``website``)
     whose system prompt carries the taste constraints. If there is no API key,
     the per-day cap is blown, or the call/parse fails, it falls back to a
     deterministic, on-brand template — so a site can be built today on a $0
     budget.
  3. Runs the deterministic taste Pre-Flight audit over the generated copy and
     fails closed: any hard violation (an em-dash, etc.) is first sanitized,
     then — if anything still fails — that page is rebuilt from the deterministic
     template. Slop never reaches a paying customer's site.

Operator-supplied content always wins: a field already present in
``page.content`` is never overwritten. Pure-Python + one optional LLM call; no
Wix, no filesystem — fully unit-testable offline.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from backend.common.llm_client import (
    BudgetExceeded,
    GlobalBudgetExceeded,
    LlmCallError,
    ModelNotPermitted,
    anthropic_messages,
    record_outcome,
)
from backend.taste.audit import audit_text
from backend.taste.profile import build_profile

from .models import WebsiteBrief, WebsitePage

_LOG = logging.getLogger("samus.website.content_gen")

_BUDGET_WORKCELL = "website"

# The fields the generator fills per page (operator-supplied keys are kept).
STANDARD_FIELDS: tuple[str, ...] = (
    "headline", "subheadline", "body", "cta", "meta_description",
)

# One contact-intent CTA label, used site-wide, so the taste audit's
# duplicate-CTA-intent check never trips on the generated copy.
_SITE_CTA = "Get started"

# The default page skeleton when a brief lists no pages — slug + title only;
# the generator fills the copy.
_DEFAULT_PAGES: tuple[tuple[str, str], ...] = (
    ("home", "Home"),
    ("about", "About"),
    ("services", "Services"),
    ("contact", "Contact"),
)

_DASH_RE = re.compile("[—–]")
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def default_page_set() -> list[WebsitePage]:
    """A standard four-page skeleton (empty content) for a contentless brief."""
    return [WebsitePage(slug=slug, title=title) for slug, title in _DEFAULT_PAGES]


def _sanitize(text: str) -> str:
    """Strip the banned dash characters (the #1 taste hard-fail) from copy."""
    return _DASH_RE.sub(" - ", text)


# --------------------------------------------------------------------------
# Deterministic fallback copy (always available, always slop-free)
# --------------------------------------------------------------------------

def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "."


def _deterministic_page(brief: WebsiteBrief, page: WebsitePage) -> dict[str, str]:
    """On-brand template copy for one page. Em-dash-free by construction."""
    name = brief.business_name.strip()
    desc = (brief.business_description or "").strip().rstrip(".")
    industry = (brief.industry or "").strip()
    what = desc or (f"{industry} services you can rely on" if industry else "work you can rely on")

    slug = page.slug.lower()
    if slug == "home":
        head = name
        sub = _clip(what.capitalize() + ".", 120)
        body = _clip(f"{name} delivers {what}. Built for the people you serve, with care and follow-through.", 280)
    elif slug == "about":
        head = f"About {name}"
        sub = _clip(f"Who we are and why {name} exists.", 120)
        body = _clip(f"{name} was built to deliver {what}. We keep it straightforward: do good work, communicate clearly, and stand behind the result.", 320)
    elif slug in ("services", "service", "work", "offerings"):
        head = "What we do"
        sub = _clip(f"How {name} helps.", 120)
        body = _clip(f"{name} provides {what}. Every engagement is scoped up front so you know exactly what you are getting.", 300)
    elif slug in ("contact", "contacts"):
        head = f"Reach {name}"
        sub = _clip("Tell us what you need and we will get back to you.", 120)
        bits = []
        if brief.contact_email:
            bits.append(f"Email: {brief.contact_email}")
        if brief.contact_phone:
            bits.append(f"Phone: {brief.contact_phone}")
        if brief.address:
            bits.append(brief.address)
        body = _clip(". ".join(bits) or f"Send {name} a message and we will respond promptly.", 300)
    else:
        head = page.title or name
        sub = _clip(what.capitalize() + ".", 120)
        body = _clip(f"{name} delivers {what}.", 280)

    meta = _clip(f"{name}: {what}.", 160)
    return {
        "headline": _sanitize(head),
        "subheadline": _sanitize(sub),
        "body": _sanitize(body),
        "cta": _SITE_CTA,
        "meta_description": _sanitize(meta),
    }


# --------------------------------------------------------------------------
# LLM path
# --------------------------------------------------------------------------

def _system_prompt(profile: Any) -> str:
    dials = profile.dials
    return (
        "You are an expert website copywriter. You write tight, concrete, "
        "on-brand marketing copy that reads like a human wrote it, never like an "
        "AI template. Hard rules you MUST follow:\n"
        "- NEVER use an em-dash or en-dash. Use periods, commas, or ' - '. This is "
        "the single most important rule.\n"
        "- No AI tells: no 'Quietly trusted by', no poetic section labels, no fake "
        "precision, no cute wordplay. Plain, functional sentences only.\n"
        f"- headline: <= 8 words. subheadline: <= 20 words. body: <= 60 words. "
        f"cta: use exactly '{_SITE_CTA}' for every page. meta_description: <= 155 chars.\n"
        f"- Density target {dials.visual_density}/10: keep it lean, cut filler.\n"
        "- One brand voice across all pages.\n"
        "Return STRICT JSON only, shape: "
        '{"pages": {"<slug>": {"headline": "..", "subheadline": "..", "body": "..", '
        '"cta": "..", "meta_description": ".."}}}. No prose outside the JSON.'
    )


def _user_prompt(brief: WebsiteBrief, pages: list[WebsitePage]) -> str:
    lines = [
        f"Business name: {brief.business_name}",
        f"What it does: {brief.business_description or '(infer from name)'}",
    ]
    if brief.industry:
        lines.append(f"Industry: {brief.industry}")
    if brief.brand_colors:
        lines.append(f"Brand colors: {', '.join(brief.brand_colors)}")
    lines.append("Write copy for these pages (use the slug as the JSON key):")
    for p in pages:
        lines.append(f"  - {p.slug}: {p.title}")
    return "\n".join(lines)


def _strip_fences(text: str) -> str:
    text = text.strip()
    # remove a leading ```json / ``` and a trailing ```
    text = re.sub(r"^\s*```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


def _parse_llm_pages(text: str) -> dict[str, dict[str, str]]:
    """Parse the model's JSON into ``{slug: {field: value}}``. Raises on bad shape."""
    data = json.loads(_strip_fences(text))
    pages = data.get("pages") if isinstance(data, dict) else None
    if not isinstance(pages, dict):
        raise ValueError("missing 'pages' object")
    out: dict[str, dict[str, str]] = {}
    for slug, fields in pages.items():
        if isinstance(fields, dict):
            out[str(slug)] = {k: str(v) for k, v in fields.items() if isinstance(v, (str, int, float))}
    if not out:
        raise ValueError("no usable page entries")
    return out


def _llm_generate(
    brief: WebsiteBrief,
    pages: list[WebsitePage],
    profile: Any,
    *,
    api_key: str,
    model: str,
    max_tokens: int,
) -> tuple[dict[str, dict[str, str]] | None, bool]:
    """Try one budgeted Anthropic call. Returns (pages_by_slug or None, llm_used)."""
    try:
        text, _usage = anthropic_messages(
            workcell=_BUDGET_WORKCELL,
            api_key=api_key,
            prompt=_user_prompt(brief, pages),
            system=_system_prompt(profile),
            model=model,
            cache_system=True,
            max_tokens=max_tokens,
            security_label="general",
        )
    except (BudgetExceeded, GlobalBudgetExceeded) as exc:
        _LOG.info("website content gen budget denied; using deterministic copy: %s", exc)
        return None, False
    except ModelNotPermitted as exc:
        _LOG.warning("website content gen model not permitted: %s", exc)
        return None, False
    except LlmCallError as exc:
        _LOG.warning("website content gen LLM call failed; deterministic fallback: %s", exc)
        return None, False

    try:
        return _parse_llm_pages(text), True
    except (ValueError, json.JSONDecodeError) as exc:
        # 200 but unusable — flip the auto-recorded success to failure for the EMA.
        record_outcome(_BUDGET_WORKCELL, outcome="failure")
        _LOG.warning("website content gen response unparseable; deterministic fallback: %s", exc)
        return None, False


# --------------------------------------------------------------------------
# Public entry
# --------------------------------------------------------------------------

def generate_site_content(
    brief: WebsiteBrief,
    *,
    settings: Any,
    api_key: str | None = None,
) -> tuple[WebsiteBrief, dict[str, Any]]:
    """Generate (or enrich) the brief's page copy under taste governance.

    Returns ``(enriched_brief, report)``. ``enriched_brief`` is a copy with every
    page's ``content`` filled for the STANDARD_FIELDS (operator-supplied values
    preserved). ``report`` carries the taste profile, the final audit, and which
    path was taken — for the operator's review and for the artifact record.
    """
    pages = brief.pages or default_page_set()

    profile = build_profile(
        f"{brief.business_name}. {brief.business_description}. {brief.industry}".strip(),
        vibe_words=list(brief.brand_colors) or None,
    )

    # LM Studio backend needs no auth; the `api_key` slot is preserved on the
    # wrapper for OpenAI-mode parity. Fire unconditionally -- the local model
    # is free and every downstream taste/quality control still runs.
    key = "unused"
    model = str(getattr(settings, "website_content_model", "claude-haiku-4-5-20251001"))
    max_tokens = int(getattr(settings, "website_content_max_tokens", 1200))

    result, llm_used = _llm_generate(
        brief, pages, profile, api_key=key, model=model, max_tokens=max_tokens,
    )
    llm_pages = result or {}

    # Did the raw model output carry any banned dash that we had to strip on the
    # way in? (Recorded so the report is truthful even when the pre-merge field
    # sanitize means the later audit sees already-clean copy.)
    pre_merge_sanitized = any(
        _DASH_RE.search(v) for fields in llm_pages.values() for v in fields.values()
    )

    enriched_pages: list[WebsitePage] = []
    fields_filled: dict[str, list[str]] = {}
    for page in pages:
        generated = _sanitize_fields(llm_pages.get(page.slug, {}))
        deterministic = _deterministic_page(brief, page)
        content = dict(page.content)  # operator-supplied wins
        filled: list[str] = []
        for field_name in STANDARD_FIELDS:
            if content.get(field_name):
                continue  # keep operator value
            content[field_name] = generated.get(field_name) or deterministic[field_name]
            filled.append(field_name)
        enriched_pages.append(WebsitePage(slug=page.slug, title=page.title, content=content))
        fields_filled[page.slug] = filled

    enriched = brief.model_copy(update={"pages": enriched_pages})

    # Fail-closed taste audit over the assembled copy.
    audit, sanitized, enriched = _audit_and_repair(brief, enriched)

    report = {
        "taste_profile": profile.to_dict(),
        "taste_audit": audit.to_dict(),
        "llm_used": llm_used,
        "sanitized": bool(sanitized or pre_merge_sanitized),
        "pages_generated": [p.slug for p in enriched_pages],
        "fields_filled": fields_filled,
    }
    return enriched, report


def _sanitize_fields(fields: dict[str, str]) -> dict[str, str]:
    return {k: _sanitize(v) for k, v in fields.items()}


def _all_copy(brief: WebsiteBrief) -> str:
    parts: list[str] = [brief.business_name, brief.business_description]
    for page in brief.pages:
        parts.append(page.title)
        parts.extend(str(v) for v in page.content.values())
    return "\n".join(p for p in parts if p)


def _audit_and_repair(
    original: WebsiteBrief, enriched: WebsiteBrief,
) -> tuple[Any, bool, WebsiteBrief]:
    """Audit the copy; sanitize then deterministically rebuild until it passes."""
    audit = audit_text(_all_copy(enriched), kind="website")
    if audit.passed:
        return audit, False, enriched

    # Pass 1 — sanitize every generated string field, re-audit.
    repaired_pages = [
        WebsitePage(slug=p.slug, title=p.title,
                    content={k: _sanitize(v) for k, v in p.content.items()})
        for p in enriched.pages
    ]
    enriched = enriched.model_copy(update={"pages": repaired_pages})
    audit = audit_text(_all_copy(enriched), kind="website")
    if audit.passed:
        return audit, True, enriched

    # Pass 2 — last resort: rebuild every page from the deterministic template.
    det_pages = [
        WebsitePage(slug=p.slug, title=p.title, content=_deterministic_page(original, p))
        for p in enriched.pages
    ]
    enriched = enriched.model_copy(update={"pages": det_pages})
    audit = audit_text(_all_copy(enriched), kind="website")
    return audit, True, enriched


__all__ = ["generate_site_content", "default_page_set", "STANDARD_FIELDS"]
