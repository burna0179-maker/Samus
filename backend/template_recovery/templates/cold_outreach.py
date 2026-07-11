"""Deterministic cold-outreach scaffold builder.

Pure Python, no I/O, no LLM, constant-time. Same input -> identical output.
"""

from __future__ import annotations

from typing import Any

TEMPLATE_VERSION = "outreach_template_v7"


def outreach_template_v7(context: dict[str, Any]) -> str:
    """Render a deterministic cold-outreach message scaffold from ``context``.

    Recognised context keys (all optional): ``business_name``,
    ``contact_name``, ``industry``, ``sender_name``, ``offer``.
    """
    business = str(context.get("business_name") or "your business").strip()
    contact = str(context.get("contact_name") or "there").strip()
    industry = str(context.get("industry") or "your industry").strip()
    sender = str(context.get("sender_name") or "the Samus team").strip()
    offer = str(context.get("offer") or "a quick performance review").strip()

    return (
        f"Subject: A quick idea for {business}\n"
        f"\n"
        f"Hi {contact},\n"
        f"\n"
        f"I work with {industry} businesses and noticed a few opportunities "
        f"that could help {business} reach more customers.\n"
        f"\n"
        f"We can start with {offer} — no commitment, just a clear picture of "
        f"where the easy wins are.\n"
        f"\n"
        f"Would a 15-minute call this week work for you? If so, just reply "
        f"with a time that suits and I will send an invite.\n"
        f"\n"
        f"Best,\n"
        f"{sender}\n"
        f"\n"
        f"-- Deterministic recovery scaffold ({TEMPLATE_VERSION}). "
        f"Replace with a personalised LLM message when budget allows.\n"
    )


__all__ = ["outreach_template_v7", "TEMPLATE_VERSION"]
