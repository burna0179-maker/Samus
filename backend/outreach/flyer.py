"""Impulse-purchase flyer for cold outreach — a lightweight, spam-safe HTML
email with ONE buy-now CTA.

Two modes:
  * MATCHED (default): the finding / security_grade / seo signals pick the
    lowest-friction relevant add-on ($29.99-$149) with a live Stripe link.
  * FEATURED (a campaign leads with one proven offer, e.g. the 48-Hour Workflow
    Rescue): the flyer pitches that offer with its proven copy, and only falls
    back to the matched add-on when a specific cheaper fix is clearly more
    relevant to that prospect.

DELIVERABILITY-FIRST (fresh sender reputation): mostly text, a single styled
CTA button, NO external images, inline CSS only, no scripts, the same CAN-SPAM
footer the plain-text path uses, and all content HTML-escaped. The plain-text
body is still sent alongside as the multipart fallback.

The buy-now URL carries ``client_reference_id=<prospect_id>`` so the Stripe
webhook can attribute a purchase back to the prospect.
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from urllib.parse import urlencode

_LOG = logging.getLogger("samus.outreach.flyer")

# Matched add-on copy (label, one-line "what you get").
_OFFER_COPY: dict[str, tuple[str, str]] = {
    "service_website_build": (
        "Business Website Build",
        "a fast, professional website built for you — so customers searching for "
        "your business actually find and reach you instead of a competitor",
    ),
    "seo_audit": (
        "Full Website & Security Audit",
        "a prioritized report of exactly what is dragging your site down in "
        "search and trust, with the fixes ranked by impact",
    ),
    "addon_404_audit": (
        "Broken-Link & 404 Sweep",
        "every broken link and dead page on your site found and listed so you "
        "stop losing visitors (and ranking) to them",
    ),
    "addon_dns_health": (
        "DNS & SSL Health Check",
        "your domain, DNS, and certificate checked for the misconfigurations "
        "that quietly break email and browser trust",
    ),
    "addon_email_deliverability": (
        "Email Deliverability Fix",
        "why your business email lands in spam and the exact DNS records to fix it",
    ),
}
_DEFAULT_SKU = "seo_audit"

# Findings that indicate a specific cheap fix is MORE relevant than a featured
# offer — when present, a featured campaign falls back to the matched add-on.
_SPECIFIC_FINDING_KEYS = (
    "404", "broken link", "dns", "ssl", "certificate", "deliver", "spf", "dkim",
)

# Featured-offer copy (proven assets). Keyed by SKU.
_FEATURED_COPY: dict[str, dict] = {
    "service_workflow_rescue": {
        "headline": "Stop doing that manual task by Friday",
        "pitch": "Pick one repetitive task that eats your team's time every "
                 "week. In 48 hours we design, build, deploy, and hand off a "
                 "production-ready automation that does it for you — so you "
                 "get back to growth instead of busywork.",
        "bullets": (
            "Workflow audit + a clear automation blueprint",
            "End-to-end build, deployed and validated on your real scenarios",
            "Handoff guide + a live walkthrough with your team",
        ),
        "cta_label": "Start My 48-Hour Automation",
        "assurance": "Limited to 3 builds per week. If we can't map a viable "
                     "automation path in the audit, you don't proceed — no risk.",
    },
}


@dataclass
class Offer:
    sku_id: str
    label: str
    price_usd: float
    payment_link: str
    why: str = ""
    kind: str = "matched"                  # "matched" | "featured"
    headline: str = ""
    pitch: str = ""
    cta_label: str = ""
    assurance: str = ""
    bullets: tuple[str, ...] = field(default_factory=tuple)


def _catalog_index() -> dict[str, object]:
    from backend.catalog.registry import CATALOG
    return {e.sku_id: e for e in CATALOG}


def _resolve(sku_id: str, index: dict[str, object]) -> object | None:
    entry = index.get(sku_id)
    if entry is not None and getattr(entry, "payment_link_url", None):
        return entry
    return None


def _matched_offer(row: dict) -> Offer | None:
    """Pick the lowest-friction relevant purchasable add-on for this prospect."""
    finding = (row.get("callsheet_finding") or "").lower()
    grade = (row.get("security_grade") or "").strip().upper()
    try:
        seo = int(float(row.get("seo_score") or 100))
    except (TypeError, ValueError):
        seo = 100

    # No website at all -> a BUILD, not an audit. Selling a $149 audit to a
    # business with no site is both irrelevant (nothing to audit) and leaves the
    # bigger, right-fit build unsold. Checked first: "no website" is the most
    # fundamental gap and overrides the narrower site-issue matches.
    if any(k in finding for k in (
        "no working website", "no website", "no real website", "no site",
        "doesn't have a website", "does not have a website", "no online presence",
        "website is down", "site is down", "not showing up",
    )):
        sku = "service_website_build"
    elif "404" in finding or "broken link" in finding:
        sku = "addon_404_audit"
    elif any(k in finding for k in ("dns", "ssl", "certificate", "not secure", "https")):
        sku = "addon_dns_health"
    elif "deliver" in finding or "spf" in finding or "dkim" in finding or "spam folder" in finding:
        sku = "addon_email_deliverability"
    else:
        sku = _DEFAULT_SKU  # broad site/security/seo/reviews issue -> full audit

    index = _catalog_index()
    entry = _resolve(sku, index) or _resolve(_DEFAULT_SKU, index)
    if entry is None:
        entry = next((e for e in index.values()
                      if getattr(e, "payment_link_url", None)), None)
    if entry is None:
        _LOG.warning("no purchasable SKU with a payment link — flyer skipped")
        return None

    label, base_why = _OFFER_COPY.get(
        entry.sku_id, ("Website Growth Audit", "the highest-impact fixes for your site"))
    why = base_why
    if grade in ("D", "F"):
        why = f"your site is currently graded {grade} for security — " + base_why
    return Offer(
        sku_id=entry.sku_id, label=label, price_usd=entry.price_usd_cents / 100.0,
        payment_link=entry.payment_link_url, why=why, kind="matched",
    )


def _featured_offer(sku_id: str) -> Offer | None:
    entry = _resolve(sku_id, _catalog_index())
    if entry is None:
        return None
    copy = _FEATURED_COPY.get(sku_id, {})
    return Offer(
        sku_id=entry.sku_id,
        label=copy.get("headline") or getattr(entry, "display_name", sku_id),
        price_usd=entry.price_usd_cents / 100.0,
        payment_link=entry.payment_link_url,
        why=copy.get("pitch", ""),
        kind="featured",
        headline=copy.get("headline", ""),
        pitch=copy.get("pitch", ""),
        cta_label=copy.get("cta_label", "Get started"),
        assurance=copy.get("assurance", ""),
        bullets=tuple(copy.get("bullets", ())),
    )


def match_offer(row: dict, *, featured_sku: str | None = None) -> Offer | None:
    """Choose the offer for this prospect.

    With ``featured_sku`` set (a campaign leads with one offer), return that
    featured offer UNLESS the prospect has a specific technical finding that a
    cheaper add-on fixes more directly — then fall back to the matched add-on.
    Without ``featured_sku``, return the matched add-on.
    """
    if featured_sku:
        finding = (row.get("callsheet_finding") or "").lower()
        specific = any(k in finding for k in _SPECIFIC_FINDING_KEYS)
        if not specific:
            feat = _featured_offer(featured_sku)
            if feat is not None:
                return feat
    return _matched_offer(row)


def buy_url(offer: Offer, *, prospect_id: str = "", email: str = "") -> str:
    """The payment link with attribution + campaign tags appended."""
    q = {"utm_source": "outreach", "utm_medium": "email", "utm_campaign": "flyer"}
    if prospect_id:
        # Namespaced so the Stripe webhook's attribution consumer recognises a
        # cold-outreach prospect (distinct from op_/upsell_ refs). See
        # backend.finance.outreach_attribution.
        q["client_reference_id"] = f"out_{prospect_id}"
    if email:
        q["prefilled_email"] = email
    sep = "&" if "?" in offer.payment_link else "?"
    return offer.payment_link + sep + urlencode(q)


def _price_str(price_usd: float) -> str:
    return f"${price_usd:,.0f}" if price_usd % 1 == 0 else f"${price_usd:,.2f}"


# ---------------------------------------------------------------------------
# Vibrant email design system (2026-07-07 operator directive: promo emails were
# text-heavy; lift them to the hand-designed "vibrant" flyer quality).
#
# Pure HTML/CSS, table-based, inline styles only — NO external images, NO raster
# (so no image-blocking / deliverability hit on a fresh sender), NO scripts. The
# same CAN-SPAM footer + hidden preheader + full HTML-escaping the text path
# uses. Dark theme, cyan/teal accents, gradient cards, rounded pills — the exact
# language of marketing/flyer_templates/..._vibrant.html, parameterised per
# offer so every SKU renders at that quality instead of one hand-built sample.
# ---------------------------------------------------------------------------

# Palette (single source so the whole email stays consistent).
_BG = "#050816"
_CARD = "rgba(7,17,31,.96)"
_INK = "#ffffff"
_MUTE = "#cbd5e1"
_DIM = "#94a3b8"
_CYAN = "#00e5ff"
_TEAL = "#14b8a6"


def _preheader(text: str) -> str:
    """Hidden inbox-preview line (shows in the inbox list, not in the body)."""
    return (
        '<div style="display:none;max-height:0;overflow:hidden;'
        f'color:{_BG};opacity:0;">{html.escape(text)}</div>'
    )


def _feature_grid(bullets: tuple[str, ...] | list[str]) -> str:
    """Two-column check-list card. Empty bullets -> empty string (no card)."""
    items = [b for b in (bullets or ()) if str(b).strip()]
    if not items:
        return ""
    check = (f'<span style="color:{_CYAN};font-weight:900;">&#10003;</span> ')
    cells = ""
    for i in range(0, len(items), 2):
        left = items[i]
        right = items[i + 1] if i + 1 < len(items) else ""
        right_td = (
            f'<td width="50%" style="padding:8px 0 8px 8px;color:#e2e8f0;'
            f'font-size:15px;line-height:1.35;">{check}{html.escape(right)}</td>'
            if right else
            '<td width="50%"></td>'
        )
        cells += (
            '<tr>'
            f'<td width="50%" style="padding:8px 8px 8px 0;color:#e2e8f0;'
            f'font-size:15px;line-height:1.35;">{check}{html.escape(left)}</td>'
            f'{right_td}</tr>'
        )
    return (
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'border="0" style="margin:20px 0 22px;"><tr><td style="background:'
        'linear-gradient(135deg,rgba(0,229,255,.16),rgba(20,184,166,.10));'
        'border:1px solid rgba(0,229,255,.22);border-radius:18px;padding:18px;">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'border="0">{cells}</table></td></tr></table>'
    )


def _price_block(price_usd: float, sub: str, badge: str) -> str:
    """Price + assurance-badge row inside a bordered card."""
    price = html.escape(_price_str(price_usd))
    return (
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'border="0" style="margin:0 0 24px;"><tr><td style="background:#020617;'
        'border:1px solid rgba(148,163,184,.18);border-radius:18px;padding:18px;">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'border="0"><tr>'
        '<td style="vertical-align:middle;">'
        f'<div style="font-size:12px;color:{_DIM};text-transform:uppercase;'
        'font-weight:800;letter-spacing:1px;margin-bottom:4px;">Your price</div>'
        f'<div style="font-size:36px;line-height:1;font-weight:950;color:{_INK};">'
        f'{price}</div>'
        f'<div style="font-size:14px;color:{_DIM};margin-top:4px;">{html.escape(sub)}</div>'
        '</td>'
        '<td align="right" style="vertical-align:middle;">'
        '<div style="display:inline-block;background:rgba(0,229,255,.10);'
        'border:1px solid rgba(0,229,255,.30);border-radius:14px;padding:10px 12px;'
        'color:#bae6fd;font-size:13px;font-weight:800;line-height:1.35;">'
        f'{html.escape(badge)}</div></td>'
        '</tr></table></td></tr></table>'
    )


def _cta(buy_link: str, label: str, price_usd: float) -> str:
    price = html.escape(_price_str(price_usd))
    lbl = html.escape(label or "Get started")
    return (
        '<div style="text-align:center;margin:0 0 20px;">'
        f'<a href="{html.escape(buy_link)}" style="display:inline-block;'
        f'background:linear-gradient(135deg,{_CYAN} 0%,{_TEAL} 100%);color:#02111f;'
        'text-decoration:none;font-size:17px;font-weight:950;padding:16px 28px;'
        'border-radius:999px;box-shadow:0 0 28px rgba(0,229,255,.30);">'
        f'{lbl} &mdash; {price} &rarr;</a></div>'
    )


def _footer_html(postal_address: str, unsubscribe_url: str) -> str:
    """CAN-SPAM footer in the vibrant palette (postal address + unsubscribe are
    LEGALLY REQUIRED on commercial mail — never drop them)."""
    return (
        '<tr><td style="padding:18px 26px 24px;background:#020617;'
        'border-top:1px solid rgba(148,163,184,.14);">'
        '<p style="margin:0 0 8px;font-size:12px;line-height:1.5;color:#64748b;'
        'text-align:center;">Questions? Email '
        '<a href="mailto:support@hustleforge.tech" style="color:#7dd3fc;'
        'text-decoration:none;">support@hustleforge.tech</a></p>'
        '<p style="margin:0;font-size:11px;line-height:1.45;color:#475569;'
        'text-align:center;">'
        f'HustleForge LLC &middot; {html.escape(postal_address)}<br>'
        'You are receiving this because you are a publicly listed local business. '
        f'<a href="{html.escape(unsubscribe_url)}" style="color:#64748b;'
        'text-decoration:underline;">Unsubscribe</a>.</p></td></tr>'
    )


def _shell(*, preheader: str, eyebrow: str, headline_html: str, body_html: str,
           footer_html: str) -> str:
    """Wrap the offer-specific body in the vibrant dark card + header + footer.

    ``headline_html`` and ``body_html`` are pre-escaped/assembled by the caller;
    ``eyebrow`` + ``preheader`` are plain text escaped here.
    """
    eb = html.escape(eyebrow) if eyebrow else ""
    eyebrow_html = (
        '<div style="display:inline-block;background:rgba(20,184,166,.14);'
        'border:1px solid rgba(45,212,191,.35);color:#99f6e4;padding:7px 12px;'
        'border-radius:999px;font-size:12px;font-weight:800;letter-spacing:.9px;'
        f'text-transform:uppercase;margin-bottom:16px;">{eb}</div>' if eb else ""
    )
    return (
        f'<body style="margin:0;padding:0;background:{_BG};font-family:'
        "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,"
        f'sans-serif;color:{_INK};">'
        f'{_preheader(preheader)}'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'border="0" style="background:radial-gradient(circle at 15% 10%,'
        'rgba(0,229,255,.22),transparent 30%),radial-gradient(circle at 85% 0%,'
        'rgba(20,184,166,.18),transparent 28%),linear-gradient(135deg,#050816 0%,'
        '#07111f 48%,#020617 100%);margin:0;padding:32px 12px;"><tr>'
        '<td align="center"><table role="presentation" width="100%" '
        'cellspacing="0" cellpadding="0" border="0" style="max-width:640px;'
        'border-collapse:separate;border-spacing:0;background:' + _CARD + ';'
        'border:1px solid rgba(0,229,255,.22);border-radius:24px;overflow:hidden;'
        'box-shadow:0 24px 80px rgba(0,0,0,.45);">'
        # header row
        '<tr><td style="padding:24px 26px 10px;'
        'border-bottom:1px solid rgba(148,163,184,.16);">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'border="0"><tr>'
        '<td style="font-size:22px;font-weight:900;letter-spacing:.4px;'
        f'color:{_INK};"><span style="color:{_CYAN};">Hustle</span>'
        '<span style="color:#ffffff;">Forge</span></td>'
        '<td align="right" style="font-size:11px;font-weight:800;'
        'letter-spacing:1.8px;text-transform:uppercase;color:#7dd3fc;">'
        'Websites &bull; Automation &bull; Results</td>'
        '</tr></table></td></tr>'
        # content row
        '<tr><td style="padding:30px 26px 16px;">'
        f'{eyebrow_html}{headline_html}{body_html}</td></tr>'
        f'{footer_html}'
        '</table></td></tr></table></body>'
    )


def render_flyer_html(
    *,
    company: str,
    first_name: str,
    offer: Offer,
    buy_link: str,
    postal_address: str,
    unsubscribe_url: str,
    stake: str = "",
) -> str:
    """Render the vibrant HTML flyer (dark theme, gradient cards, pill CTA).

    Pure HTML/CSS, table-based, inline styles, NO images/scripts — spam-safe on
    a fresh sender. All user/offer content is HTML-escaped. The plain-text body
    is still sent alongside as the multipart fallback; the CAN-SPAM footer
    (postal address + unsubscribe) is preserved.
    """
    greet = html.escape(first_name.strip() or "there")
    c = html.escape(company or "your business")
    footer = _footer_html(postal_address, unsubscribe_url)

    if offer.kind == "featured":
        headline_txt = offer.headline or offer.label
        headline_html = (
            '<h1 style="margin:0 0 14px;font-size:38px;line-height:1.04;'
            'font-weight:950;letter-spacing:-1.2px;color:#ffffff;">'
            f'{html.escape(headline_txt)}</h1>'
        )
        body_html = (
            f'<p style="margin:0 0 22px;font-size:18px;line-height:1.55;color:{_MUTE};">'
            f'Hi {greet}, {html.escape(offer.pitch)}</p>'
            + _feature_grid(offer.bullets)
            + _price_block(
                offer.price_usd,
                sub="One fixed-scope engagement",
                badge="Delivered in days,<br>not months",
            )
            + _cta(buy_link, offer.cta_label or "Get started", offer.price_usd)
            + (
                '<p style="margin:0;text-align:center;font-size:14px;line-height:1.55;'
                f'color:{_DIM};">{html.escape(offer.assurance)}</p>'
                if offer.assurance else ""
            )
        )
        return _shell(
            preheader=offer.pitch or headline_txt,
            eyebrow=offer.label,
            headline_html=headline_html,
            body_html=body_html,
            footer_html=footer,
        )

    # Matched add-on layout — the "we found X on your site" angle.
    why = html.escape(offer.why)
    label_txt = offer.label
    headline_html = (
        '<h1 style="margin:0 0 14px;font-size:34px;line-height:1.06;'
        'font-weight:950;letter-spacing:-1.1px;color:#ffffff;">'
        f'{html.escape(label_txt)}</h1>'
    )
    stake_line = (
        f'<p style="margin:0 0 16px;font-size:17px;line-height:1.5;color:{_MUTE};">'
        f'{html.escape(stake)}</p>' if stake else ""
    )
    body_html = (
        f'<p style="margin:0 0 18px;font-size:18px;line-height:1.55;color:{_MUTE};">'
        f'Hi {greet},</p>'
        f'{stake_line}'
        f'<p style="margin:0 0 20px;font-size:18px;line-height:1.55;color:{_MUTE};">'
        f'When we looked at <strong style="color:#ffffff;">{c}</strong>, '
        f'we found {why}.</p>'
        + _price_block(
            offer.price_usd,
            sub="Flat one-time price, delivered digitally",
            badge="Done for you.<br>No call required.",
        )
        + _cta(buy_link, "Fix this now", offer.price_usd)
        + (
            '<p style="margin:0;text-align:center;font-size:14px;line-height:1.55;'
            f'color:{_DIM};">If it is not useful, just reply and tell us &mdash; '
            'no hard feelings.</p>'
        )
    )
    return _shell(
        preheader=f"We found {offer.why} on {company or 'your site'}." if offer.why else label_txt,
        eyebrow="Free audit finding",
        headline_html=headline_html,
        body_html=body_html,
        footer_html=footer,
    )


# ---------------------------------------------------------------------------
# ONE reusable flyer attach — every promo send path calls this so no path can
# silently regress to text-only. Accepts a dict row OR an object prospect
# (getattr fallback), renders the vibrant flyer, returns "" on no-offer/fault.
# ---------------------------------------------------------------------------

def flyer_html_for(
    *,
    prospect_id: str,
    to_email: str,
    row: dict | None = None,
    prospect: object | None = None,
    stake: str = "",
    settings: object | None = None,
    featured_sku: str | None = None,
) -> str:
    """Render the vibrant HTML flyer for a prospect, or "" (text-only) on
    no-matching-offer OR any fault.

    Fail-SOFT by contract: a caller wraps ``html_body=flyer_html_for(...)`` and
    the plain-text body still sends when this returns "". Pass EITHER a ``row``
    dict (morning_batch / campaign rows) OR a ``prospect`` object (cash_engine
    ctx.prospect / buying-signal records) — fields resolve from whichever is
    present. The buy-now CTA carries per-prospect Stripe attribution.
    """
    try:
        def _g(key: str, default: str = "") -> object:
            if row is not None:
                v = row.get(key)
                if v not in (None, ""):
                    return v
            if prospect is not None:
                v = getattr(prospect, key, None)
                if v not in (None, ""):
                    return v
            return default

        r = {
            "company_name": str(_g("company_name") or ""),
            "website_url": str(_g("website_url") or ""),
            "owner_email": str(_g("owner_email") or _g("email") or to_email or ""),
            "owner_name": str(_g("owner_name") or ""),
            "security_grade": str(_g("security_grade") or ""),
            "seo_score": _g("seo_score", ""),
            "callsheet_finding": str(_g("callsheet_finding") or _g("finding") or ""),
        }
        offer = match_offer(r, featured_sku=featured_sku)
        if offer is None:
            return ""
        if settings is None:
            from backend.common.config import get_settings
            settings = get_settings()
        return render_flyer_html(
            company=r["company_name"],
            first_name=r["owner_name"],
            offer=offer,
            buy_link=buy_url(offer, prospect_id=prospect_id, email=to_email),
            postal_address=str(getattr(settings, "sender_postal_address", "") or ""),
            unsubscribe_url=str(getattr(settings, "unsubscribe_url", "") or ""),
            stake=str(stake or ""),
        )
    except Exception as exc:  # noqa: BLE001 — flyer is additive; text still sends
        _LOG.warning("flyer_html_for failed for prospect=%s (text still sends): %s",
                     prospect_id, exc)
        return ""


__all__ = ["match_offer", "buy_url", "render_flyer_html", "flyer_html_for", "Offer"]
