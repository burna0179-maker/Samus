"""Risk classification + approval decision per doc §3.4.

``classify_risk(objective, actions)`` term-matches against the lowercased
combination of objective + actions and returns ``(level, reasons)``.
``approval_decision(...)`` wraps it with explicit-approvals enforcement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


CRITICAL_RISK_TERMS: tuple[str, ...] = (
    "irreversible delete",
    "payment capture",
    "wire funds",
    "legal submission",
    "public launch without review",
    "delete production",
    "drop table",
    "purge",
)

HIGH_RISK_TERMS: tuple[str, ...] = (
    "production delete",
    "database migration",
    "financial transfer",
    "bulk outbound messaging",
    "credential rotation",
    "mass contact import",
    "schema migration",
    "force-push",
)

EXTERNAL_ACTION_TERMS: tuple[str, ...] = (
    "external messaging",
    "customer send",
    "publish",
    "deploy production",
    "send email",
    "post to social",
    "outbound call",
)


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    approved: bool
    risk_level: str
    reasons: list[str] = field(default_factory=list)
    required_approvals: list[str] = field(default_factory=list)


_REQUIRED_BY_LEVEL: dict[str, list[str]] = {
    "critical": ["owner", "governance"],
    "high": ["owner"],
    "normal": [],
}


def _normalize(objective: str, actions: Iterable[str]) -> str:
    parts = [objective or ""]
    parts.extend(a or "" for a in actions or ())
    return " ".join(parts).lower()


def classify_risk(
    objective: str,
    actions: Iterable[str] | None = None,
) -> tuple[str, list[str]]:
    """Return ``(level, reasons)`` — level ∈ {"critical", "high", "normal"}."""
    haystack = _normalize(objective, actions or ())
    reasons: list[str] = []

    for term in CRITICAL_RISK_TERMS:
        if term in haystack:
            reasons.append(f"critical term matched: {term}")
            return "critical", reasons

    matched_high = False
    for term in HIGH_RISK_TERMS:
        if term in haystack:
            reasons.append(f"high-risk term matched: {term}")
            matched_high = True
    for term in EXTERNAL_ACTION_TERMS:
        if term in haystack:
            reasons.append(f"external action matched: {term}")
            matched_high = True

    if matched_high:
        return "high", reasons

    return "normal", ["no elevated risk terms matched"]


def approval_decision(
    objective: str,
    actions: Iterable[str] | None = None,
    approvals: Iterable[str] | None = None,
) -> ApprovalDecision:
    """Classify + check explicit approvals (case-insensitive)."""
    level, reasons = classify_risk(objective, actions or ())
    required = list(_REQUIRED_BY_LEVEL.get(level, []))

    provided = {str(a).strip().lower() for a in (approvals or ())}
    missing = [r for r in required if r.lower() not in provided]

    if missing:
        reasons = list(reasons)
        reasons.append(f"required approvals missing: {', '.join(missing)}")
        return ApprovalDecision(False, level, reasons, required)

    return ApprovalDecision(True, level, list(reasons), required)
