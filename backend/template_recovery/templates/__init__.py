"""Pre-validated deterministic template assets for template-recovery.

Every builder is a versioned, deterministic, pure-Python function: no I/O,
no LLM, no clock-dependence, constant-time. ``SCAFFOLD_LIBRARY`` maps a task
kind to its template builder. ``GENERIC_BUILDER`` is the safe fallback used
for any task kind absent from the library — recovery never raises on an
unknown kind.

``TEMPLATE_VERSIONS`` records the version string each builder advertises so
the service can report which exact template served a recovery.
"""

from __future__ import annotations

from typing import Any, Callable

from .callsheet import callsheet_template_v5
from .cold_outreach import outreach_template_v7
from .generic import generic_template_v1
from .proposal import proposal_template_v2
from .seo_audit import seo_template_v3

# Type alias for a template builder: a deterministic context -> scaffold fn.
ScaffoldBuilder = Callable[[dict[str, Any]], str]

# Task kind -> deterministic template builder.
SCAFFOLD_LIBRARY: dict[str, ScaffoldBuilder] = {
    "seo_audit": seo_template_v3,
    "proposal": proposal_template_v2,
    "cold_outreach": outreach_template_v7,
    "callsheet": callsheet_template_v5,
}

# Safe fallback builder for any task kind not in SCAFFOLD_LIBRARY.
GENERIC_BUILDER: ScaffoldBuilder = generic_template_v1

# Builder function -> advertised version string.
TEMPLATE_VERSIONS: dict[ScaffoldBuilder, str] = {
    seo_template_v3: "seo_template_v3",
    proposal_template_v2: "proposal_template_v2",
    outreach_template_v7: "outreach_template_v7",
    callsheet_template_v5: "callsheet_template_v5",
    generic_template_v1: "generic_template_v1",
}


def version_for(builder: ScaffoldBuilder) -> str:
    """Return the advertised version string for ``builder``."""
    return TEMPLATE_VERSIONS.get(builder, getattr(builder, "__name__", "unknown"))


__all__ = [
    "SCAFFOLD_LIBRARY",
    "GENERIC_BUILDER",
    "TEMPLATE_VERSIONS",
    "ScaffoldBuilder",
    "version_for",
    "seo_template_v3",
    "proposal_template_v2",
    "outreach_template_v7",
    "callsheet_template_v5",
    "generic_template_v1",
]
