"""Schemas for the design-taste subsystem.

Pure-stdlib dataclasses with ``to_dict()`` JSON projections, mirroring
``backend.governance.elegance_scorer.ElegancePlan`` and
``backend.observability.confusion_meter.ConfusionScore`` so a
``TasteAuditResult`` drops straight into the PDC composite finding. No I/O,
no LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Violation severities.
SEVERITY_FAIL = "fail"  # a non-negotiable tell (em-dash, banned palette) — gate should refuse
SEVERITY_WARN = "warn"  # a soft signal — degrades the score, never hard-fails alone


def _clamp_dial(x: Any) -> int:
    """Coerce a dial to the inclusive 1..10 range."""
    try:
        v = int(round(float(x)))
    except (TypeError, ValueError):
        v = 1
    return max(1, min(10, v))


def _grade(score: float) -> str:
    """Shared A-F ladder (identical thresholds to elegance/confusion graders)."""
    if score >= 0.8:
        return "A"
    if score >= 0.6:
        return "B"
    if score >= 0.4:
        return "C"
    if score >= 0.2:
        return "D"
    return "F"


@dataclass
class TasteDials:
    """The three anti-slop dials, each on a 1-10 scale.

    Baseline 8 / 6 / 4 unless the design read overrides them.

      * design_variance  — layout asymmetry  (1 = symmetric, 10 = artsy chaos)
      * motion_intensity — animation scope   (1 = static, 10 = cinematic)
      * visual_density   — information pack   (1 = gallery, 10 = cockpit)
    """

    design_variance: int = 8
    motion_intensity: int = 6
    visual_density: int = 4

    def __post_init__(self) -> None:
        self.design_variance = _clamp_dial(self.design_variance)
        self.motion_intensity = _clamp_dial(self.motion_intensity)
        self.visual_density = _clamp_dial(self.visual_density)

    def to_dict(self) -> dict[str, Any]:
        return {
            "design_variance": self.design_variance,
            "motion_intensity": self.motion_intensity,
            "visual_density": self.visual_density,
        }


@dataclass
class DesignRead:
    """The one-line "reading the room" inference made before any generation."""

    page_kind: str  # landing | portfolio | redesign | editorial | proposal | ...
    audience: str  # b2b-buyer | consumer | recruiter | public-sector | ...
    vibe: str  # minimalist | premium | playful | brutal | editorial | ...
    aesthetic_family: str  # the design-system / aesthetic family leaned toward
    one_liner: str  # the declared "Reading this as: …" sentence
    signals: list[str] = field(default_factory=list)  # matched dial-signal ids
    needs_clarification: bool = False  # True when the brief is too ambiguous to read

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_kind": self.page_kind,
            "audience": self.audience,
            "vibe": self.vibe,
            "aesthetic_family": self.aesthetic_family,
            "one_liner": self.one_liner,
            "signals": list(self.signals),
            "needs_clarification": self.needs_clarification,
        }


@dataclass
class TasteProfile:
    """Resolved generation guidance for one deliverable.

    The producer reads this before generating: it pins the dials, the official
    design-system package to install (never hand-recreate), and the
    palette/type constraints that keep the brand from collapsing into the AI
    default.
    """

    design_read: DesignRead
    dials: TasteDials
    design_system: str  # package id, or "tailwind-v4-native" default
    design_system_install: str  # the install command (or "" for the native default)
    design_system_rationale: str
    palette_guidance: list[str] = field(default_factory=list)
    typography_guidance: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)  # hard rules carried into generation

    def to_dict(self) -> dict[str, Any]:
        return {
            "design_read": self.design_read.to_dict(),
            "dials": self.dials.to_dict(),
            "design_system": self.design_system,
            "design_system_install": self.design_system_install,
            "design_system_rationale": self.design_system_rationale,
            "palette_guidance": list(self.palette_guidance),
            "typography_guidance": list(self.typography_guidance),
            "constraints": list(self.constraints),
        }


@dataclass
class TasteViolation:
    """One failed Pre-Flight check."""

    check_id: str
    severity: str  # SEVERITY_FAIL | SEVERITY_WARN
    message: str
    evidence: str = ""  # the offending snippet / count, truncated
    weight: float = 0.1  # score penalty contributed by this violation

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "severity": self.severity,
            "message": self.message,
            "evidence": self.evidence,
            "weight": round(self.weight, 4),
        }


@dataclass
class TasteAuditResult:
    """Result of running the deterministic Pre-Flight audit over a deliverable."""

    score: float  # 0.0..1.0 (1.0 = no tells)
    grade: str  # A..F
    passed: bool  # True when no SEVERITY_FAIL present
    checks_run: list[str] = field(default_factory=list)
    violations: list[TasteViolation] = field(default_factory=list)
    rationale: list[str] = field(default_factory=list)

    @property
    def fail_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == SEVERITY_FAIL)

    @property
    def warn_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == SEVERITY_WARN)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "grade": self.grade,
            "passed": self.passed,
            "fail_count": self.fail_count,
            "warn_count": self.warn_count,
            "checks_run": list(self.checks_run),
            "violations": [v.to_dict() for v in self.violations],
            "rationale": list(self.rationale),
        }
