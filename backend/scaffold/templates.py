"""Markdown templates for scaffold asset types (doc §6.templates)."""
from __future__ import annotations

from typing import Any


def _join_goals(goals: list[str]) -> str:
    if not goals:
        return "- (no goals supplied)"
    return "\n".join(f"- {g}" for g in goals)


def _sequence_block(sequence: list[dict[str, Any]]) -> str:
    lines = []
    for step in sequence:
        lines.append(f"{step.get('step','?')}. {step.get('message','')}")
    return "\n".join(lines) if lines else "(no sequence)"


def _proposal_pack(payload: dict[str, Any]) -> str:
    pos = payload.get("positioning", {})
    offer = payload.get("offer", {})
    return (
        f"# Proposal Pack — {payload.get('title','')}\n"
        f"**Client:** {payload.get('client','')}\n"
        f"**Brand voice:** {payload.get('brand_voice','')}\n\n"
        f"## Problem\n{pos.get('problem','')}\n\n"
        f"## Mechanism\n{pos.get('mechanism','')}\n\n"
        f"## Outcome\n{pos.get('outcome','')}\n\n"
        f"## Offer\n**{offer.get('headline','')}**\n\n"
        f"- Mechanism: {offer.get('mechanism','')}\n"
        f"- Outcome: {offer.get('outcome','')}\n"
        f"- Price anchor: {offer.get('price_anchor','')}\n\n"
        f"## Goals\n{_join_goals(payload.get('goals', []))}\n\n"
        f"## Outreach sequence\n{_sequence_block(payload.get('sequence', []))}\n"
    )


def _implementation_plan(payload: dict[str, Any]) -> str:
    pos = payload.get("positioning", {})
    offer = payload.get("offer", {})
    return (
        f"# Implementation Plan — {payload.get('title','')}\n"
        f"**Client:** {payload.get('client','')}\n\n"
        f"## Objective\n{pos.get('outcome','')}\n\n"
        f"## Mechanism\n{pos.get('mechanism','')}\n\n"
        f"## Workstreams\n"
        f"1. Discovery + baseline measurements.\n"
        f"2. Toolchain alignment and access provisioning.\n"
        f"3. Pilot of {offer.get('headline','the offer')} on one workflow.\n"
        f"4. Expand to adjacent workflows once pilot meets exit criteria.\n"
        f"5. Operate + iterate with weekly review cadence.\n\n"
        f"## Goals\n{_join_goals(payload.get('goals', []))}\n\n"
        f"## Risks\n"
        f"- Scope creep — re-anchor on the pilot workflow weekly.\n"
        f"- Tooling lock-in — keep the orchestration layer portable.\n"
        f"- Approval drag — confirm sign-off authority on day 1.\n"
    )


def _operating_brief(payload: dict[str, Any]) -> str:
    pos = payload.get("positioning", {})
    return (
        f"# Operating Brief — {payload.get('title','')}\n"
        f"**Client:** {payload.get('client','')}\n"
        f"**Brand voice:** {payload.get('brand_voice','')}\n\n"
        f"## Situation\n{pos.get('problem','')}\n\n"
        f"## How we operate\n{pos.get('mechanism','')}\n\n"
        f"## What success looks like\n{pos.get('outcome','')}\n\n"
        f"## Cadence\n"
        f"- Daily: standup against the active workstream.\n"
        f"- Weekly: stakeholder review + metric refresh.\n"
        f"- Monthly: governance review with audit-event sample.\n\n"
        f"## Goals\n{_join_goals(payload.get('goals', []))}\n"
    )


def _campaign_brief(payload: dict[str, Any]) -> str:
    offer = payload.get("offer", {})
    return (
        f"# Campaign Brief — {payload.get('title','')}\n"
        f"**Client:** {payload.get('client','')}\n"
        f"**Brand voice:** {payload.get('brand_voice','')}\n\n"
        f"## Offer\n**{offer.get('headline','')}**\n\n"
        f"- Mechanism: {offer.get('mechanism','')}\n"
        f"- Outcome: {offer.get('outcome','')}\n"
        f"- Price anchor: {offer.get('price_anchor','')}\n\n"
        f"## Goals\n{_join_goals(payload.get('goals', []))}\n\n"
        f"## Sequence\n{_sequence_block(payload.get('sequence', []))}\n\n"
        f"## Measurement\n"
        f"- Reply rate per step.\n"
        f"- Booked-call rate.\n"
        f"- Pipeline value created within 30 days of send.\n"
    )


_RENDERERS = {
    "proposal_pack": _proposal_pack,
    "implementation_plan": _implementation_plan,
    "operating_brief": _operating_brief,
    "campaign_brief": _campaign_brief,
}


def render_template(asset_type: str, payload: dict[str, Any]) -> str:
    """Render a Markdown document for ``asset_type``."""
    renderer = _RENDERERS.get(asset_type)
    if renderer is None:
        return f"# {payload.get('title','asset')}\n\n(unknown asset_type: {asset_type})\n"
    return renderer(payload)
