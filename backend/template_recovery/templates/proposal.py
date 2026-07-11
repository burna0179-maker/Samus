"""Deterministic proposal scaffold builder.

Pure Python, no I/O, no LLM, constant-time. Same input -> identical output.
"""
from __future__ import annotations

from typing import Any

TEMPLATE_VERSION = "proposal_template_v2"


def proposal_template_v2(context: dict[str, Any]) -> str:
    """Render a deterministic sales-proposal scaffold from ``context``.

    Recognised context keys (all optional): ``business_name``, ``offer``,
    ``contact_name``, ``price``.
    """
    business = str(context.get("business_name") or "the prospect").strip()
    offer = str(context.get("offer") or "our growth engagement").strip()
    contact = str(context.get("contact_name") or "there").strip()
    price = str(context.get("price") or "to be confirmed").strip()

    return (
        f"# Proposal Scaffold — {business}\n"
        f"\n"
        f"Hi {contact},\n"
        f"\n"
        f"## 1. Summary\n"
        f"  This proposal outlines {offer} for {business}.\n"
        f"\n"
        f"## 2. Objectives\n"
        f"  - Establish a measurable baseline for current performance.\n"
        f"  - Deliver a prioritised set of improvements within the engagement.\n"
        f"  - Report outcomes against agreed metrics.\n"
        f"\n"
        f"## 3. Scope of work\n"
        f"  - Discovery and baseline assessment.\n"
        f"  - Execution of the agreed action plan.\n"
        f"  - Review, reporting and handover.\n"
        f"\n"
        f"## 4. Timeline\n"
        f"  - Week 1: discovery and baseline.\n"
        f"  - Weeks 2-3: execution.\n"
        f"  - Week 4: review and reporting.\n"
        f"\n"
        f"## 5. Investment\n"
        f"  {price}\n"
        f"\n"
        f"## 6. Next step\n"
        f"  Reply to confirm and we will schedule the kickoff call.\n"
        f"\n"
        f"_Deterministic recovery scaffold ({TEMPLATE_VERSION}). "
        f"Replace with a tailored LLM proposal when budget allows._\n"
    )


__all__ = ["proposal_template_v2", "TEMPLATE_VERSION"]
