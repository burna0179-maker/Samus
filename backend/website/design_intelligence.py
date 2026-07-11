"""Industry -> design-system reasoning (vendored from ui-ux-pro-max-skill, MIT).

Maps a business (industry + description) to an industry-appropriate design
system: a color palette, a Google-Fonts type pairing, a layout pattern, a UI
style, key effects, and explicit anti-patterns. This makes Samus-generated
sites *industry-tailored* (a spa looks like a spa, a law firm like a law firm)
instead of generic, and complements the taste subsystem (which enforces
anti-slop rules) + media_gen (visuals).

Data: ``backend/website/data/uiux/{colors,typography,ui-reasoning}.csv`` —
161 palettes, 74 font pairings, 161 reasoning rules. See ATTRIBUTION.md.

Pure-stdlib (csv), deterministic, zero-cost, loaded once + cached. Never raises
on bad data — degrades to a neutral professional default.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

_LOG = logging.getLogger("samus.website.design_intelligence")
_DATA = Path(__file__).resolve().parent / "data" / "uiux"
_HEX = re.compile(r"#[0-9A-Fa-f]{6}\b")


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


# Google-Places industry keywords -> dataset vocabulary. The ui-reasoning
# taxonomy names its categories in product language ("Home Services
# (Plumber/Electrician)", "Automotive/Car Dealership"); the prospecting
# pipeline feeds Places search keywords ("hvac contractor", "car dealer").
# With zero token overlap the matcher silently degraded to the generic
# Professional default — every trade got the same template (found 2026-06-11:
# an HVAC demo rendered identically to a cleaning service). Expansion, not
# replacement: original tokens stay in the query so a description that
# already speaks dataset vocabulary keeps its full weight.
_INDUSTRY_SYNONYMS: dict[str, str] = {
    "hvac": "home services plumber electrician heating cooling",
    "heating": "home services plumber electrician",
    "plumber": "home services plumber electrician",
    "plumbing": "home services plumber electrician",
    "electrician": "home services plumber electrician",
    "roofing": "home services plumber electrician construction",
    "roofer": "home services plumber electrician construction",
    "handyman": "home services plumber electrician",
    "cleaning": "home services",
    "landscaping": "home services",
    "realtor": "real estate property",
    "realty": "real estate property",
    "accounting": "banking traditional finance",
    "accountant": "banking traditional finance",
    "cpa": "banking traditional finance",
    "tax": "banking traditional finance",
    "bookkeeping": "banking traditional finance",
    "dentist": "dental practice",
    "dental": "dental practice",
    "attorney": "legal services",
    "lawyer": "legal services",
    "insurance": "insurance platform",
    "dealer": "automotive car dealership",
    "dealership": "automotive car dealership",
}


def _expand_industry(q: set[str]) -> set[str]:
    out = set(q)
    for tok in q:
        syn = _INDUSTRY_SYNONYMS.get(tok)
        if syn:
            out |= _tokens(syn)
    return out


@dataclass
class DesignSystem:
    category: str = "Professional (Default)"
    pattern: str = "Hero + Features + CTA"
    style: str = "Minimalism & Flat Design"
    color_mood: str = ""
    typography_mood: str = ""
    effects: str = "Subtle hover (200-250ms) + smooth transitions"
    anti_patterns: list[str] = field(default_factory=list)
    severity: str = ""
    palette: dict[str, str] = field(default_factory=dict)  # name/primary/accent/background/text
    typography: dict[str, str] = field(
        default_factory=dict
    )  # pairing/heading/body/css_import/google_url

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "pattern": self.pattern,
            "style": self.style,
            "color_mood": self.color_mood,
            "typography_mood": self.typography_mood,
            "effects": self.effects,
            "anti_patterns": list(self.anti_patterns),
            "severity": self.severity,
            "palette": dict(self.palette),
            "typography": dict(self.typography),
        }


def _read(path: Path) -> list[list[str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
    except (OSError, csv.Error) as exc:
        _LOG.warning("design data read failed (%s): %s", path, exc)
        return []
    # drop header row (first cell is "No")
    return (
        [r for r in rows[1:] if r]
        if rows and rows[0] and rows[0][0].strip().lower() == "no"
        else rows
    )


@lru_cache(maxsize=1)
def _palettes() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for r in _read(_DATA / "colors.csv"):
        if len(r) < 3:
            continue
        hexes = [h for h in r if _HEX.fullmatch(h.strip())]
        if not hexes:
            continue

        def at(i: int, default: str) -> str:
            return hexes[i] if i < len(hexes) else default

        out.append(
            {
                "name": r[1].strip(),
                "primary": at(0, "#2563EB"),
                "accent": at(4, at(0, "#EA580C")),
                "background": at(6, "#FFFFFF"),
                "text": at(7, "#1E293B"),
            }
        )
    return out


@lru_cache(maxsize=1)
def _typography() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for r in _read(_DATA / "typography.csv"):
        if len(r) < 9:
            continue
        out.append(
            {
                "pairing": r[1].strip(),
                "category": r[2].strip(),
                "heading": r[3].strip(),
                "body": r[4].strip(),
                "mood": r[5].strip(),
                "best_for": r[6].strip(),
                "google_url": r[7].strip(),
                "css_import": r[8].strip(),
            }
        )
    return out


@lru_cache(maxsize=1)
def _rules() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for r in _read(_DATA / "ui-reasoning.csv"):
        if len(r) < 9:
            continue
        out.append(
            {
                "category": r[1].strip(),
                "pattern": r[2].strip(),
                "style": r[3].strip(),
                "color_mood": r[4].strip(),
                "typography_mood": r[5].strip(),
                "effects": r[6].strip(),
                "anti_patterns": r[8].strip(),
                "severity": r[9].strip() if len(r) > 9 else "",
            }
        )
    return out


def _best(
    query: set[str], rows: list[dict[str, str]], fields: tuple[str, ...]
) -> tuple[dict[str, str] | None, int]:
    best, best_score = None, 0
    for row in rows:
        text = " ".join(row.get(f, "") for f in fields)
        score = len(query & _tokens(text))
        if score > best_score:
            best, best_score = row, score
    return best, best_score


@lru_cache(maxsize=256)
def recommend(industry: str = "", description: str = "") -> DesignSystem:
    """Recommend a design system for a business. Cached; never raises."""
    q = _expand_industry(_tokens(f"{industry} {description}"))
    ds = DesignSystem()
    if not q:
        q = {"professional", "business"}

    rule, _ = _best(q, _rules(), ("category",))
    if rule:
        ds.category = rule["category"]
        ds.pattern = rule["pattern"] or ds.pattern
        ds.style = rule["style"] or ds.style
        ds.color_mood = rule["color_mood"]
        ds.typography_mood = rule["typography_mood"]
        ds.effects = rule["effects"] or ds.effects
        ds.severity = rule["severity"]
        if rule["anti_patterns"]:
            ds.anti_patterns = [
                a.strip() for a in re.split(r"[+,;]", rule["anti_patterns"]) if a.strip()
            ]

    # palette: match on category name (palette names mirror the rule taxonomy)
    cat_tokens = _tokens(ds.category) | q
    pal, _ = _best(cat_tokens, _palettes(), ("name",))
    if pal:
        ds.palette = {k: v for k, v in pal.items()}

    # typography: match the type pairing against the rule's mood + the query
    type_q = q | _tokens(ds.typography_mood) | _tokens(ds.color_mood)
    typ, score = _best(type_q, _typography(), ("mood", "best_for", "category"))
    if typ and score > 0:
        ds.typography = {
            "pairing": typ["pairing"],
            "heading": typ["heading"],
            "body": typ["body"],
            "css_import": typ["css_import"],
            "google_url": typ["google_url"],
        }
    return ds


__all__ = ["DesignSystem", "recommend"]
