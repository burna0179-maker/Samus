"""Design-taste deliverable-quality subsystem (anti-slop frontend governance).

Samus is the only HustleForge agent that produces client-facing deliverables as
a revenue product (scaffold proposals, brand copy, and — in a later phase —
generated frontend code). This package is the quality organ for those
deliverables. It has two separable halves:

  * ``profile`` — brief-inference -> three dials (variance / motion / density)
    -> official design-system selection + palette/type guidance. Generation
    guidance, fed into a producer's prompt or template choice.

  * ``audit`` — a deterministic Pre-Flight gate: the em-dash ban, eyebrow
    restraint, banned palette/font families, and the AI-tell scan. Pure-stdlib,
    zero-LLM, so it runs inside the $1/day global cap without spending a cent.

The audit emits a ``TasteAuditResult`` shaped to slot into the PDC composite
(``backend.governance.pdc_composite``) next to ``ElegancePlan`` and
``ConfusionScore`` as a fifth quality dimension.

Source of the encoded rules: the open ``Leonxlnx/taste-skill`` skill, mapped
onto Samus's domain. Dormant by default — see SAMUS_TASTE_* settings.
"""

from __future__ import annotations

from .audit import audit_deliverable, audit_text
from .models import (
    DesignRead,
    TasteAuditResult,
    TasteDials,
    TasteProfile,
    TasteViolation,
)
from .profile import build_profile, infer_design_read, resolve_dials, select_design_system

__all__ = [
    "DesignRead",
    "TasteAuditResult",
    "TasteDials",
    "TasteProfile",
    "TasteViolation",
    "audit_deliverable",
    "audit_text",
    "build_profile",
    "infer_design_read",
    "resolve_dials",
    "select_design_system",
]
