"""Archive subsystem — passive read-only archive scanner.

Source: Samus/recovery/archive_reasoning_pipeline.md (stages 1+2 + partial 4).

Passive scan + classify only; active generation deferred per memory rule
(Darwin owns mutation governance, self-modifying-code risk).
"""
from __future__ import annotations

from .scanner import (
    ArchiveArtifact,
    ArchiveScanner,
    ArtifactKind,
    ExtractedConcept,
    GapFinding,
    GapType,
    ScanReport,
)

__all__ = [
    "ArchiveArtifact",
    "ArchiveScanner",
    "ArtifactKind",
    "ExtractedConcept",
    "GapFinding",
    "GapType",
    "ScanReport",
]
