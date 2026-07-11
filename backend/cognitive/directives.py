"""Directive loader — reads operator standpoint documents.

Directives ship WITH the package at ``backend/cognitive/directives_data/`` so
they survive the ``samus-data`` volume mount, which shadows the image's baked-in
``data/`` at ``/opt/samus/data`` (the old location — gitignored + shadowed, so
directives silently vanished in the container). The legacy ``data/identity/
directives/`` path is kept as a fallback for host/dev.

Pure filesystem reads; no heavy dependencies. Cached after first load per
directive name. Fail-safe: a missing or unreadable file returns an empty string
so the caller degrades without crashing.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

_LOG = logging.getLogger(__name__)

# Packaged location FIRST (container-safe, tracked in git, baked into the image
# under /opt/samus/backend/... which the data volume never shadows), then the
# legacy repo/host path as a fallback.
_DIRECTIVE_DIRS = (
    Path(__file__).resolve().parent / "directives_data",
    Path(__file__).resolve().parent.parent.parent / "data" / "identity" / "directives",
)


@lru_cache(maxsize=16)
def load_directive(name: str) -> str:
    """Load a directive by stem name (without .md extension).

    Returns the file contents as a string, or "" on any failure (checked across
    the packaged + legacy directories).
    """
    for base in _DIRECTIVE_DIRS:
        path = base / f"{name}.md"
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8").strip()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("directive %r unreadable at %s: %s", name, path, exc)
    _LOG.warning("directive %r not found in %s", name, [str(d) for d in _DIRECTIVE_DIRS])
    return ""


def strategic_intelligence_cycle() -> str:
    """Load the Strategic Daily Intelligence and Guidance Cycle standpoint."""
    return load_directive("strategic_intelligence_cycle")
