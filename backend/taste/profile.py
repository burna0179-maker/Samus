"""Brief inference -> TasteProfile (generation guidance).

Deterministic, pure-stdlib. Reads a free-text brief (plus optional explicit
hints) and produces a ``TasteProfile``: the resolved dials, the official
design-system to install, and the palette/type/constraint guidance a producer
should honor before generating a deliverable. No LLM, no I/O.
"""

from __future__ import annotations


from . import rules
from .models import DesignRead, TasteDials, TasteProfile

# Keyword -> page_kind / audience / vibe lexicons for the design read.
_PAGE_KIND_LEXICON: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("redesign", ("redesign", "revamp", "refresh existing", "overhaul")),
    ("portfolio", ("portfolio", "showcase", "case study", "case-study")),
    ("editorial", ("editorial", "magazine", "article", "blog")),
    ("proposal", ("proposal", "pitch", "brief", "deck", "scope")),
    ("landing", ("landing", "homepage", "marketing", "product page", "waitlist")),
)
_AUDIENCE_LEXICON: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("public-sector", ("public sector", "public-sector", "government", "gov", "citizen")),
    ("b2b-buyer", ("b2b", "enterprise", "saas buyer", "procurement", "decision maker")),
    ("recruiter", ("recruiter", "hiring", "job", "resume")),
    ("consumer", ("consumer", "shopper", "customer", "end user", "everyday")),
)
_VIBE_LEXICON: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("brutal", ("brutal", "brutalist", "industrial", "raw")),
    ("playful", ("playful", "fun", "quirky", "vibrant")),
    ("premium", ("premium", "luxury", "high-end", "elegant", "refined")),
    ("editorial", ("editorial", "publication", "magazine")),
    ("glassy", ("glassy", "glass", "frosted")),
    ("minimalist", ("minimalist", "minimal", "spare", "clean", "understated")),
)


def _first_match(text: str, lexicon: tuple[tuple[str, tuple[str, ...]], ...], default: str) -> str:
    low = text.lower()
    for label, keywords in lexicon:
        if any(kw in low for kw in keywords):
            return label
    return default


def infer_design_read(
    brief: str,
    *,
    references: list[str] | None = None,
    vibe_words: list[str] | None = None,
) -> DesignRead:
    """Read the room from a brief. Mirrors the skill's one-line "Design Read".

    Ambiguity rule: an essentially empty brief sets ``needs_clarification`` so
    the caller can ask exactly one clarifying question rather than guessing.
    """
    parts = [brief or ""]
    if vibe_words:
        parts.append(" ".join(vibe_words))
    if references:
        parts.append(" ".join(references))
    text = " ".join(p for p in parts if p).strip()

    needs_clarification = len(text.split()) < 3

    page_kind = _first_match(text, _PAGE_KIND_LEXICON, "landing")
    audience = _first_match(text, _AUDIENCE_LEXICON, "consumer")
    vibe = _first_match(text, _VIBE_LEXICON, "premium")

    # The aesthetic family the dial signal map matched (if any) doubles as the
    # leaning; otherwise echo the vibe.
    signals = _matched_signals(text)
    aesthetic_family = signals[0] if signals else vibe

    one_liner = (
        f"Reading this as: {page_kind} for {audience}, with a {vibe} language, "
        f"leaning toward {aesthetic_family}."
    )
    return DesignRead(
        page_kind=page_kind,
        audience=audience,
        vibe=vibe,
        aesthetic_family=aesthetic_family,
        one_liner=one_liner,
        signals=signals,
        needs_clarification=needs_clarification,
    )


def _matched_signals(text: str) -> list[str]:
    low = text.lower()
    matched: list[str] = []
    for signal in rules.DIAL_SIGNAL_MAP:
        if any(kw in low for kw in signal["keywords"]):
            matched.append(signal["id"])
    return matched


def resolve_dials(design_read: DesignRead) -> TasteDials:
    """Resolve the three dials from a design read.

    The first matching signal wins (signals are ordered most-specific first:
    trust-first before minimalist before playful before premium before
    mainstream-landing). No match -> the 8/6/4 baseline.
    """
    for signal in rules.DIAL_SIGNAL_MAP:
        if signal["id"] in design_read.signals:
            return TasteDials(
                design_variance=signal["variance"],
                motion_intensity=signal["motion"],
                visual_density=signal["density"],
            )
    return TasteDials(**rules.DIAL_BASELINE)


def select_design_system(brief: str) -> dict[str, str]:
    """Pick the official design system to install. Default = Tailwind v4 native.

    Honesty rule: install the real package; never hand-recreate a system's CSS.
    """
    low = (brief or "").lower()
    for entry in rules.DESIGN_SYSTEM_MAP:
        if any(kw in low for kw in entry["keywords"]):
            return {
                "package": entry["package"],
                "install": entry["install"],
                "why": entry["why"],
            }
    return dict(rules.TAILWIND_DEFAULT)


def _typography_guidance(vibe: str) -> list[str]:
    guidance = [
        "Prefer a sans display family: " + ", ".join(rules.PREFERRED_DISPLAY_FONTS) + ".",
        "Inter is discouraged as the default body font.",
        "Banned display serifs as defaults: Fraunces, Instrument_Serif.",
        "Emphasis = italic/bold of the SAME font; never inject a serif word into a sans headline.",
        "Italic words with descenders (y g j p q) need leading-[1.1] min + pb-1 reserve.",
    ]
    if vibe in {"editorial", "premium"}:
        guidance.append(
            "Serif is permitted only if the brief names one or the work is genuinely "
            "editorial/luxury; if so, rotate families per project."
        )
    return guidance


def _palette_guidance(vibe: str) -> list[str]:
    guidance = [
        "One accent color, locked across the whole page; saturation < 80%.",
        "Neutral bases (Zinc/Slate/Stone) + a single high-contrast accent.",
        "No AI purple/blue glow, no neon gradients (the LILA rule).",
    ]
    if vibe in {"premium", "consumer"}:
        guidance.append(
            "Premium-consumer: the beige/cream + brass/clay/oxblood + espresso "
            "default is BANNED. Rotate a fresh family: "
            + " | ".join(rules.PALETTE_ROTATION_POOL[:4])
            + "."
        )
    return guidance


def build_profile(
    brief: str,
    *,
    references: list[str] | None = None,
    vibe_words: list[str] | None = None,
    dial_overrides: dict[str, int] | None = None,
) -> TasteProfile:
    """End-to-end: brief -> resolved TasteProfile.

    ``dial_overrides`` (any of design_variance / motion_intensity /
    visual_density) take precedence over the inferred dials, so an operator can
    pin a value while letting the rest resolve from the brief.
    """
    design_read = infer_design_read(brief, references=references, vibe_words=vibe_words)
    dials = resolve_dials(design_read)
    if dial_overrides:
        merged = dials.to_dict()
        merged.update({k: v for k, v in dial_overrides.items() if k in merged})
        dials = TasteDials(**merged)

    ds = select_design_system(brief)
    return TasteProfile(
        design_read=design_read,
        dials=dials,
        design_system=ds["package"],
        design_system_install=ds["install"],
        design_system_rationale=ds["why"],
        palette_guidance=_palette_guidance(design_read.vibe),
        typography_guidance=_typography_guidance(design_read.vibe),
        constraints=list(rules.CORE_CONSTRAINTS),
    )
