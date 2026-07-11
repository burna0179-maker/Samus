"""Pre-send lint: catch subject/body/CTA misalignment before a tracked send.

The Kelly Zimmerman 2026-06-30 send went out with subject
``"Kelly — your receptionist build, scoped"`` (implying the $2,500–$3,000
Workflow System Buildout) but a hero button pointing at the $500
48-Hour Workflow Rescue Stripe link. Two different SKUs, two different
prices — the operator caught it on read-back, but the send had already
left. This module is the durable insurance: extract the buy URLs from a
prepared message, resolve them to SKUs via the catalog, and check the
subject (+ optionally body) for SKU keywords that don't line up.

Returns a structured :class:`LintReport`. Never sends. Never raises into
the caller — a lint fault produces a warning, not an exception. Callers
choose whether to block (the new :func:`send_promotional` wrapper does so
when ``settings.outreach_send_lint_mode == "enforce"``).

Wired-DORMANT — two-key posture mirroring the rest of the outreach surface:
  * ``OUTREACH_SEND_LINT_MODE`` (``off`` | ``audit`` | ``enforce``,
    default ``off``).
  * ``off``: returns an empty report (skipped). ``audit``: returns a real
    report but the wrapper never raises. ``enforce``: the wrapper raises
    :class:`SendLintBlocked` when ``report.aligned`` is False.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urlparse

from backend.catalog.registry import CATALOG, CatalogEntry


class SendLintBlocked(RuntimeError):
    """Raised by :func:`send_promotional` in ENFORCE mode on misalignment."""


# Per-SKU keyword bag — the lint uses this to detect which SKU the
# operator's subject/body copy *implies*. Built from each SKU's display
# name plus a small hand-curated synonym list so common copy phrasings
# resolve correctly. Keep this conservative — false positives degrade
# the lint into noise.
_SKU_KEYWORDS: dict[str, tuple[str, ...]] = {
    "seo_audit": ("seo audit", "seo report", "audit"),
    "service_seo_implementation": (
        "seo implementation",
        "seo fixes",
        "implementation",
    ),
    "retainer_seo_optimization": (
        "seo optimization",
        "seo retainer",
        "ongoing seo",
    ),
    "service_workflow_rescue": (
        "48-hour",
        "48 hour",
        "workflow rescue",
        "rescue",
    ),
    "service_workflow_buildout": (
        "workflow buildout",
        "workflow system buildout",
        "system buildout",
        "receptionist build",
        "buildout",
    ),
    "service_website_build": ("website build", "site build"),
    "service_ai_ops_partner_build": (
        "ai ops partner",
        "ai ops build",
        "ops partner build",
    ),
    "retainer_ai_ops_partner_entry": (
        "ai ops partner",
        "ops partner",
        "ai ops retainer",
    ),
    "retainer_ai_ops_partner_premium": (
        "ai ops premium",
        "ops partner premium",
    ),
    "retainer_ai_receptionist": (
        "ai receptionist",
        "ai digital receptionist",
        "receptionist subscription",
    ),
    "playbook_lead_qual": ("lead qualification", "lead qual playbook"),
    "playbook_client_onboarding": ("client onboarding playbook", "onboarding playbook"),
}

# Stripe checkout host — every catalog payment_link_url lives here.
_STRIPE_HOST = "buy.stripe.com"

# URL regex — permissive enough to catch hrefs and bare text URLs.
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.IGNORECASE)


@dataclass
class LintReport:
    """Structured pre-send check result.

    ``aligned`` is the headline: True iff every resolved SKU from a button
    appears in the subject keyword bag (or there are no buttons at all —
    a transactional message with no buy URL is trivially aligned).
    """

    extracted_buy_urls: list[str] = field(default_factory=list)
    button_skus: list[str] = field(default_factory=list)
    subject_implied_skus: list[str] = field(default_factory=list)
    body_implied_skus: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    aligned: bool = True
    skipped: bool = False


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _strip_html(html: str) -> str:
    """Best-effort tag strip for keyword scanning. Not a full HTML parser —
    we only care about visible text + href attributes."""
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    # Collapse runs of whitespace so phrase matching works.
    return re.sub(r"\s+", " ", text)


def _extract_buy_urls(*sources: str) -> list[str]:
    """Pull every buy.stripe.com URL out of the given text/html sources."""
    seen: set[str] = set()
    out: list[str] = []
    for src in sources:
        if not src:
            continue
        for match in _URL_RE.findall(src):
            host = urlparse(match).netloc.lower()
            if host != _STRIPE_HOST:
                continue
            # Drop the query string (client_reference_id varies per send)
            # for canonical equality against catalog payment_link_url.
            base = match.split("?", 1)[0].rstrip(".,);")
            if base in seen:
                continue
            seen.add(base)
            out.append(base)
    return out


def _catalog_url_index() -> dict[str, CatalogEntry]:
    """Map of normalized buy URL → CatalogEntry. Built fresh each call —
    cheap, the catalog has ~15 entries."""
    out: dict[str, CatalogEntry] = {}
    for entry in CATALOG:
        url = (entry.payment_link_url or "").strip()
        if not url:
            continue
        base = url.split("?", 1)[0].rstrip(".,);")
        out[base] = entry
    return out


def _resolve_skus(urls: Iterable[str]) -> tuple[list[str], list[str]]:
    """Resolve buy URLs to sku_ids. Returns (resolved, unmapped)."""
    idx = _catalog_url_index()
    resolved: list[str] = []
    unmapped: list[str] = []
    for url in urls:
        entry = idx.get(url)
        if entry:
            if entry.sku_id not in resolved:
                resolved.append(entry.sku_id)
        else:
            unmapped.append(url)
    return resolved, unmapped


def _detect_sku_keywords(text: str) -> list[str]:
    """Return sku_ids whose keyword bag appears in ``text`` (case-insensitive)."""
    if not text:
        return []
    haystack = text.lower()
    out: list[str] = []
    for sku_id, kws in _SKU_KEYWORDS.items():
        if any(kw in haystack for kw in kws):
            out.append(sku_id)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def lint_send(
    *,
    subject: str,
    html_body: str = "",
    text_body: str = "",
    claimed_sku: str | None = None,
) -> LintReport:
    """Pre-send alignment check. Never raises.

    ``claimed_sku`` is the SKU the operator *intends* to sell with this send;
    when provided, the lint requires it to be present in both the buttons
    AND the subject. Without it, the lint just checks button↔subject
    consistency.
    """
    from backend.common.config import get_settings

    mode = str(getattr(get_settings(), "outreach_send_lint_mode", "off") or "off").strip().lower()
    if mode == "off":
        return LintReport(skipped=True, aligned=True)

    urls = _extract_buy_urls(html_body, text_body)
    button_skus, unmapped = _resolve_skus(urls)
    subject_skus = _detect_sku_keywords(subject)
    body_skus = _detect_sku_keywords(_strip_html(html_body) + " " + (text_body or ""))

    report = LintReport(
        extracted_buy_urls=urls,
        button_skus=button_skus,
        subject_implied_skus=subject_skus,
        body_implied_skus=body_skus,
    )

    for url in unmapped:
        report.warnings.append(f"buy.stripe.com URL not in catalog (cannot lint): {url}")

    # Aligned iff every button SKU is implied by the subject. A subject that
    # mentions SKUs the buttons don't sell is a warning, not a block — the
    # operator might be referencing the upgrade path on purpose.
    if not button_skus:
        # No clickable hero — transactional / informational. Trivially aligned.
        report.aligned = True
    else:
        unaligned = [s for s in button_skus if s not in subject_skus]
        if unaligned:
            report.aligned = False
            for sku in unaligned:
                report.warnings.append(
                    f"button SKU {sku!r} not implied by subject "
                    f"(subject implies: {subject_skus or 'nothing in catalog'})"
                )

    # Subject implies a SKU the buttons don't sell — that's the Kelly bug.
    subject_only = [s for s in subject_skus if s not in button_skus]
    if subject_only and button_skus:
        report.aligned = False
        for sku in subject_only:
            report.warnings.append(
                f"subject implies SKU {sku!r} but no button sells it (buttons sell: {button_skus})"
            )

    if claimed_sku:
        if claimed_sku not in button_skus:
            report.aligned = False
            report.warnings.append(
                f"claimed_sku {claimed_sku!r} not present in buttons: {button_skus}"
            )
        if claimed_sku not in subject_skus:
            report.aligned = False
            report.warnings.append(f"claimed_sku {claimed_sku!r} not implied by subject")

    return report


def send_promotional(
    *,
    to: str,
    subject: str,
    body: str,
    html_body: str | None = None,
    claimed_sku: str | None = None,
    **send_kwargs,
) -> dict:
    """Thin wrapper around :func:`backend.common.email_backend.send_email`
    that runs :func:`lint_send` first. In ENFORCE mode an unaligned send
    raises :class:`SendLintBlocked`; in AUDIT mode the warnings are logged
    and the send proceeds; in OFF mode the lint is skipped entirely.
    """
    import logging

    log = logging.getLogger("samus.outreach.send_lint")

    from backend.common.config import get_settings
    from backend.common.email_backend import send_email

    mode = str(getattr(get_settings(), "outreach_send_lint_mode", "off") or "off").strip().lower()

    report = lint_send(
        subject=subject,
        html_body=html_body or "",
        text_body=body,
        claimed_sku=claimed_sku,
    )
    if not report.skipped and report.warnings:
        for w in report.warnings:
            log.warning("send_lint warning to=%s: %s", to, w)
    if mode == "enforce" and not report.aligned:
        raise SendLintBlocked(
            f"send blocked by send_lint (subject={subject!r}, warnings={report.warnings})"
        )
    resp = send_email(
        to=to,
        subject=subject,
        body=body,
        html_body=html_body,
        **send_kwargs,
    )
    if isinstance(resp, dict):
        resp = dict(resp)
        resp.setdefault(
            "lint",
            {
                "aligned": report.aligned,
                "warnings": report.warnings,
                "button_skus": report.button_skus,
                "subject_implied_skus": report.subject_implied_skus,
                "mode": mode,
            },
        )
    return resp
