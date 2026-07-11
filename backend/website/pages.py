"""Marketing sub-pages for the headless site — depth toward an agency-grade build.

Generates Services / Gallery / FAQ / Reviews / service-area pages from the brief.
Each returns inner-content HTML (the caller wraps it in shared page chrome) plus
optional <head> extras (e.g. FAQPage JSON-LD). Deterministic, honest:
  * No fabricated testimonials (Reviews is a real scaffold inviting reviews).
  * No unverifiable claims (insurance/guarantees) in the FAQ.
  * Local service-area pages are generated only for areas the operator lists.

Pure-Python. Reuses site_builder's escaping + icon set.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from backend.seo import schema_builder

from .site_builder import _ICON_ORDER, _ICONS, _esc, _safe_url


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _google_form_embed(url: str) -> str:
    """Return a safe Google Forms embed src for ``url``, or "" if it is not a
    Google Forms link. Adds ``?embedded=true`` so the form renders inside the
    iframe without Google's surrounding page chrome. Restricts the host to
    Google's own form domains so an attacker-supplied brief can't frame an
    arbitrary origin."""
    raw = _safe_url((url or "").strip())
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in ("docs.google.com", "forms.gle"):
        return ""
    # forms.gle short links resolve to a docs.google.com viewform; frame as-is.
    if host == "forms.gle":
        return raw
    if "/forms/" not in parts.path:  # docs.google.com must be a Forms path
        return ""
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q["embedded"] = "true"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), ""))


def _service_blurb(name: str, item: str) -> str:
    """A concrete description for a service line, keyed off its wording."""
    it = item.lower()
    if "deep" in it:
        return (f"A top-to-bottom reset. {name} scrubs the spots routine cleaning misses - baseboards, "
                "inside appliances, grout, vents and high-touch surfaces - so the whole space feels new.")
    if "airbnb" in it or "turnover" in it or "bnb" in it:
        return (f"Fast, reliable turnovers between guests. {name} resets the property to hotel standard, "
                "restocks the essentials, and flags anything that needs your attention before the next check-in.")
    if "move" in it:
        return (f"Moving in or out? {name} handles the full move-out (or move-in) clean - inside cabinets, "
                "appliances, baseboards and floors - so you leave it spotless, protect your deposit, or hand "
                "over a fresh start to the new owners.")
    if "commercial" in it:
        return (f"Clean, professional spaces your clients and team notice. {name} works around your hours "
                "with dependable commercial cleaning for offices and storefronts.")
    if "residential" in it or "house" in it or "home" in it:
        return (f"Your home, consistently spotless. {name} handles the regular upkeep so you get your "
                "weekends back, with the same trusted person every visit.")
    if "regular" in it or "recurring" in it or "weekly" in it:
        return (f"Set it and forget it. {name} keeps your space clean on a schedule that fits your life - "
                "weekly, biweekly, or monthly.")
    if "one" in it and "time" in it:
        return (f"Need it clean once, done right? {name} delivers a thorough one-time clean for move-outs, "
                "events, or a seasonal refresh.")
    return f"{name} delivers {item.lower()} with care, consistency, and an eye for the details that matter."


def _services_page(brief: Any) -> dict[str, Any]:
    name = brief.business_name
    items = [s.strip() for s in re.split(r"[|,]", _service_list(brief)) if s.strip()]
    cards = "".join(
        '<div class="reveal glass rounded-2xl bg-white/70 border border-slate-200/70 p-7 shadow-sm '
        f'transition duration-300 hover:-translate-y-1.5 hover:shadow-xl" style="--d:{i*70}ms">'
        '<div class="h-12 w-12 rounded-xl flex items-center justify-center p-2.5 text-white" '
        f'style="background:linear-gradient(135deg,var(--accent),var(--accent2))">{_ICONS[_ICON_ORDER[i%len(_ICON_ORDER)]]}</div>'
        f'<h2 class="mt-4 text-xl font-semibold text-slate-900">{_esc(it)}</h2>'
        f'<p class="mt-2 text-slate-600 leading-relaxed">{_esc(_service_blurb(name, it))}</p></div>'
        for i, it in enumerate(items)
    )
    content = (
        '<div class="reveal max-w-2xl"><h1 class="text-4xl md:text-5xl font-semibold tracking-tight grad-text inline-block">Our Services</h1>'
        f'<p class="mt-4 text-lg text-slate-600">{_esc(brief.business_description)}</p></div>'
        f'<div class="stagger mt-12 grid sm:grid-cols-2 lg:grid-cols-3 gap-6">{cards}</div>'
        '<div class="reveal mt-14 text-center"><a href="/#contact" data-quote class="inline-block rounded-full '
        'px-8 py-4 font-semibold text-slate-900" style="background:linear-gradient(90deg,var(--accent2),var(--accent))">'
        'Get a Free Quote</a></div>'
    )
    return {"file": "services.html", "title": f"Services | {name}", "label": "Services", "content": content, "head": ""}


def _gallery_page(brief: Any, media: dict[str, str]) -> dict[str, Any]:
    name = brief.business_name
    # WEB-D8-01 — allowlist URL schemes before they reach src=.
    imgs = [
        s for s in (
            _safe_url(u) for u in (
                media.get("hero_image"), media.get("section_image"), media.get("og_image")
            )
        ) if s
    ]
    tiles = "".join(
        f'<div class="reveal overflow-hidden rounded-2xl shadow-lg"><img src="{_esc(u)}" loading="lazy" '
        f'alt="{_esc(name)} cleaning work" class="w-full h-64 object-cover transition duration-700 hover:scale-105"></div>'
        for u in imgs
    )
    note = (
        '<div class="reveal mt-10 rounded-2xl border border-dashed border-slate-300 p-8 text-center text-slate-500">'
        'Real before-and-after photos of our work are added here as jobs are completed. '
        'Want to see examples for a space like yours? Ask when you request a quote.</div>'
    )
    content = (
        '<div class="reveal max-w-2xl"><h1 class="text-4xl md:text-5xl font-semibold tracking-tight grad-text inline-block">Our Work</h1>'
        f'<p class="mt-4 text-lg text-slate-600">A look at the standard {name} brings to every space.</p></div>'
        f'<div class="stagger mt-12 grid sm:grid-cols-2 lg:grid-cols-3 gap-6">{tiles}</div>{note}'
    )
    return {"file": "gallery.html", "title": f"Gallery | {name}", "label": "Gallery", "content": content, "head": ""}


def _faq_items(brief: Any) -> list[dict[str, str]]:
    name = brief.business_name
    city = _city(brief)
    return [
        {"q": "What areas do you serve?",
         "a": f"{name} serves {city and (city + ' and the surrounding area') or 'the local area'}. "
              "Not sure if you are in range? Ask when you request a quote."},
        {"q": "Do you offer one-time and recurring cleaning?",
         "a": "Yes. We do regular recurring cleans (weekly, biweekly, or monthly) as well as one-time and "
              "deep cleans for move-outs, events, and seasonal refreshes."},
        {"q": "Do you clean Airbnb and vacation rentals?",
         "a": "Yes. We handle quick, reliable turnovers between guests so your property is guest-ready every time."},
        {"q": "Do you clean both homes and businesses?",
         "a": "Both. We provide residential and commercial cleaning, and we can work around your hours for offices and storefronts."},
        {"q": "How do I get a quote?",
         "a": 'Tap "Get a Free Quote" anywhere on the site and tell us about your space. We will follow up promptly with pricing.'},
        {"q": "Are you family owned?",
         "a": f"Yes. {name} is family owned and operated, so you get a consistent, trusted team that stands behind its work."},
    ]


def _faq_page(brief: Any) -> dict[str, Any]:
    name = brief.business_name
    items = _faq_items(brief)
    rows = "".join(
        '<details class="reveal group rounded-2xl border border-slate-200 bg-white p-5 open:shadow-md">'
        f'<summary class="cursor-pointer list-none font-semibold text-slate-900 flex justify-between items-center">'
        f'{_esc(it["q"])}<span class="text-[var(--accent)] transition group-open:rotate-45 text-2xl leading-none">+</span></summary>'
        f'<p class="mt-3 text-slate-600 leading-relaxed">{_esc(it["a"])}</p></details>'
        for it in items
    )
    content = (
        '<div class="reveal max-w-2xl"><h1 class="text-4xl md:text-5xl font-semibold tracking-tight grad-text inline-block">FAQ</h1>'
        f'<p class="mt-4 text-lg text-slate-600">Answers to the questions we hear most.</p></div>'
        f'<div class="stagger mt-10 grid gap-4 max-w-3xl">{rows}</div>'
    )
    head = schema_builder.to_script_tag(schema_builder.faq_page(items))  # FAQPage JSON-LD (rich results)
    return {"file": "faq.html", "title": f"FAQ | {name}", "label": "FAQ", "content": content, "head": head}


def _reviews_page(brief: Any) -> dict[str, Any]:
    name = brief.business_name
    review_url = _safe_url(getattr(brief, "review_url", "") or "")  # WEB-D8-01
    cta = (f'<a href="{_esc(review_url)}" target="_blank" rel="noopener" '
           'class="inline-block rounded-full px-8 py-4 font-semibold text-slate-900" '
           'style="background:linear-gradient(90deg,var(--accent2),var(--accent))">Leave a Review</a>') if review_url else (
           '<a href="/#contact" data-quote class="inline-block rounded-full px-8 py-4 font-semibold text-slate-900" '
           'style="background:linear-gradient(90deg,var(--accent2),var(--accent))">Work With Us</a>')
    content = (
        '<div class="reveal max-w-2xl"><h1 class="text-4xl md:text-5xl font-semibold tracking-tight grad-text inline-block">Reviews</h1>'
        f'<p class="mt-4 text-lg text-slate-600">{name} is built on word of mouth and repeat clients. '
        'As reviews come in they are featured here.</p></div>'
        '<div class="reveal mt-10 rounded-2xl border border-dashed border-slate-300 p-10 text-center">'
        '<p class="text-slate-600">Recent customer? We would love your feedback.</p>'
        f'<div class="mt-6">{cta}</div></div>'
    )
    return {"file": "reviews.html", "title": f"Reviews | {name}", "label": "Reviews", "content": content, "head": ""}


def _careers_page(brief: Any) -> dict[str, Any]:
    """An honest 'Join Our Team' / hiring page. No fabricated wages or benefits —
    invites applications via the brief's real contact email/phone."""
    name = brief.business_name
    city = _city(brief)
    where = (city and f"in {city} and the surrounding area") or "in our local area"
    email = (brief.contact_email or "").strip()
    phone = (brief.contact_phone or "").strip()
    embed = _google_form_embed(getattr(brief, "careers_form_url", "") or "")
    tel_href = _safe_url("tel:" + re.sub(r"[^0-9+]", "", phone)) if phone else ""
    phone_line = (f'<p class="mt-4 text-slate-600">Prefer to call? Reach us at '
                  f'<a href="{_esc(tel_href)}" '
                  f'class="font-semibold text-[var(--accent)]">{_esc(phone)}</a>.</p>') if tel_href else ""
    # Apply experience: an embedded Google Form (candidates apply digitally;
    # responses land in the owner's Google Sheet + email) when the owner has
    # provided one; otherwise a mailto: "Apply Now" so the page always works.
    if embed:
        apply_section = (
            '<div class="reveal mt-14 rounded-2xl border border-slate-200/70 bg-white/70 p-6 md:p-8 shadow-sm">'
            '<h2 class="text-2xl font-semibold text-slate-900 text-center">Apply online</h2>'
            '<p class="mt-2 text-center text-slate-600">Fill out the short application below - it only takes '
            'a couple of minutes, and you can attach a resume if you have one.</p>'
            '<div class="mt-7 overflow-hidden rounded-xl border border-slate-200 bg-white">'
            f'<iframe src="{_esc(embed)}" title="{_esc(name)} job application" loading="lazy" '
            'class="w-full" style="height:1400px;border:0" marginheight="0" marginwidth="0">'
            'Loading the application form...</iframe></div>'
            '<p class="mt-4 text-center text-sm text-slate-500">Trouble seeing the form? '
            f'<a href="{_esc(embed)}" target="_blank" rel="noopener" '
            'class="font-semibold text-[var(--accent)]">Open it in a new tab</a>.</p>'
            f'{phone_line}</div>'
        )
    else:
        apply_href = _safe_url(f"mailto:{email}?subject=Cleaning%20Position%20Application") if email else ""
        if apply_href:
            apply_btn = (f'<a href="{_esc(apply_href)}" class="inline-block rounded-full px-8 py-4 font-semibold '
                         'text-slate-900" style="background:linear-gradient(90deg,var(--accent2),var(--accent))">Apply Now</a>')
        else:
            apply_btn = ('<a href="/#contact" data-quote class="inline-block rounded-full px-8 py-4 font-semibold '
                         'text-slate-900" style="background:linear-gradient(90deg,var(--accent2),var(--accent))">Apply Now</a>')
        apply_section = (
            '<div class="reveal mt-14 rounded-2xl border border-dashed border-slate-300 p-10 text-center">'
            '<p class="text-lg text-slate-700">Ready to apply? Send us a quick note about yourself and your '
            'availability.</p>'
            f'<div class="mt-6">{apply_btn}</div>{phone_line}</div>'
        )
    # "What we look for" cards — honest expectations, not invented perks.
    looks = [
        ("Dependable", "You show up on time, every time, and clients can count on you."),
        ("Detail-oriented", "You take pride in the spots others miss and leave every space better than you found it."),
        ("Respectful", "You treat clients' homes and businesses with care, honesty, and discretion."),
        ("Team player", "We are family owned and operated - we look out for each other and the people we serve."),
    ]
    cards = "".join(
        '<div class="reveal glass rounded-2xl bg-white/70 border border-slate-200/70 p-7 shadow-sm '
        f'transition duration-300 hover:-translate-y-1.5 hover:shadow-xl" style="--d:{i*70}ms">'
        '<div class="h-12 w-12 rounded-xl flex items-center justify-center p-2.5 text-white" '
        f'style="background:linear-gradient(135deg,var(--accent),var(--accent2))">{_ICONS[_ICON_ORDER[i%len(_ICON_ORDER)]]}</div>'
        f'<h2 class="mt-4 text-xl font-semibold text-slate-900">{_esc(title)}</h2>'
        f'<p class="mt-2 text-slate-600 leading-relaxed">{_esc(body)}</p></div>'
        for i, (title, body) in enumerate(looks)
    )
    content = (
        '<div class="reveal max-w-2xl"><h1 class="text-4xl md:text-5xl font-semibold tracking-tight grad-text inline-block">Join Our Team</h1>'
        f'<p class="mt-4 text-lg text-slate-600">{_esc(name)} is growing, and we are looking for dependable, '
        f'detail-minded people {_esc(where)} who take pride in their work. If that sounds like you, we would '
        'love to hear from you.</p></div>'
        f'<div class="stagger mt-12 grid sm:grid-cols-2 lg:grid-cols-4 gap-6">{cards}</div>'
        f'{apply_section}'
    )
    return {"file": "careers.html", "title": f"Careers | {name}", "label": "Careers", "content": content, "head": ""}


def _area_page(brief: Any, area: str) -> dict[str, Any]:
    name = brief.business_name
    primary = (brief.industry or "Cleaning").split(",")[0].strip() or "Cleaning"
    state = _state(brief)
    loc = f"{area}, {state}".strip(", ")
    lb = schema_builder.local_business(
        name=name, telephone=brief.contact_phone, city=area, region=state,
        country="US", url="",
    )
    if area:
        lb["areaServed"] = {"@type": "City", "name": area}
    content = (
        f'<div class="reveal max-w-3xl"><h1 class="text-4xl md:text-5xl font-semibold tracking-tight grad-text inline-block">'
        f'{_esc(primary)} in {_esc(loc)}</h1>'
        f'<p class="mt-5 text-lg text-slate-600 leading-relaxed">{_esc(name)} provides professional, reliable '
        f'cleaning for homes and businesses in {_esc(area)} and nearby. Regular upkeep, deep cleans, and '
        'rental turnovers, handled by a family owned and operated team you can count on.</p>'
        f'<p class="mt-4 text-lg text-slate-600">Serving {_esc(loc)} with the same care we bring to every space.</p>'
        '<div class="mt-8"><a href="/#contact" data-quote class="inline-block rounded-full px-8 py-4 font-semibold '
        'text-slate-900" style="background:linear-gradient(90deg,var(--accent2),var(--accent))">Get a Free Quote</a></div></div>'
    )
    return {"file": f"areas/{_slug(area)}.html", "title": f"{primary} in {loc} | {name}",
            "label": area, "content": content, "head": schema_builder.to_script_tag(lb), "area": True}


# --- helpers ---------------------------------------------------------------

def _service_list(brief: Any) -> str:
    for p in brief.pages:
        if p.slug.lower().startswith("service"):
            return p.content.get("list") or p.content.get("body") or ""
    return ""


def _city(brief: Any) -> str:
    parts = [p.strip() for p in (brief.address or "").split(",") if p.strip()]
    return parts[1] if len(parts) >= 2 else ""


def _state(brief: Any) -> str:
    m = re.search(r"\b([A-Z]{2})\b\s+\d{5}", brief.address or "")
    return m.group(1) if m else (brief.registered_state or "")


def build_marketing_pages(brief: Any, *, media: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """All marketing sub-pages for the brief (Services, Gallery, FAQ, Reviews, areas)."""
    media = media or {}
    pages = [
        _services_page(brief),
        _gallery_page(brief, media),
        _faq_page(brief),
        _reviews_page(brief),
        _careers_page(brief),
    ]
    areas = list(getattr(brief, "service_areas", None) or [])
    if not areas:
        c = _city(brief)
        areas = [c] if c else []
    for area in areas:
        pages.append(_area_page(brief, area))
    return pages


__all__ = ["build_marketing_pages"]
