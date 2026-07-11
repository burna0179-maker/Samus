"""Deterministic Pre-Flight audit — the anti-slop quality gate.

Runs the mechanical half of the taste-skill over a rendered deliverable:
the em-dash ban, eyebrow restraint, banned palette/font families, the AI-tell
scan, and the forbidden-animation patterns. Every check here is pure-stdlib
and zero-LLM, so the gate runs inside the $1/day global cap without spending a
cent (the subjective copy self-audit, which does want an LLM, is intentionally
out of scope for this module).

The result is a ``TasteAuditResult`` shaped to slot into the PDC composite
alongside ElegancePlan / ConfusionScore.

Input shapes (both accepted):
  * ``audit_text(text, kind=..., section_count=..., colors=..., fonts=...)``
  * ``audit_deliverable(deliverable: dict)`` — pulls text from the first of
    ``html`` / ``markup`` / ``document`` / ``text`` / ``content`` and reads the
    optional ``kind`` / ``section_count`` / ``colors`` / ``fonts`` hints.

Markdown deliverables (Samus's current output) are audited too — the em-dash
ban, AI-tell scan, middle-dot ration, and duplicate-CTA check all apply to
prose. The frontend-specific checks (eyebrow, h-screen, scroll listener)
no-op gracefully when the markup doesn't contain them.
"""
from __future__ import annotations

import math
import re
from typing import Any, Iterable

from . import rules
from .models import (
    SEVERITY_FAIL,
    SEVERITY_WARN,
    TasteAuditResult,
    TasteViolation,
    _grade,
)

_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}\b")
_SECTION_TAG_RE = re.compile(r"<section\b", re.IGNORECASE)
_FENCE_RE = re.compile(r"```[\s\S]*?```")
_TRUNC = 80


def _truncate(s: str) -> str:
    s = s.replace("\n", " ").strip()
    return s if len(s) <= _TRUNC else s[: _TRUNC - 1] + "…"


def _clamp01(x: float) -> float:
    if math.isnan(x) or math.isinf(x):
        return 0.0
    return max(0.0, min(1.0, x))


# --- individual checks ------------------------------------------------------
# Each check appends TasteViolation(s) for what it finds. Weights are the score
# penalty; the em-dash and banned-palette checks carry SEVERITY_FAIL.


def _check_em_dash(text: str, out: list[TasteViolation]) -> None:
    hits = rules.EM_DASH_RE.findall(text)
    if hits:
        out.append(TasteViolation(
            check_id="em_dash_ban",
            severity=SEVERITY_FAIL,
            message=(
                f"{len(hits)} banned dash char(s) (em/en dash) present. The em-dash "
                "is the single most-tested AI tell; only regular hyphens are allowed."
            ),
            evidence=_truncate("".join(hits[:10])),
            weight=0.5,
        ))


def _check_banned_palette(text: str, colors: Iterable[str] | None, out: list[TasteViolation]) -> None:
    found: set[str] = set()
    for hx in _HEX_RE.findall(text):
        if hx.lower() in rules.BANNED_HEX_ALL:
            found.add(hx.lower())
    for hx in colors or []:
        h = str(hx).strip().lower()
        if h in rules.BANNED_HEX_ALL:
            found.add(h)
    if found:
        out.append(TasteViolation(
            check_id="banned_palette",
            severity=SEVERITY_FAIL,
            message=(
                "AI-default premium-consumer palette family (beige/cream + brass/"
                "clay/oxblood + espresso) present. Rotate a distinct palette."
            ),
            evidence=_truncate(", ".join(sorted(found))),
            weight=0.4,
        ))


def _check_fonts(text: str, fonts: Iterable[str] | None, out: list[TasteViolation]) -> None:
    low = text.lower()
    names = {str(f).strip().lower() for f in (fonts or [])}
    haystack = low + " " + " ".join(names)
    for banned in rules.BANNED_FONTS:
        if banned in haystack:
            out.append(TasteViolation(
                check_id="font_discipline",
                severity=SEVERITY_FAIL,
                message=f"Banned default display serif present: {banned}.",
                evidence=banned,
                weight=0.3,
            ))
    for discouraged in rules.DISCOURAGED_DEFAULT_FONTS:
        if re.search(rf"\b{re.escape(discouraged)}\b", haystack):
            out.append(TasteViolation(
                check_id="font_default_discouraged",
                severity=SEVERITY_WARN,
                message=f'"{discouraged}" is discouraged as the default font; reach for a display family first.',
                evidence=discouraged,
                weight=0.07,
            ))


def _check_eyebrow_restraint(text: str, section_count: int | None, out: list[TasteViolation]) -> None:
    eyebrows = len(rules.EYEBROW_RE.findall(text))
    if eyebrows == 0:
        return
    # Estimate section count from <section> tags when not supplied.
    sections = section_count if section_count and section_count > 0 else len(_SECTION_TAG_RE.findall(text))
    if sections <= 0:
        # Can't apply the ratio mechanically; flag only egregious counts.
        if eyebrows >= 4:
            out.append(TasteViolation(
                check_id="eyebrow_restraint",
                severity=SEVERITY_WARN,
                message=f"{eyebrows} uppercase-tracking eyebrows; section count unknown — verify <= 1 per 3 sections.",
                evidence=f"{eyebrows} eyebrows",
                weight=0.1,
            ))
        return
    allowed = math.ceil(sections / 3)
    if eyebrows > allowed:
        out.append(TasteViolation(
            check_id="eyebrow_restraint",
            severity=SEVERITY_WARN,
            message=(
                f"{eyebrows} eyebrows across {sections} sections exceeds the "
                f"max of {allowed} (1 per 3 sections, hero counts as 1)."
            ),
            evidence=f"{eyebrows} > {allowed}",
            weight=0.12,
        ))


def _check_forbidden_animation(text: str, out: list[TasteViolation]) -> None:
    if rules.SCROLL_LISTENER_RE.search(text):
        out.append(TasteViolation(
            check_id="scroll_listener_ban",
            severity=SEVERITY_FAIL,
            message='window.addEventListener("scroll", …) is banned (jank). Use ScrollTrigger / useScroll / IntersectionObserver.',
            evidence="addEventListener('scroll')",
            weight=0.25,
        ))
    if rules.SCROLLY_STATE_RE.search(text):
        out.append(TasteViolation(
            check_id="scrolly_state_warn",
            severity=SEVERITY_WARN,
            message="window.scrollY read (likely per-frame React state) — prefer useMotionValue/useTransform.",
            evidence="window.scrollY",
            weight=0.08,
        ))
    if rules.H_SCREEN_RE.search(text):
        out.append(TasteViolation(
            check_id="viewport_stability",
            severity=SEVERITY_WARN,
            message="h-screen used; prefer min-h-[100dvh] for mobile viewport stability.",
            evidence="h-screen",
            weight=0.05,
        ))


def _check_middle_dot_ration(text: str, out: list[TasteViolation]) -> None:
    over = 0
    for line in text.splitlines():
        if line.count(rules.MIDDLE_DOT) > 1:
            over += 1
    if over:
        out.append(TasteViolation(
            check_id="middle_dot_ration",
            severity=SEVERITY_WARN,
            message=f"{over} line(s) use more than one middle-dot (·); ration to max 1 per line.",
            evidence=f"{over} line(s)",
            weight=0.05,
        ))


def _check_duplicate_cta_intent(text: str, out: list[TasteViolation]) -> None:
    low = text.lower()
    present = [label for label in rules.CONTACT_CTA_LABELS if label in low]
    if len(set(present)) >= 2:
        out.append(TasteViolation(
            check_id="duplicate_cta_intent",
            severity=SEVERITY_WARN,
            message="Multiple contact-intent CTA labels present; pick ONE label everywhere.",
            evidence=_truncate(", ".join(sorted(set(present)))),
            weight=0.08,
        ))


def _check_marquee_max_one(text: str, out: list[TasteViolation]) -> None:
    count = len(re.findall(r"\bmarquee\b", text, re.IGNORECASE))
    if count > 1:
        out.append(TasteViolation(
            check_id="marquee_max_one",
            severity=SEVERITY_WARN,
            message=f"{count} marquees on one page; max one per page.",
            evidence=f"{count} marquees",
            weight=0.06,
        ))


def _check_ai_tells(text: str, out: list[TasteViolation]) -> None:
    for check_id, pattern, severity, message in rules.AI_TELL_PATTERNS:
        m = pattern.search(text)
        if m:
            out.append(TasteViolation(
                check_id=check_id,
                severity=severity,
                message=message,
                evidence=_truncate(m.group(0)),
                weight=0.07 if severity == SEVERITY_WARN else 0.2,
            ))


_ALL_CHECK_IDS: tuple[str, ...] = (
    "em_dash_ban",
    "banned_palette",
    "font_discipline",
    "eyebrow_restraint",
    "forbidden_animation",
    "middle_dot_ration",
    "duplicate_cta_intent",
    "marquee_max_one",
    "ai_tells",
)


def audit_text(
    text: str,
    *,
    kind: str = "frontend",
    section_count: int | None = None,
    colors: Iterable[str] | None = None,
    fonts: Iterable[str] | None = None,
) -> TasteAuditResult:
    """Run the deterministic Pre-Flight audit over rendered deliverable text.

    ``kind`` is informational (e.g. "frontend" | "markdown" | "proposal"); all
    checks run regardless and the markup-specific ones simply find nothing on
    prose. Returns a ``TasteAuditResult`` — never raises on content.
    """
    text = text or ""
    violations: list[TasteViolation] = []

    _check_em_dash(text, violations)
    _check_banned_palette(text, colors, violations)
    _check_fonts(text, fonts, violations)
    _check_eyebrow_restraint(text, section_count, violations)
    _check_forbidden_animation(text, violations)
    _check_middle_dot_ration(text, violations)
    _check_duplicate_cta_intent(text, violations)
    _check_marquee_max_one(text, violations)
    _check_ai_tells(text, violations)

    penalty = sum(v.weight for v in violations)
    score = _clamp01(1.0 - penalty)
    passed = not any(v.severity == SEVERITY_FAIL for v in violations)

    rationale: list[str] = []
    if not violations:
        rationale.append("No tested AI tells detected.")
    else:
        fails = [v for v in violations if v.severity == SEVERITY_FAIL]
        if fails:
            rationale.append(
                "Hard fail(s): " + ", ".join(sorted({v.check_id for v in fails}))
            )
        warns = [v for v in violations if v.severity == SEVERITY_WARN]
        if warns:
            rationale.append(
                "Soft signal(s): " + ", ".join(sorted({v.check_id for v in warns}))
            )

    return TasteAuditResult(
        score=score,
        grade=_grade(score),
        passed=passed,
        checks_run=list(_ALL_CHECK_IDS),
        violations=violations,
        rationale=rationale,
    )


def _extract_text(deliverable: dict[str, Any]) -> str:
    for field_name in ("html", "markup", "document", "text", "content"):
        val = deliverable.get(field_name)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def audit_deliverable(deliverable: dict[str, Any]) -> TasteAuditResult:
    """Audit a deliverable dict.

    Pulls the rendered text from the first present of html / markup / document /
    text / content, and reads optional ``kind`` / ``section_count`` / ``colors``
    / ``fonts`` hints. A deliverable with no readable text returns a clean
    (empty) result with an explanatory rationale rather than failing.
    """
    text = _extract_text(deliverable)
    if not text:
        return TasteAuditResult(
            score=1.0,
            grade="A",
            passed=True,
            checks_run=[],
            violations=[],
            rationale=["No rendered text found on the deliverable; audit skipped."],
        )
    return audit_text(
        text,
        kind=str(deliverable.get("kind", "frontend")),
        section_count=deliverable.get("section_count"),
        colors=deliverable.get("colors"),
        fonts=deliverable.get("fonts"),
    )
