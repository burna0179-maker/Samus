#!/usr/bin/env python3
"""
CallSheet ProductRegistry — replaces if/else funnel logic with pluggable products
Source: ChatGPT recovery chat 03 (callsheet upgrade section)

Canonical relationship:
- [NEW pack] business/sales — product-registry pattern
- [EXPANDS §11 pack wiring] each product = pack-style registration

Solves: hard-coded SEO-vs-Website branch logic doesn't scale to
        ads / AI agents / CRM automation / reputation products.

Stacking: primary product (opener/pitch) + secondary product (fallback close).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Union


class QualificationReason(str, Enum):
    NO_WEBSITE = "no_website"
    SOCIAL_ONLY = "social_only"
    DIRECTORY_ONLY = "directory_only"
    POOR_SEO = "poor_seo"
    HIGH_MANUAL_WORKLOAD = "high_manual_workload"
    NO_AUTOMATION = "no_automation"
    LOW_LEAD_VOLUME = "low_lead_volume"
    NO_GOOGLE_ADS = "no_google_ads"
    LOW_REVIEWS = "low_reviews"
    BAD_REVIEWS = "bad_reviews"


@dataclass
class ProductConfig:
    name: str
    qualification_reasons: set
    offer_by_priority: Dict[str, str]
    pitch_by_priority: Dict[str, str]
    opener_by_priority: Dict[str, str]
    voicemail_by_priority: Dict[str, str]
    objections: List[str]
    issue_labels: Union[Dict[str, str], List[str]]


@dataclass
class CallSheet:
    company_name: str
    product: str
    call_priority: str
    top_issues: List[str]
    opener: str
    voicemail: str
    pitch_angle: str
    suggested_offer: str
    objection_handlers: List[str]
    call_strategy: Dict[str, Any] = field(default_factory=dict)
    secondary_product: Optional[str] = None


# ---------------------------------------------------------------------------
# Registry — extend by appending ProductConfig instances
# ---------------------------------------------------------------------------

PRODUCTS: List[ProductConfig] = []


def register_product(cfg: ProductConfig) -> None:
    PRODUCTS.append(cfg)


def resolve_product(reason: Optional[QualificationReason]) -> ProductConfig:
    for p in PRODUCTS:
        if reason in p.qualification_reasons:
            return p
    # fallback: first product whose qualification_reasons is empty = catch-all
    for p in PRODUCTS:
        if not p.qualification_reasons:
            return p
    raise LookupError("No matching product and no fallback registered")


def resolve_products_stack(reason: Optional[QualificationReason]) -> List[ProductConfig]:
    matched = [p for p in PRODUCTS if reason in p.qualification_reasons]
    if not matched and PRODUCTS:
        matched = [resolve_product(reason)]
    return matched


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def _priority_label(call_priority: Any) -> str:
    if hasattr(call_priority, "value"):
        return call_priority.value
    return str(call_priority or "default")


def _format_template(tpl: Optional[str], **kw) -> str:
    if not tpl:
        return ""
    try:
        return tpl.format(**kw)
    except (KeyError, IndexError):
        return tpl


def build_call_sheet(
    *,
    prospect: Any,
    company_name: str,
    qualification_reason: Optional[QualificationReason],
    seo_issues: Optional[List[str]] = None,
    signals: Optional[Dict[str, Any]] = None,
    confidence: float = 0.8,
) -> CallSheet:
    stack = resolve_products_stack(qualification_reason)
    primary = stack[0]
    secondary = stack[1].name if len(stack) > 1 else None

    priority = _priority_label(prospect.call_priority)

    offer = primary.offer_by_priority.get(priority) or primary.offer_by_priority.get("default", "")
    pitch = primary.pitch_by_priority.get(priority) or primary.pitch_by_priority.get("default", "")

    opener = _format_template(
        primary.opener_by_priority.get(priority) or primary.opener_by_priority.get("default"),
        company=company_name,
    )
    voicemail = _format_template(
        primary.voicemail_by_priority.get(priority) or primary.voicemail_by_priority.get("default"),
        company=company_name,
    )

    if isinstance(primary.issue_labels, dict):
        top_issues = [primary.issue_labels.get(k, k) for k in (seo_issues or [])[:3]]
    else:
        top_issues = list(primary.issue_labels)[:3]

    # Dynamic pitch injection from signals (high-impact upgrade)
    sig = signals or {}
    if sig.get("avg_rating", 5) < 4:
        pitch += " I also noticed your reviews may be impacting conversion."

    return CallSheet(
        company_name=company_name,
        product=primary.name,
        call_priority=priority,
        top_issues=top_issues,
        opener=opener,
        voicemail=voicemail,
        pitch_angle=pitch,
        suggested_offer=offer,
        objection_handlers=primary.objections[:3],
        call_strategy={
            "angle": "pain" if top_issues else "opportunity",
            "confidence": confidence,
            "product": primary.name,
        },
        secondary_product=secondary,
    )
