"""Deterministic call-sheet scaffold builder.

Pure Python, no I/O, no LLM, constant-time. Same input -> identical output.
"""
from __future__ import annotations

from typing import Any

TEMPLATE_VERSION = "callsheet_template_v5"


def callsheet_template_v5(context: dict[str, Any]) -> str:
    """Render a deterministic call-sheet scaffold from ``context``.

    Recognised context keys (all optional): ``business_name``,
    ``contact_name``, ``phone``, ``industry``, ``offer``.
    """
    business = str(context.get("business_name") or "the prospect").strip()
    contact = str(context.get("contact_name") or "the decision maker").strip()
    phone = str(context.get("phone") or "(not supplied)").strip()
    industry = str(context.get("industry") or "general").strip()
    offer = str(context.get("offer") or "a performance review").strip()

    return (
        f"# Call Sheet — {business}\n"
        f"\n"
        f"Contact: {contact}\n"
        f"Phone: {phone}\n"
        f"Industry: {industry}\n"
        f"\n"
        f"## Opening\n"
        f"  \"Hi {contact}, this is the Samus team — do you have a quick "
        f"minute?\"\n"
        f"\n"
        f"## Purpose\n"
        f"  Introduce {offer} for {business}.\n"
        f"\n"
        f"## Discovery questions\n"
        f"  1. What is working well for you right now?\n"
        f"  2. Where do you feel you are leaving customers on the table?\n"
        f"  3. What have you already tried to fix that?\n"
        f"\n"
        f"## Value points\n"
        f"  - Measurable baseline before any work starts.\n"
        f"  - Prioritised action plan, not a vague promise.\n"
        f"  - Clear reporting against agreed metrics.\n"
        f"\n"
        f"## Objection handling\n"
        f"  - \"No budget\": start with the free baseline review.\n"
        f"  - \"No time\": the call is 15 minutes; we do the heavy lifting.\n"
        f"  - \"Already have someone\": offer a second-opinion audit.\n"
        f"\n"
        f"## Close\n"
        f"  Book the 15-minute review call and confirm the calendar invite.\n"
        f"\n"
        f"_Deterministic recovery scaffold ({TEMPLATE_VERSION}). "
        f"Replace with a tailored LLM call sheet when budget allows._\n"
    )


__all__ = ["callsheet_template_v5", "TEMPLATE_VERSION"]
