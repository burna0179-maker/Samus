"""Headless site generator — Samus owns the frontend.

The frontier path: instead of binding a Wix template's Editor elements (a hard
Wix wall — no API for element IDs), Samus *generates the entire frontend itself*
and Wix is reduced to an optional headless CMS/commerce backend. It composes the
whole pipeline into one deployable, professional artifact:

  * taste              — anti-slop rules + the Pre-Flight audit (fail-closed)
  * design_intelligence — industry palette + Google-Fonts pairing + UI style + effects
  * content            — page copy (operator-authored or content_gen)
  * media              — Gemini hero/section images (+ optional Veo hero video)
  * seo                — LocalBusiness/Organization JSON-LD + meta in <head>

Visual layer (professional by construction): glassmorphism (backdrop-blur nav +
cards), brand gradients + an animated gradient mesh, gradient text, and
scroll-reveal animations via IntersectionObserver (NO scroll-listener jank,
reduced-motion aware, and content stays visible if JS is disabled). Tailwind via
CDN; self-contained single ``index.html``.

The generated HTML is run through the taste audit FAIL-CLOSED. Pure-Python +
deterministic; no network. Dormant/flag-gated via ``website_headless_enabled``.
"""
from __future__ import annotations

import html as _html
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from . import seo_enrich
from .models import WebsiteBrief

_LOG = logging.getLogger("samus.website.site_builder")

_DASH_RE = re.compile("[—–]")
# A site-wide CTA whose words are NOT in the taste duplicate-CTA-intent set.
_CTA = "Get a Free Quote"
_FONT = "Outfit"  # brand-safe default (NOT Inter / Fraunces / Instrument_Serif)

# Cleaning line-icons recreated from the <sample-client> business card
# (spray bottle, bucket/caddy, squeegee, push-broom). stroke=currentColor so
# they inherit the surrounding text color. Reused as service-card icons + a
# hero accent row that echoes the card's icon strip.
def _svg(paths: str) -> str:
    return (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" '
        f'stroke-linecap="round" stroke-linejoin="round" class="w-full h-full">{paths}</svg>'
    )

_ICONS: dict[str, str] = {
    "spray": _svg('<path d="M10 9h4v9.5a1 1 0 0 1-1 1h-2a1 1 0 0 1-1-1z"/><path d="M10 9V6h3v3"/>'
                  '<path d="M13 6h3l1.5 1.5"/><path d="M10 6H7v2"/>'
                  '<path d="M18 5l1.8.6M18 7l1.8-.6M18.4 6h1.8"/>'),
    "bucket": _svg('<path d="M6 9h12l-1.1 9.3a1.4 1.4 0 0 1-1.4 1.2H8.5a1.4 1.4 0 0 1-1.4-1.2z"/>'
                   '<path d="M7 9a5 5 0 0 1 10 0"/>'),
    "squeegee": _svg('<path d="M12 3v10"/><path d="M6 13h12"/><path d="M7.5 13l1 4h7l1-4"/>'),
    "broom": _svg('<path d="M12 3v11"/><path d="M6 14h12l1 4H5z"/>'
                  '<path d="M8 18v2M10.5 18v2.6M12 18v3M13.5 18v2.6M16 18v2"/>'),
}
_ICON_ORDER = ("spray", "bucket", "squeegee", "broom")


@dataclass
class GeneratedSite:
    files: dict[str, str] = field(default_factory=dict)
    taste_audit: dict[str, Any] = field(default_factory=dict)
    pages: list[str] = field(default_factory=list)
    sanitized: bool = False
    warnings: list[str] = field(default_factory=list)
    design_system: dict[str, Any] | None = None
    compliance: dict[str, Any] | None = None   # jurisdiction + legal obligations report

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": {name: len(content) for name, content in self.files.items()},
            "taste_audit": self.taste_audit,
            "pages": list(self.pages),
            "sanitized": self.sanitized,
            "warnings": list(self.warnings),
            "design_system": self.design_system,
            "compliance": self.compliance,
        }


def _fix_mojibake(text: str) -> str:
    """Repair UTF-8-misread-as-cp1252 sequences (e.g. an em-dash showing as 'â€"')."""
    if "Ã" in text or "â€" in text or "Â" in text:
        try:
            return text.encode("cp1252", "strict").decode("utf-8", "strict")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return text
    return text


def _clean(text: str) -> str:
    text = _DASH_RE.sub(" - ", _fix_mojibake(text or ""))
    return " ".join(text.split())


def _esc(text: str) -> str:
    return _html.escape(_clean(text), quote=True)


def _safe_url(url: str) -> str:
    """Allow only http(s)/mailto/tel and relative URLs; drop everything else
    (javascript:, data:, vbscript: ...). Returns "" when unsafe so the caller
    omits the link/attribute. Apply BEFORE _esc()."""
    u = (url or "").strip()
    if not u:
        return ""
    low = u.lower()
    if low.startswith(("http://", "https://", "mailto:", "tel:", "/", "#", "./")):
        return u
    return ""


def _content_by_slug(brief: WebsiteBrief) -> dict[str, dict[str, str]]:
    return {p.slug.lower(): dict(p.content) for p in brief.pages}


# --------------------------------------------------------------------------
# section renderers (glassmorphism + gradients + scroll-reveal)
# --------------------------------------------------------------------------

def _tel_href(phone: str) -> str:
    """A safe tel: href for a display phone number, or "" when unusable."""
    digits = re.sub(r"[^0-9+]", "", phone or "")
    return _safe_url(f"tel:{digits}") if digits else ""


def _nav(name: str, links: list[tuple[str, str]], *, home: bool = False, phone: str = "") -> str:
    """Slim glass nav with page links. On the homepage the giant 3D title is the
    brand, so the nav shows only links + Quote (no redundant name banner).
    When ``phone`` is set, a click-to-call tel: link rides in the header — the
    contact-prominence rule for service businesses (mobile callers especially)."""
    linkhtml = "".join(
        f'<a href="{href}" class="text-white/80 hover:text-white transition">{_esc(label)}</a>'
        for label, href in links
    )
    links_div = f'<div class="hidden md:flex items-center gap-6 text-sm">{linkhtml}</div>'
    left = links_div if home else f'<a href="/" class="font-semibold tracking-tight text-white">{_esc(name)}</a>'
    mid = "" if home else links_div
    tel = ""
    href = _tel_href(phone)
    if href:
        tel = (f'<a href="{_esc(href)}" class="text-sm font-semibold text-white/90 hover:text-white '
               f'whitespace-nowrap">&#9742; {_esc(phone)}</a>')
    quote = ('<a href="/#contact" data-quote class="rounded-full px-4 py-2 text-sm font-semibold text-slate-900" '
             'style="background:linear-gradient(90deg,var(--accent2),var(--accent))">Quote</a>')
    right = f'<div class="flex items-center gap-4">{tel}{quote}</div>'
    return (
        '<header class="fixed top-0 inset-x-0 z-30">'
        '<nav class="glass mx-auto mt-4 max-w-6xl rounded-full bg-slate-900/40 border border-white/15 '
        'px-5 h-14 flex items-center justify-between text-white shadow-lg shadow-black/10">'
        f'{left}{mid}{right}</nav></header>'
    )


def _cta_buttons(scheduling_url: str, phone: str = "") -> str:
    """Primary CTA (opens the quote modal) + optional click-to-call + Schedule Now."""
    quote = (
        '<a href="#contact" data-quote class="inline-block rounded-full px-8 py-4 font-semibold '
        'text-slate-900 shadow-lg transition hover:-translate-y-0.5 hover:shadow-xl" '
        'style="background:linear-gradient(90deg,var(--accent2),var(--accent))">' + _CTA + '</a>'
    )
    call = ""
    _th = _tel_href(phone)
    if _th:
        call = (
            f'<a href="{_esc(_th)}" class="inline-block rounded-full px-8 py-4 font-semibold text-white '
            f'border border-white/40 transition hover:bg-white/10">Call {_esc(phone)}</a>'
        )
    sched = ""
    _su = _safe_url(scheduling_url)  # WEB-D8-01 — drop javascript:/data: schemes
    if _su:
        sched = (
            f'<a href="{_esc(_su)}" target="_blank" rel="noopener" '
            'class="inline-block rounded-full px-8 py-4 font-semibold text-white border border-white/40 '
            'transition hover:bg-white/10">Schedule Now</a>'
        )
    return f'<div class="flex flex-wrap gap-4 justify-center">{quote}{call}{sched}</div>'


def _hero(name: str, headline: str, intro: str, hero_media: str, hero_video: str,
          accent: str, accent2: str, cta_html: str) -> str:
    # Background pushed DOWN (object-position lower) to leave headroom up top for
    # the 3D animated company name — replaces the redundant name/CTA nav banner.
    _hv = _safe_url(hero_video)  # WEB-D8-01 — drop javascript:/data: schemes
    _hm = _safe_url(hero_media)
    if _hv:
        bg = (
            '<video autoplay muted loop playsinline class="absolute inset-0 w-full h-full object-cover" '
            'style="object-position:center 80%">'
            f'<source src="{_esc(_hv)}" type="video/mp4"></video>'
        )
    elif _hm:
        bg = (f'<img src="{_esc(_hm)}" alt="{_esc(name)}" '
              'class="absolute inset-0 w-full h-full object-cover" style="object-position:center 82%">')
    else:
        bg = '<div class="absolute inset-0" style="background:linear-gradient(135deg,var(--accent),#0b2f35)"></div>'

    icons = "".join(
        f'<span class="hero-ico inline-block h-8 w-8 md:h-10 md:w-10 text-white/85" '
        f'style="animation-delay:{i * 0.4:.1f}s">{_ICONS[k]}</span>'
        for i, k in enumerate(_ICON_ORDER)
    )
    return (
        '<section class="relative min-h-[100dvh] overflow-hidden">'
        f'{bg}'
        # lighter scrim: enough contrast for the title/card, but the video reads clearly
        '<div class="absolute inset-0 bg-gradient-to-b from-slate-950/60 via-slate-900/10 to-slate-950/55"></div>'
        '<div class="mesh absolute inset-0 opacity-50" style="background:'
        'radial-gradient(40rem 40rem at 20% 15%,var(--accent)40,transparent 60%),'
        'radial-gradient(36rem 36rem at 85% 70%,var(--accent2)33,transparent 60%)"></div>'
        '<div class="relative z-10 min-h-[100dvh] flex flex-col items-center px-6">'
        # headroom: icon strip + 3D animated company name
        '<div class="pt-24 md:pt-28 text-center">'
        f'<div class="flex justify-center gap-5 md:gap-8 mb-7">{icons}</div>'
        f'<div class="hero3d"><h1 class="name3d">{_esc(name)}</h1></div>'
        '</div>'
        # value-prop glass card near the bottom (tagline + CTA, not the name)
        '<div class="mt-auto mb-14 w-full max-w-2xl">'
        # mobile: extra-transparent so the video reads through the card; the glass
        # backdrop-blur keeps the text legible. Desktop keeps a touch more contrast.
        '<div class="glass reveal rounded-3xl bg-slate-950/10 md:bg-slate-950/25 border border-white/15 p-6 md:p-10 text-center shadow-2xl">'
        f'<p class="text-2xl md:text-3xl font-semibold text-white leading-snug drop-shadow">{_esc(headline)}</p>'
        f'<p class="mt-3 text-white/85 max-w-[46ch] mx-auto">{_esc(intro)}</p>'
        f'<div class="mt-8">{cta_html}</div></div></div>'
        '</div></section>'
    )


def _services(name: str, heading: str, body: str, items: list[str], section_img: str) -> str:
    if items:
        cards = "".join(
            ('<div class="reveal glass rounded-2xl bg-white/70 border border-slate-200/70 p-6 shadow-sm '
             'transition duration-300 hover:-translate-y-1.5 hover:shadow-xl" style="--d:%dms">'
             '<div class="h-12 w-12 rounded-xl flex items-center justify-center p-2.5 text-white" '
             'style="background:linear-gradient(135deg,var(--accent),var(--accent2))">%s</div>'
             '<h3 class="mt-4 text-lg font-semibold text-slate-900">%s</h3></div>')
            % (i * 80, _ICONS[_ICON_ORDER[i % len(_ICON_ORDER)]], _esc(it))
            for i, it in enumerate(items)
        )
        grid = f'<div class="stagger mt-12 grid sm:grid-cols-2 lg:grid-cols-3 gap-6">{cards}</div>'
    else:
        grid = ""
    _si = _safe_url(section_img)  # WEB-D8-01 — drop javascript:/data: schemes
    img = (
        f'<div class="reveal mt-12 overflow-hidden rounded-3xl shadow-xl">'
        f'<img src="{_esc(_si)}" alt="{_esc(name)} team" loading="lazy" '
        f'class="w-full h-72 md:h-96 object-cover transition duration-700 hover:scale-[1.03]"></div>'
        if _si else ""
    )
    return (
        '<section id="services" class="relative py-24 md:py-32 px-6 bg-gradient-to-b from-slate-50 to-white">'
        '<div class="mx-auto max-w-6xl">'
        '<div class="reveal max-w-2xl">'
        f'<h2 class="text-3xl md:text-5xl font-semibold tracking-tight grad-text inline-block">{_esc(heading)}</h2>'
        f'<p class="mt-4 text-lg text-slate-600 max-w-[60ch]">{_esc(body)}</p></div>'
        f'{grid}{img}</div></section>'
    )


def _trust_strip(items: list[str]) -> str:
    """Glass 'pills' conveying an established, credible operation."""
    if not items:
        return ""
    pills = "".join(
        '<span class="reveal glass rounded-full bg-white/70 border border-slate-200/70 px-5 py-2 '
        f'text-sm font-medium text-slate-700 shadow-sm">{_esc(t)}</span>' for t in items
    )
    return (
        '<section class="relative -mt-2 px-6"><div class="mx-auto max-w-6xl flex flex-wrap justify-center '
        f'gap-3 py-8">{pills}</div></section>'
    )


def _promo_band(text: str, phone: str = "") -> str:
    """A full-width highlighted promotion + easy-action band ('Free estimates -
    call today'). Renders ONLY when the brief supplies a ``promo`` line on the
    home page — inert for briefs that don't opt in."""
    if not text:
        return ""
    _th = _tel_href(phone)
    if _th:
        btn = (f'<a href="{_esc(_th)}" class="inline-block rounded-full bg-white px-7 py-3 font-semibold '
               f'text-slate-900 shadow-md transition hover:-translate-y-0.5">Call {_esc(phone)}</a>')
    else:
        btn = ('<a href="/#contact" data-quote class="inline-block rounded-full bg-white px-7 py-3 '
               'font-semibold text-slate-900 shadow-md transition hover:-translate-y-0.5">' + _CTA + '</a>')
    return (
        '<section id="promo" class="px-6 py-10" '
        'style="background:linear-gradient(90deg,var(--accent),var(--accent2))">'
        '<div class="mx-auto max-w-6xl flex flex-col sm:flex-row items-center justify-center gap-6 text-center">'
        f'<p class="text-xl md:text-2xl font-semibold text-white drop-shadow">{_esc(text)}</p>'
        f'{btn}</div></section>'
    )


def _placeholder_card(label: str, text: str) -> str:
    """A tasteful, clearly-labeled demo placeholder — shows the prospect what a
    section does WITHOUT inventing testimonials/licenses/photos (honesty rule)."""
    return (
        '<div class="reveal rounded-2xl border border-dashed border-slate-300 p-8 text-center">'
        f'<span class="inline-block rounded-full bg-slate-100 px-3 py-1 text-xs font-medium uppercase '
        f'tracking-wide text-slate-500">{_esc(label)}</span>'
        f'<p class="mt-3 text-slate-600">{_esc(text)}</p></div>'
    )


def _social_proof(trust_line: str, reviews_ph: str, credentials_ph: str) -> str:
    """Testimonials & credentials display. The ONLY hard claim rendered is the
    real Google-review trust line; testimonials/license content the owner must
    supply renders as labeled placeholders. Empty inputs -> no section."""
    if not (trust_line or reviews_ph or credentials_ph):
        return ""
    blocks: list[str] = []
    if trust_line:
        blocks.append(
            '<div class="reveal rounded-2xl bg-white border border-slate-200/70 p-8 text-center shadow-sm">'
            '<div class="text-2xl" style="color:var(--accent2)">&#9733;&#9733;&#9733;&#9733;&#9733;</div>'
            f'<p class="mt-3 text-xl font-semibold text-slate-900">{_esc(trust_line)}</p></div>'
        )
    if reviews_ph:
        blocks.append(_placeholder_card("Demo preview", reviews_ph))
    if credentials_ph:
        blocks.append(_placeholder_card("Demo preview", credentials_ph))
    return (
        '<section id="reviews" class="py-24 px-6 bg-slate-50">'
        '<div class="mx-auto max-w-4xl">'
        '<h2 class="reveal text-3xl md:text-4xl font-semibold tracking-tight text-slate-900 text-center">'
        'Trusted by your neighbors</h2>'
        f'<div class="stagger mt-10 grid gap-6">{"".join(blocks)}</div></div></section>'
    )


def _portfolio(placeholder: str) -> str:
    """Portfolio / case-studies section — a labeled placeholder until the owner
    supplies real project material (never stock-faked). Empty -> no section."""
    if not placeholder:
        return ""
    return (
        '<section id="portfolio" class="py-24 px-6">'
        '<div class="mx-auto max-w-4xl">'
        '<h2 class="reveal text-3xl md:text-4xl font-semibold tracking-tight text-slate-900 text-center">'
        'Our Work</h2>'
        f'<div class="mt-10">{_placeholder_card("Demo preview", placeholder)}</div></div></section>'
    )


def _about(name: str, body: str) -> str:
    return (
        '<section id="about" class="py-24 md:py-32 px-6">'
        '<div class="reveal mx-auto max-w-3xl text-center">'
        '<div class="mx-auto mb-6 h-1 w-16 rounded-full" '
        'style="background:linear-gradient(90deg,var(--accent),var(--accent2))"></div>'
        f'<h2 class="text-3xl md:text-4xl font-semibold tracking-tight text-slate-900">About {_esc(name)}</h2>'
        f'<p class="mt-5 text-lg leading-relaxed text-slate-600">{_esc(body)}</p></div></section>'
    )


def _contact(name: str, body: str, phone: str, email: str, address: str, cta_html: str) -> str:
    def _wrap(v: str) -> str:
        # Let long values wrap cleanly on narrow screens. For an email, add
        # <wbr> break opportunities after the "@" and dots so it breaks at
        # natural boundaries instead of mid-word (the "malformed on mobile" look).
        esc = _esc(v)
        if "@" in v:
            esc = esc.replace("@", "@<wbr>").replace(".", ".<wbr>")
        return esc

    contact_items = []
    if phone:
        digits = re.sub(r"[^0-9+]", "", phone)
        sms_href = _esc(_safe_url(f"sms:{digits}"))
        tel_href = _esc(_safe_url(f"tel:{digits}"))
        contact_items.append(
            f'<span class="flex flex-col items-center gap-1">'
            f'<a href="{sms_href}" class="text-white/90 break-words max-w-full font-semibold hover:text-white">{_esc(phone)}</a>'
            f'<span class="inline-flex items-center gap-1 rounded-full bg-white/15 px-2.5 py-0.5 text-xs font-medium text-white/80">'
            f'&#128172; Text preferred &mdash; <a href="{tel_href}" class="underline underline-offset-2 hover:text-white">call also OK</a>'
            f'</span></span>'
        )
    if email:
        contact_items.append(f'<span class="text-white/90 break-words max-w-full">{_wrap(email)}</span>')
    if address:
        contact_items.append(f'<span class="text-white/90 break-words max-w-full">{_wrap(address)}</span>')
    bits = "".join(contact_items)
    return (
        '<section id="contact" class="relative py-24 md:py-32 px-6 overflow-hidden '
        'bg-gradient-to-br from-slate-950 to-slate-900">'
        '<div class="mesh absolute inset-0 opacity-50" style="background:'
        'radial-gradient(40rem 40rem at 80% 20%,var(--accent)40,transparent 60%),'
        'radial-gradient(36rem 36rem at 10% 90%,var(--accent2)33,transparent 60%)"></div>'
        '<div class="relative z-10 reveal glass mx-auto max-w-3xl rounded-3xl bg-white/5 border border-white/15 '
        'p-10 md:p-14 text-center shadow-2xl">'
        f'<h2 class="text-3xl md:text-4xl font-semibold tracking-tight text-white">Reach {_esc(name)}</h2>'
        f'<p class="mt-4 text-lg text-white/80 max-w-[52ch] mx-auto break-words">{_esc(body)}</p>'
        f'<div class="mt-8 flex flex-col sm:flex-row sm:flex-wrap justify-center items-center gap-x-8 gap-y-2 px-2">{bits}</div>'
        f'<div class="mt-10">{cta_html}</div></div></section>'
    )


_INPUT = ("w-full rounded-lg border border-slate-300 px-4 py-2.5 text-slate-900 "
          "focus:outline-none focus:ring-2 focus:ring-[var(--accent)]")


def _quote_modal(brief: Any, services_list: list[str]) -> str:
    """Onboarding/quote dialog. Captures the lead + business LOCATION (which feeds
    Samus's jurisdiction legal-template generation) and POSTs to the intake DB."""
    svc = "".join(
        f'<label class="flex items-center gap-2 text-sm text-slate-600">'
        f'<input type="checkbox" name="services" value="{_esc(s)}"> {_esc(s)}</label>'
        for s in services_list
    )
    svc_block = (
        '<fieldset class="rounded-lg border border-slate-200 p-3"><legend class="px-1 text-sm text-slate-500">'
        f'Services needed</legend><div class="mt-1 grid grid-cols-2 gap-2">{svc}</div></fieldset>'
    ) if svc else ""
    return (
        '<dialog id="quoteModal" class="w-[92vw] max-w-lg rounded-2xl p-0 backdrop:bg-slate-950/60">'
        f'<form id="quoteForm" data-endpoint="{_esc(brief.intake_endpoint)}" '
        f'data-email="{_esc(brief.contact_email)}" class="p-7 md:p-8">'
        '<div class="flex items-center justify-between">'
        '<h3 class="text-xl font-semibold text-slate-900">Request a Free Quote</h3>'
        '<button type="button" data-close class="text-2xl leading-none text-slate-400 hover:text-slate-700">&times;</button></div>'
        '<div class="mt-5 grid gap-3">'
        f'<input name="name" required placeholder="Your name" class="{_INPUT}">'
        f'<input name="business" placeholder="Business name (optional)" class="{_INPUT}">'
        f'<input name="email" type="email" required placeholder="Email" class="{_INPUT}">'
        f'<input name="phone" type="tel" placeholder="Phone" class="{_INPUT}">'
        f'<input name="city" placeholder="City" class="{_INPUT}">'
        '<div class="grid grid-cols-2 gap-3">'
        f'<input name="state" placeholder="State / Province" class="{_INPUT}">'
        f'<input name="country" value="US" placeholder="Country" class="{_INPUT}"></div>'
        f'{svc_block}'
        f'<textarea name="message" rows="3" placeholder="Tell us about the job" class="{_INPUT}"></textarea>'
        '</div>'
        '<button type="submit" class="mt-6 w-full rounded-full px-6 py-3 font-semibold text-slate-900 '
        'transition hover:brightness-110" style="background:linear-gradient(90deg,var(--accent2),var(--accent))">'
        'Send Request</button>'
        '<p id="quoteThanks" class="hidden mt-4 text-center font-medium text-emerald-700">'
        'Thank you. We will be in touch shortly.</p>'
        '</form></dialog>'
    )


def build_static_site(
    brief: WebsiteBrief, *, settings: Any = None, media: dict[str, str] | None = None,
    public_url: str = "",
) -> GeneratedSite:
    """Generate a self-contained, professional, taste-governed static site."""
    media = media or {}
    name = brief.business_name

    # --- industry design intelligence (fonts + palette + anti-patterns) ----
    di_enabled = True if settings is None else bool(getattr(settings, "website_design_intelligence_enabled", True))
    ds = None
    heading_font, body_font = _FONT, _FONT
    font_import = (
        '<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800'
        '&display=swap" rel="stylesheet">'
    )
    if di_enabled:
        try:
            from . import design_intelligence as _di
            ds = _di.recommend(brief.industry, brief.business_description)
            typ = ds.typography or {}
            if typ.get("heading") and typ.get("css_import"):
                heading_font, body_font = typ["heading"], typ["body"]
                font_import = f"<style>{typ['css_import']}</style>"
        except Exception as exc:  # noqa: BLE001 — enrichment, never blocks generation
            _LOG.warning("design intelligence failed; using defaults: %s", exc)
            ds = None

    if brief.brand_colors:
        colors = brief.brand_colors
    elif ds and ds.palette.get("primary"):
        colors = [ds.palette["primary"], ds.palette.get("accent", ds.palette["primary"])]
    else:
        colors = ["#0E7C8B", "#5CB544", "#F2EC4F"]
    accent, accent2 = colors[0], (colors[1] if len(colors) > 1 else colors[0])

    content = _content_by_slug(brief)
    home, about = content.get("home", {}), content.get("about", {})
    services, contact = content.get("services", {}), content.get("contact", {})

    seo = seo_enrich.build_seo_package(brief, public_url=public_url)
    home_meta = seo["page_meta"].get("home") or next(iter(seo["page_meta"].values()), {})
    title = home_meta.get("title", name)
    description = home_meta.get("description", brief.business_description)

    headline = home.get("headline") or name
    intro = home.get("intro") or home.get("subheadline") or brief.business_description
    hero_media, hero_video = media.get("hero_image", ""), media.get("hero_video", "")
    section_img = media.get("section_image", "")
    og_img = media.get("og_image", hero_media)
    services_list = [s.strip() for s in re.split(r"[|,]", services.get("list", "")) if s.strip()]

    # --- sub-pages: jurisdiction-aware legal + marketing depth -------------
    # Each entry: {file,title,description,content,head,main_class}. Rendered
    # below through the shared sub-page chrome (nav + footer + quote modal).
    sub_pages: list[dict[str, Any]] = []
    legal_link_html = ""
    legal_enabled = True if settings is None else bool(getattr(settings, "website_legal_pages_enabled", True))
    compliance: dict[str, Any] | None = None
    if legal_enabled:
        try:
            from . import legal as _legal
            country = (brief.registered_country or "US").strip()
            state = (brief.registered_state or _derive_state(brief.address)).strip()
            juris = _legal.resolve_jurisdiction(country, state)
            compliance = _legal.compliance_report(brief, juris)
            for fname, ltitle, lcontent in (
                ("privacy.html", "Privacy Policy", _legal.privacy_policy_html(brief, juris)),
                ("terms.html", "Terms & Conditions", _legal.terms_html(brief, juris)),
                ("disclaimer.html", "Disclaimer", _legal.disclaimer_html(brief, juris)),
            ):
                sub_pages.append({
                    "file": fname, "title": f"{ltitle} | {name}",
                    "description": f"{ltitle} for {name}.", "content": lcontent,
                    "head": "", "main_class": "max-w-3xl",
                })
            legal_link_html = (
                '<a href="/privacy" class="hover:text-white transition">Privacy Policy</a>'
                '<span class="mx-3 text-slate-600">|</span>'
                '<a href="/terms" class="hover:text-white transition">Terms</a>'
                '<span class="mx-3 text-slate-600">|</span>'
                '<a href="/disclaimer" class="hover:text-white transition">Disclaimer</a>'
            )
        except Exception as exc:  # noqa: BLE001 — legal is additive, never blocks
            _LOG.warning("legal page generation failed: %s", exc)
            sub_pages, compliance, legal_link_html = [], None, ""

    # --- marketing depth: Services / Gallery / FAQ / Reviews / areas -------
    multipage = True if settings is None else bool(getattr(settings, "website_multipage_enabled", True))
    nav_links: list[tuple[str, str]] = []
    area_links: list[tuple[str, str]] = []
    if multipage:
        try:
            from . import pages as _pages
            for p in _pages.build_marketing_pages(brief, media=media):
                href = _clean_href(p["file"])
                if p.get("area"):
                    area_links.append((p["label"], href))
                else:
                    nav_links.append((p["label"], href))
                sub_pages.append({
                    "file": p["file"], "title": p["title"],
                    "description": _page_desc(p["label"], name) if not p.get("area")
                    else p["title"].split(" | ")[0],
                    "content": p["content"], "head": p.get("head", ""),
                    "main_class": "max-w-4xl" if p.get("area") else "max-w-6xl",
                })
        except Exception as exc:  # noqa: BLE001 — marketing pages are additive
            _LOG.warning("marketing page generation failed: %s", exc)
            nav_links, area_links = [], []
    # ≤2-click rule: Services / About / Contact must all be reachable from the
    # header. Marketing pages usually supply a Services link; when they don't
    # (multipage off / page-gen failure) fall back to the homepage anchor.
    if not any(label == "Services" for label, _ in nav_links):
        nav_links.insert(0, ("Services", "/#services"))
    nav_links.append(("About", "/#about"))
    nav_links.append(("Contact", "/#contact"))

    # Footer: page links + service-area links + legal.
    _foot = [f'<a href="{h}" class="hover:text-white transition">{_esc(l)}</a>' for l, h in nav_links]
    _foot += [f'<a href="{h}" class="hover:text-white transition">{_esc(l)}</a>' for l, h in area_links]
    footer_links = '<span class="mx-2 text-slate-600">&middot;</span>'.join(_foot)
    if legal_link_html:
        if footer_links:
            footer_links += '<span class="mx-3 text-slate-600">|</span>'
        footer_links += legal_link_html

    # Trust pills (established/credible feel) derived from the brief's real facts.
    _dl = (brief.business_description + " " + (about.get("body") or "")).lower()
    trust: list[str] = []
    if "family" in _dl:
        trust.append("Family Owned & Operated")
    if "commercial" in _dl and "residential" in _dl:
        trust.append("Commercial & Residential")
    if "airbnb" in _dl:
        trust.append("Airbnb Turnovers")
    _yrs = re.search(r"(\d+)\+?\s*years", _dl)
    if _yrs:
        trust.append(f"{_yrs.group(1)}+ Years Experience")

    cta_html = _cta_buttons(brief.scheduling_url, brief.contact_phone)
    body_html = (
        _hero(name, headline, intro, hero_media, hero_video, accent, accent2, cta_html)
        + _trust_strip(trust)
        + _promo_band(home.get("promo", ""), brief.contact_phone)
        + _services(name, services.get("heading") or "What we do", services.get("body") or "",
                    services_list, section_img)
        + _portfolio(home.get("portfolio_placeholder", ""))
        + _about(name, about.get("body") or "")
        + _social_proof(home.get("trust_line", ""), home.get("testimonials_placeholder", ""),
                        home.get("credentials_placeholder", ""))
        + _contact(name, contact.get("body") or "", brief.contact_phone, brief.contact_email,
                   brief.address, cta_html)
    )
    # Homepage nav shows page LINKS only (the 3D animated name is the header —
    # the operator flagged a name+CTA banner as redundant with the hero).
    modal_html = _quote_modal(brief, services_list)
    page = _PAGE_TEMPLATE.format(
        lang="en", title=_esc(title), description=_esc(description),
        font_import=font_import, heading_font=heading_font, body_font=body_font,
        accent=accent, accent2=accent2, og_img=_esc(_safe_url(og_img)), jsonld=seo["jsonld_script"],
        name=_esc(name), nav=_nav(name, nav_links, home=True, phone=brief.contact_phone),
        body=body_html,
        footer_links=footer_links, modal=modal_html,
    )

    from backend.taste.audit import audit_text

    def _audit_fix(h: str) -> tuple[str, Any, bool]:
        r = audit_text(h, kind="frontend", section_count=4)
        if r.passed:
            return h, r, False
        h = _DASH_RE.sub(" - ", h)
        return h, audit_text(h, kind="frontend", section_count=4), True

    page, result, sanitized = _audit_fix(page)
    files: dict[str, str] = {"index.html": page}
    pages = ["index.html"]
    subnav = _nav(name, nav_links, home=False, phone=brief.contact_phone)
    for sp in sub_pages:
        rendered = _SUBPAGE_TEMPLATE.format(
            lang="en", title=_esc(sp["title"]), description=_esc(sp["description"]),
            font_import=font_import, heading_font=heading_font, body_font=body_font,
            accent=accent, accent2=accent2, name=_esc(name), nav=subnav,
            content=sp["content"], head=sp.get("head", ""), modal=modal_html,
            footer_links=footer_links, main_class=sp.get("main_class", "max-w-3xl"),
        )
        rendered, _r, rsan = _audit_fix(rendered)
        files[sp["file"]] = rendered
        pages.append(sp["file"])
        sanitized = sanitized or rsan

    warnings: list[str] = []
    if not hero_media and not hero_video:
        warnings.append("no hero media supplied — used a brand gradient (run media_gen for a real hero)")

    return GeneratedSite(
        files=files, taste_audit=result.to_dict(), pages=pages,
        sanitized=sanitized, warnings=warnings,
        design_system=ds.to_dict() if ds else None, compliance=compliance,
    )


def _derive_state(address: str) -> str:
    """Best-effort US state code from a 'City, ST ZIP' address."""
    m = re.search(r"\b([A-Z]{2})\b\s+\d{5}", address or "")
    return m.group(1) if m else ""


def _clean_href(file: str) -> str:
    """A sub-page file path -> its clean public URL (Cloudflare drops .html)."""
    f = file[:-5] if file.endswith(".html") else file
    return "/" + f


def _page_desc(label: str, name: str) -> str:
    """A focused meta description per marketing page (SEO)."""
    return {
        "Services": f"Cleaning services from {name} - deep cleans, recurring service, rental turnovers, and commercial cleaning.",
        "Gallery": f"See the standard {name} brings to every home and business.",
        "FAQ": f"Answers to the questions clients ask most about {name}'s cleaning services.",
        "Reviews": f"What clients say about working with {name}.",
        "Careers": f"Join the {name} team - we're hiring dependable, detail-minded cleaners. Apply today.",
    }.get(label, name)


_PAGE_TEMPLATE = """<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{description}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{description}">
<meta property="og:image" content="{og_img}">
<meta property="og:type" content="website">
<script>document.documentElement.classList.add('js')</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
{font_import}
<script src="https://cdn.tailwindcss.com"></script>
<style>
:root{{--accent:{accent};--accent2:{accent2}}}
body{{font-family:'{body_font}',system-ui,sans-serif}}
h1,h2,h3,header nav span{{font-family:'{heading_font}','{body_font}',system-ui,sans-serif}}
.glass{{backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}}
.grad-text{{background:linear-gradient(90deg,var(--accent),var(--accent2));-webkit-background-clip:text;background-clip:text;color:transparent}}
/* 3D animated company name (the homepage header) */
.hero3d{{perspective:900px}}
.name3d{{font-size:clamp(2.25rem,7vw,5.5rem);font-weight:800;letter-spacing:-.02em;line-height:1.05;color:#fff;transform-style:preserve-3d;animation:float3d 7s ease-in-out infinite;text-shadow:0 1px 0 rgba(255,255,255,.45),0 2px 0 var(--accent2),0 4px 0 var(--accent),0 8px 16px rgba(0,0,0,.45),0 18px 36px rgba(0,0,0,.4)}}
@keyframes float3d{{0%,100%{{transform:rotateX(13deg) rotateY(-7deg) translateY(0)}}50%{{transform:rotateX(13deg) rotateY(7deg) translateY(-10px)}}}}
.hero-ico{{animation:icofloat 4.5s ease-in-out infinite}}
@keyframes icofloat{{0%,100%{{transform:translateY(0)}}50%{{transform:translateY(-8px)}}}}
@keyframes meshmove{{0%{{transform:translate3d(0,0,0) scale(1)}}50%{{transform:translate3d(-3%,-2%,0) scale(1.05)}}100%{{transform:translate3d(0,0,0) scale(1)}}}}
.mesh{{animation:meshmove 20s ease-in-out infinite}}
.js .reveal{{opacity:0;transform:translateY(28px)}}
.reveal{{transition:opacity .7s cubic-bezier(.16,1,.3,1),transform .7s cubic-bezier(.16,1,.3,1)}}
.reveal.in{{opacity:1;transform:none}}
.stagger>*{{transition-delay:var(--d,0ms)}}
@media (prefers-reduced-motion:reduce){{.reveal,.mesh,.name3d,.hero-ico{{opacity:1!important;transform:none!important;animation:none!important;transition:none!important}}}}
html{{scroll-behavior:smooth}}
</style>
{jsonld}
</head>
<body class="bg-white text-slate-900 antialiased">
{nav}
{body}
{modal}
<footer class="py-10 px-6 text-center text-sm text-slate-400 bg-slate-950">
<div class="space-x-1">{footer_links}</div>
<div class="mt-3">{name}</div></footer>
<script>
const io=new IntersectionObserver((es)=>es.forEach(e=>{{if(e.isIntersecting){{e.target.classList.add('in');io.unobserve(e.target)}}}}),{{threshold:.15}});
document.querySelectorAll('.reveal').forEach(el=>io.observe(el));
const qm=document.getElementById('quoteModal');
if(qm){{
 document.querySelectorAll('[data-quote]').forEach(b=>b.addEventListener('click',e=>{{e.preventDefault();qm.showModal()}}));
 qm.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>qm.close()));
 qm.addEventListener('click',e=>{{if(e.target===qm)qm.close()}});
 const f=document.getElementById('quoteForm');
 f.addEventListener('submit',async(e)=>{{
  e.preventDefault();
  const data={{}};new FormData(f).forEach((v,k)=>{{if(data[k]){{data[k]=[].concat(data[k],v)}}else{{data[k]=v}}}});
  const ep=f.dataset.endpoint;
  if(ep){{try{{const r=await fetch(ep,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(data)}});if(r.ok){{document.getElementById('quoteThanks').classList.remove('hidden');f.reset();return}}}}catch(_){{}}}}
  const subj=encodeURIComponent('Quote request: '+(data.business||data.name||''));
  const body=encodeURIComponent(Object.entries(data).map(([k,v])=>k+': '+v).join(String.fromCharCode(10)));
  window.location.href='mailto:'+f.dataset.email+'?subject='+subj+'&body='+body;
 }});
}}
</script>
</body>
</html>"""


_SUBPAGE_TEMPLATE = """<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{description}">
<meta name="robots" content="index,follow">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{description}">
<meta property="og:type" content="website">
<script>document.documentElement.classList.add('js')</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
{font_import}
<script src="https://cdn.tailwindcss.com"></script>
<style>
:root{{--accent:{accent};--accent2:{accent2}}}
body{{font-family:'{body_font}',system-ui,sans-serif}}
h1,h2,h3,header nav span{{font-family:'{heading_font}','{body_font}',system-ui,sans-serif}}
.glass{{backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}}
.grad-text{{background:linear-gradient(90deg,var(--accent),var(--accent2));-webkit-background-clip:text;background-clip:text;color:transparent}}
.js .reveal{{opacity:0;transform:translateY(28px)}}
.reveal{{transition:opacity .7s cubic-bezier(.16,1,.3,1),transform .7s cubic-bezier(.16,1,.3,1)}}
.reveal.in{{opacity:1;transform:none}}
.stagger>*{{transition-delay:var(--d,0ms)}}
@media (prefers-reduced-motion:reduce){{.reveal{{opacity:1!important;transform:none!important;transition:none!important}}}}
html{{scroll-behavior:smooth}}
</style>
{head}
</head>
<body class="bg-white text-slate-900 antialiased">
{nav}
<main class="mx-auto {main_class} px-6 pt-32 pb-20">{content}</main>
{modal}
<footer class="py-10 px-6 text-center text-sm text-slate-400 bg-slate-950">
<div class="space-x-1">{footer_links}</div>
<div class="mt-3">{name}</div></footer>
<script>
const io=new IntersectionObserver((es)=>es.forEach(e=>{{if(e.isIntersecting){{e.target.classList.add('in');io.unobserve(e.target)}}}}),{{threshold:.12}});
document.querySelectorAll('.reveal').forEach(el=>io.observe(el));
const qm=document.getElementById('quoteModal');
if(qm){{
 document.querySelectorAll('[data-quote]').forEach(b=>b.addEventListener('click',e=>{{e.preventDefault();qm.showModal()}}));
 qm.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>qm.close()));
 qm.addEventListener('click',e=>{{if(e.target===qm)qm.close()}});
 const f=document.getElementById('quoteForm');
 f.addEventListener('submit',async(e)=>{{
  e.preventDefault();
  const data={{}};new FormData(f).forEach((v,k)=>{{if(data[k]){{data[k]=[].concat(data[k],v)}}else{{data[k]=v}}}});
  const ep=f.dataset.endpoint;
  if(ep){{try{{const r=await fetch(ep,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(data)}});if(r.ok){{document.getElementById('quoteThanks').classList.remove('hidden');f.reset();return}}}}catch(_){{}}}}
  const subj=encodeURIComponent('Quote request: '+(data.business||data.name||''));
  const body=encodeURIComponent(Object.entries(data).map(([k,v])=>k+': '+v).join(String.fromCharCode(10)));
  window.location.href='mailto:'+f.dataset.email+'?subject='+subj+'&body='+body;
 }});
}}
</script>
</body>
</html>"""


__all__ = ["GeneratedSite", "build_static_site"]
