"""Deterministic generic fallback scaffold builder.

Served for any task kind not present in ``SCAFFOLD_LIBRARY`` — recovery must
never raise on an unknown kind. Pure Python, no I/O, no LLM, constant-time.
"""

from __future__ import annotations

from typing import Any

TEMPLATE_VERSION = "generic_template_v1"


def generic_template_v1(context: dict[str, Any]) -> str:
    """Render a safe generic deterministic scaffold from ``context``.

    Used when the requested task kind has no dedicated template. Echoes the
    business name when supplied; otherwise stays fully generic.
    """
    business = str(context.get("business_name") or "the target").strip()

    return (
        f"# Recovery Scaffold — {business}\n"
        f"\n"
        f"The originating LLM-driven step did not complete and no dedicated\n"
        f"template exists for this task kind. This generic scaffold lets the\n"
        f"workflow continue deterministically.\n"
        f"\n"
        f"## Suggested structure\n"
        f"  1. State the objective in one sentence.\n"
        f"  2. List the known inputs and constraints.\n"
        f"  3. Define the deliverable and its acceptance criteria.\n"
        f"  4. Outline the steps to produce it.\n"
        f"  5. Note any follow-up required once budget allows an LLM pass.\n"
        f"\n"
        f"_Deterministic generic recovery scaffold ({TEMPLATE_VERSION}). "
        f"Replace with a task-specific output when budget allows._\n"
    )


__all__ = ["generic_template_v1", "TEMPLATE_VERSION"]
