"""Encoded rule data for the design-taste subsystem.

This module is the structured form of the ``Leonxlnx/taste-skill`` anti-slop
skill: the dial signal map, the official design-system table, the banned
palette/font families, the AI-tell regex catalogue, and the canonical GSAP
motion skeletons. Keeping it as data (not prose) lets ``profile`` resolve a
``TasteProfile`` deterministically and lets ``audit`` run mechanical Pre-Flight
checks with zero LLM spend.

Pure-stdlib. No I/O.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Dials
# ---------------------------------------------------------------------------

# Baseline 8 / 6 / 4 (variance / motion / density) unless the read overrides.
DIAL_BASELINE: dict[str, int] = {
    "design_variance": 8,
    "motion_intensity": 6,
    "visual_density": 4,
}

# Dial-to-behavior map. The first matching signal (by keyword) wins; the dial
# values are the midpoints of the skill's published ranges.
#   variance / motion / density
DIAL_SIGNAL_MAP: list[dict[str, Any]] = [
    {
        "id": "trust_first_regulated",
        "keywords": (
            "regulated",
            "public sector",
            "public-sector",
            "government",
            "gov",
            "healthcare",
            "finance compliance",
            "legal",
            "trust-first",
            "bank",
            "insurance",
        ),
        "variance": 4,
        "motion": 2,
        "density": 5,
    },
    {
        "id": "minimalist_editorial",
        "keywords": (
            "minimalist",
            "editorial",
            "linear-style",
            "linear style",
            "spare",
            "understated",
            "swiss",
            "magazine",
        ),
        "variance": 6,
        "motion": 3,
        "density": 3,
    },
    {
        "id": "playful_agency_awwwards",
        "keywords": (
            "playful",
            "agency",
            "awwwards",
            "experimental",
            "creative studio",
            "bold",
            "expressive",
            "maximalist",
        ),
        "variance": 10,
        "motion": 9,
        "density": 4,
    },
    {
        "id": "premium_consumer",
        "keywords": (
            "premium",
            "luxury",
            "apple",
            "apple-adjacent",
            "high-end",
            "artisan",
            "wellness",
            "cookware",
            "boutique",
        ),
        "variance": 8,
        "motion": 6,
        "density": 4,
    },
    {
        "id": "landing_mainstream",
        "keywords": (
            "landing",
            "saas",
            "startup",
            "marketing site",
            "product page",
            "waitlist",
            "signup",
        ),
        "variance": 8,
        "motion": 7,
        "density": 4,
    },
]


# ---------------------------------------------------------------------------
# Design-system mapping — install the official package, never hand-recreate.
# ---------------------------------------------------------------------------

TAILWIND_DEFAULT: dict[str, str] = {
    "package": "tailwind-v4-native",
    "install": "",  # default for indie / SaaS / AI marketing — no package to add
    "why": "Default for small teams: Tailwind v4 + native CSS, full control.",
}

DESIGN_SYSTEM_MAP: list[dict[str, Any]] = [
    {
        "keywords": ("microsoft", "fluent", "enterprise saas"),
        "package": "@fluentui/react-components",
        "install": "npm install @fluentui/react-components",
        "why": "Official Microsoft tokens for enterprise SaaS.",
    },
    {
        "keywords": ("material", "google product", "android"),
        "package": "@material/web",
        "install": "npm install @material/web",
        "why": "Official, theme-able Material 3 tokens.",
    },
    {
        "keywords": ("ibm", "carbon", "analytics", "data platform"),
        "package": "@carbon/react",
        "install": "npm install @carbon/react @carbon/styles",
        "why": "Official IBM Carbon — mature data patterns.",
    },
    {
        "keywords": ("shopify", "shopify app", "polaris"),
        "package": "polaris.js",
        "install": "npm install @shopify/polaris",
        "why": "Required for Shopify admin apps.",
    },
    {
        "keywords": ("atlassian", "jira", "confluence", "atlaskit"),
        "package": "@atlaskit/*",
        "install": "yarn add @atlaskit/css-reset @atlaskit/tokens @atlaskit/button",
        "why": "Official Atlassian design system.",
    },
    {
        "keywords": ("github", "primer", "devtool", "developer tool"),
        "package": "@primer/react-brand",
        "install": "npm install @primer/react-brand",
        "why": "Official GitHub Primer (marketing variant).",
    },
    {
        "keywords": ("uk public", "gov.uk", "govuk", "uk government"),
        "package": "govuk-frontend",
        "install": "npm install govuk-frontend",
        "why": "Legally expected for UK public service.",
    },
    {
        "keywords": ("us public", "uswds", "us government", "federal"),
        "package": "uswds",
        "install": "npm install uswds",
        "why": "Trust-first requirement for US public sector.",
    },
    {
        "keywords": ("accessible react", "radix", "wcag react"),
        "package": "@radix-ui/themes",
        "install": "npm install @radix-ui/themes",
        "why": "Accessible primitives + polished theme.",
    },
    {
        "keywords": ("shadcn", "own components", "customizable"),
        "package": "shadcn/ui",
        "install": "npx shadcn@latest init",
        "why": "Customizable — but never ship the default state.",
    },
]


# ---------------------------------------------------------------------------
# Color calibration — banned default palettes.
# ---------------------------------------------------------------------------

# The AI premium-consumer default (warm beige/cream + brass/clay/oxblood +
# espresso text) makes every brand invisible. These hex families are banned as
# the default reach. Stored lowercased for case-insensitive matching.
BANNED_BACKGROUND_HEX: frozenset[str] = frozenset(
    {"#f5f1ea", "#f7f5f1", "#fbf8f1", "#efeae0", "#ece6db"}
)
BANNED_ACCENT_HEX: frozenset[str] = frozenset(
    {"#b08947", "#b6553a", "#9a2436", "#9c6e2a", "#bc7c3a"}
)
BANNED_TEXT_HEX: frozenset[str] = frozenset({"#1a1714", "#1a1814", "#1b1814"})
BANNED_HEX_ALL: frozenset[str] = BANNED_BACKGROUND_HEX | BANNED_ACCENT_HEX | BANNED_TEXT_HEX

# Default palette families to rotate through instead (never reuse the last one).
PALETTE_ROTATION_POOL: tuple[str, ...] = (
    "Cold Luxury: silver-grey + chrome + smoke",
    "Forest: deep green + bone + amber accent",
    "Black and Tan: true off-black + warm tan, sharp contrast",
    "Cobalt + Cream: saturated blue + single neutral",
    "Terracotta + Slate: warm rust + cool grey",
    "Olive + Brick + Paper: muted olive + brick-red accent",
    "Pure monochrome + single saturated pop",
)


# ---------------------------------------------------------------------------
# Typography discipline.
# ---------------------------------------------------------------------------

# Banned as defaults outright (the two LLM-favorite display serifs).
BANNED_FONTS: frozenset[str] = frozenset({"fraunces", "instrument_serif", "instrument serif"})
# Discouraged as the default body font (warn, not fail).
DISCOURAGED_DEFAULT_FONTS: frozenset[str] = frozenset({"inter"})
# Preferred sans display families to reach for first.
PREFERRED_DISPLAY_FONTS: tuple[str, ...] = (
    "Geist",
    "Outfit",
    "Cabinet Grotesk",
    "Satoshi",
    "GT Walsheim",
    "PP Neue Montreal",
    "Söhne Breit",
)


# ---------------------------------------------------------------------------
# AI-tell regex catalogue (deterministic Pre-Flight checks).
# ---------------------------------------------------------------------------

# Both em-dash (U+2014) and en-dash (U+2013) are banned everywhere visible.
# Only the regular hyphen (-) and the math minus (U+2212) are permitted.
EM_DASH_RE = re.compile("[—–]")

# Eyebrow = small uppercase wide-tracking label above a headline. Tailwind
# renders these as "uppercase tracking-…". Count co-located occurrences.
EYEBROW_RE = re.compile(r"uppercase[^>{}\n]{0,60}?tracking", re.IGNORECASE)

# Forbidden animation patterns (code-level tells / jank).
SCROLL_LISTENER_RE = re.compile(r"""addEventListener\(\s*["']scroll["']""", re.IGNORECASE)
SCROLLY_STATE_RE = re.compile(r"window\.scrollY", re.IGNORECASE)
H_SCREEN_RE = re.compile(r"\bh-screen\b")

# Middle-dot is rationed to max 1 per line in metadata strips.
MIDDLE_DOT = "·"

# Contact-intent CTA labels — two distinct ones on a page = duplicate intent.
CONTACT_CTA_LABELS: tuple[str, ...] = (
    "get in touch",
    "contact us",
    "start a project",
    "let's talk",
    "lets talk",
    "book a call",
    "get started",
    "reach out",
)

# Marketing-copy / structural tells. Each entry: (check_id, compiled regex,
# severity, human message). All conservative (warn) to avoid false positives on
# legitimate copy; the em-dash and banned-palette checks carry the hard fails.
AI_TELL_PATTERNS: tuple[tuple[str, "re.Pattern[str]", str, str], ...] = (
    (
        "tell_quietly_trusted",
        re.compile(r"quietly\s+(trusted by|in use at|used by)", re.IGNORECASE),
        "warn",
        '"Quietly trusted by / in use at" is a tested social-proof tell.',
    ),
    (
        "tell_section_number_eyebrow",
        re.compile(r"\b0\d{1,2}\s*[·/]\s*[A-Za-z]"),
        "warn",
        'Section-number eyebrow ("00 / INDEX", "001 . Capabilities") is a tell.',
    ),
    (
        "tell_field_notes",
        re.compile(r"\b(from the field|field notes|on our desks|loose plates)\b", re.IGNORECASE),
        "warn",
        "Poetic section label is an AI copy tell.",
    ),
    (
        "tell_scroll_cue",
        re.compile(r"(↓\s*scroll|scroll\s*↓|mouse-wheel)", re.IGNORECASE),
        "warn",
        "Decorative scroll cue is a tell; a plain arrow is enough.",
    ),
    (
        "tell_version_footer",
        re.compile(r"\bv\d+\.\d+\.\d+\b"),
        "warn",
        "Version footer (v1.4.2 / Build 0048) on a marketing page is a tell.",
    ),
    (
        "tell_live_stock_counter",
        re.compile(r"reservation\s+\d+\s+of\s+\d+", re.IGNORECASE),
        "warn",
        "Live-stock / reservation counter as decoration is a tell.",
    ),
    (
        "tell_invite_only",
        re.compile(r"\b(invite[- ]only|early access)\b", re.IGNORECASE),
        "warn",
        "Hero exclusivity stamp (INVITE-ONLY / EARLY ACCESS) is a tell unless launch.",
    ),
)


# ---------------------------------------------------------------------------
# Canonical GSAP motion skeletons (generation guidance, not audited).
# ---------------------------------------------------------------------------

_STICKY_STACK = """"use client";
import { useRef, useEffect } from "react";
import { gsap } from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import { useReducedMotion } from "motion/react";

gsap.registerPlugin(ScrollTrigger);

export function StickyStack({ cards }: { cards: React.ReactNode[] }) {
  const ref = useRef<HTMLDivElement>(null);
  const reduce = useReducedMotion();
  useEffect(() => {
    if (reduce || !ref.current) return;
    const ctx = gsap.context(() => {
      const cardEls = gsap.utils.toArray<HTMLElement>(".stack-card");
      cardEls.forEach((card, i) => {
        if (i === cardEls.length - 1) return;
        ScrollTrigger.create({
          trigger: card, start: "top top",
          endTrigger: cardEls[cardEls.length - 1], end: "top top",
          pin: true, pinSpacing: false,
        });
        gsap.to(card, {
          scale: 0.92, opacity: 0.55, ease: "none",
          scrollTrigger: { trigger: cardEls[i + 1], start: "top bottom", end: "top top", scrub: true },
        });
      });
    }, ref);
    return () => ctx.revert();
  }, [reduce]);
  return (
    <div ref={ref} className="relative">
      {cards.map((card, i) => (
        <div key={i} className="stack-card sticky top-0 min-h-[100dvh] flex items-center justify-center">{card}</div>
      ))}
    </div>
  );
}
"""

_HORIZONTAL_PAN = """"use client";
import { useRef, useEffect } from "react";
import { gsap } from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import { useReducedMotion } from "motion/react";

gsap.registerPlugin(ScrollTrigger);

export function HorizontalPan({ children }: { children: React.ReactNode }) {
  const wrap = useRef<HTMLDivElement>(null);
  const track = useRef<HTMLDivElement>(null);
  const reduce = useReducedMotion();
  useEffect(() => {
    if (reduce || !wrap.current || !track.current) return;
    const ctx = gsap.context(() => {
      const distance = track.current!.scrollWidth - window.innerWidth;
      gsap.to(track.current, {
        x: -distance, ease: "none",
        scrollTrigger: { trigger: wrap.current, start: "top top", end: () => `+=${distance}`, pin: true, scrub: 1, invalidateOnRefresh: true },
      });
    }, wrap);
    return () => ctx.revert();
  }, [reduce]);
  return (
    <section ref={wrap} className="relative overflow-hidden">
      <div ref={track} className="flex h-[100dvh] items-center">{children}</div>
    </section>
  );
}
"""

_REVEAL_STAGGER = """"use client";
import { motion, useReducedMotion } from "motion/react";

export function RevealStagger({ items }: { items: string[] }) {
  const reduce = useReducedMotion();
  return (
    <ul className="grid gap-6">
      {items.map((item, i) => (
        <motion.li key={item}
          initial={reduce ? false : { opacity: 0, y: 24 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, amount: 0.3 }}
          transition={{ duration: 0.6, delay: i * 0.06, ease: [0.16, 1, 0.3, 1] }}>
          {item}
        </motion.li>
      ))}
    </ul>
  );
}
"""

GSAP_SKELETONS: dict[str, str] = {
    "sticky-stack": _STICKY_STACK,
    "horizontal-pan": _HORIZONTAL_PAN,
    "reveal-stagger": _REVEAL_STAGGER,
}


def gsap_skeleton(name: str) -> str:
    """Return a canonical motion skeleton by name (``""`` if unknown)."""
    return GSAP_SKELETONS.get(name, "")


# The non-negotiable hard rules carried into every generation profile.
CORE_CONSTRAINTS: tuple[str, ...] = (
    "ZERO em-dashes or en-dashes anywhere visible (the #1 AI tell).",
    "One accent color, locked across the whole page; saturation < 80%.",
    "Hero fits the viewport: headline <= 2 lines, subtext <= 20 words, CTA visible.",
    "Max 1 eyebrow per 3 sections (hero counts as 1).",
    "At least 4 different layout families across 8 sections; zigzag max 2 in a row.",
    "Every button text readable against its background (WCAG AA 4.5:1).",
    "Real images (gen-tool first, picsum-seed second); no div-based fake screenshots.",
    "Reduced-motion wrapped for everything when motion_intensity > 3.",
)
