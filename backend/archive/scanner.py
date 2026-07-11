"""Passive archive scanner — read-only inspector for archive trees.

Source: Samus/recovery/archive_reasoning_pipeline.md

Implements ONLY the passive half of the Continuous Archive Reasoning Pipeline:
  - Stage 1 INGEST   — walk an archive directory, hash + classify each artifact.
  - Stage 2 PROCESS  — extract concepts (classes / defs / markdown headers).
  - Stage 4 (partial) — classify each concept as
        NEW_CONCEPT | EXTENDS_SUBSYSTEM | MISSING_IMPL | IMPLIES_FEATURE
    against a snapshot of (system_capabilities, system_modules).

Passive scan + classify only; active generation deferred per memory rule
(Darwin owns mutation governance, self-modifying-code risk).

EXPLICITLY EXCLUDED (deferred to Darwin / governance review):
  - Stage 3 REASONING EXPANSION (code generation)
  - Stage 5 OUTPUT GENERATION  (writing .py files)
  - Stage 7 CONTINUOUS IMPROVEMENT (refactoring existing modules)
  - ARCHIVE MANAGEMENT auto-move to processed/ or rejected/

Hard rules enforced by this module:
  - READ-ONLY: no file mutations, renames, deletions, or shell-outs.
  - No network, no LLM calls. Pure stdlib only.
  - Files > 1 MiB and binary files are skipped (recorded but no concepts).
  - All output is returned as a frozen ``ScanReport`` for downstream review;
    nothing is auto-routed anywhere.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

MAX_FILE_BYTES: int = 1024 * 1024  # 1 MiB — skip larger files.

# Suffixes that look like a "feature" / orchestration component when found at the
# tail of an extracted concept name. Matching is suffix-aware (concept name ends
# with the token).
_FEATURE_SUFFIXES: tuple[str, ...] = (
    "_engine",
    "_pipeline",
    "_orchestrator",
    "_manager",
    "_worker",
    "_scorer",
)

# Patterns for top-level class / def in a Python module (no leading whitespace).
_PY_TOP_CLASS_RE = re.compile(r"^class\s+([A-Za-z_][A-Za-z0-9_]*)")
_PY_TOP_DEF_RE = re.compile(r"^def\s+([A-Za-z_][A-Za-z0-9_]*)")

# Markdown headers (only #, ##).
_MD_H1_RE = re.compile(r"^#\s+(.+?)\s*#*\s*$")
_MD_H2_RE = re.compile(r"^##\s+(.+?)\s*#*\s*$")

# Inside an .md line: a Python class or def declaration.
_MD_INLINE_CLASS_OR_DEF_RE = re.compile(r"\b(?:class\s+\w+|def\s+\w+\s*\()")

# Snake-case canonicalization helpers.
_NON_WORD_RE = re.compile(r"[^\w\s\-]+", flags=re.UNICODE)
_WS_HYPHEN_RE = re.compile(r"[\s\-]+", flags=re.UNICODE)

# Schema-marker suffixes (per heuristic spec).
_SCHEMA_SUFFIXES: frozenset[str] = frozenset({".sql", ".yaml", ".yml", ".json"})


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class ArtifactKind(str, Enum):
    PYTHON_MODULE = "python_module"  # .py
    DESIGN_DOC = "design_doc"        # .md
    SCHEMA = "schema"                # .sql, .yaml, .yml, .json
    UNKNOWN = "unknown"


class GapType(str, Enum):
    NEW_CONCEPT = "new_concept"               # introduces concept not in system
    EXTENDS_SUBSYSTEM = "extends_subsystem"   # extends existing capability/module
    MISSING_IMPL = "missing_impl"             # implies impl that doesn't exist
    IMPLIES_FEATURE = "implies_feature"       # implies non-existent feature


# --------------------------------------------------------------------------- #
# Data classes (all frozen — read-only)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ArchiveArtifact:
    """A single file discovered in the archive tree."""

    path: str           # relative path from scan root (POSIX-style)
    kind: ArtifactKind
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class ExtractedConcept:
    """A concept/architecture/algorithm/feature name extracted from an artifact."""

    name: str           # canonicalized snake_case
    source_path: str
    line_number: int
    excerpt: str        # ~120 chars of surrounding context
    confidence: float   # 0.0-1.0 heuristic


@dataclass(frozen=True)
class GapFinding:
    concept: ExtractedConcept
    gap_type: GapType
    rationale: str
    candidate_modules: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScanReport:
    scan_root: str
    artifacts: tuple[ArchiveArtifact, ...]
    concepts: tuple[ExtractedConcept, ...]
    gaps: tuple[GapFinding, ...]
    timestamp: str  # ISO8601 UTC


# --------------------------------------------------------------------------- #
# Helpers — pure functions
# --------------------------------------------------------------------------- #


def _canonicalize(text: str) -> str:
    """Lower-case snake_case canonicalization.

    Rules:
      - Strip leading/trailing whitespace and punctuation.
      - Drop characters that are not word-chars, whitespace, or hyphen.
      - Collapse whitespace + hyphen runs into a single underscore.
      - Lower-case.
    """
    cleaned = _NON_WORD_RE.sub("", text).strip()
    cleaned = _WS_HYPHEN_RE.sub("_", cleaned)
    # Collapse repeated underscores from successive non-word stripping.
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_").lower()


def _excerpt(line: str, limit: int = 120) -> str:
    """Return a single-line excerpt, trimmed to ``limit`` chars."""
    flat = line.strip()
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"  # ellipsis


def _sha256_bytes(blob: bytes) -> str:
    h = hashlib.sha256()
    h.update(blob)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# ArchiveScanner
# --------------------------------------------------------------------------- #


class ArchiveScanner:
    """READ-ONLY scanner.

    No file mutations, no code generation, no auto-routing.

    Parameters
    ----------
    system_capabilities:
        Flat set of capability names the live system already provides
        (e.g., ``SERVICE_CAPABILITIES`` flattened across services).
    system_modules:
        Set of dotted module paths that already exist
        (e.g., ``"backend.fulfillment.dag"``).
    """

    def __init__(
        self,
        system_capabilities: set[str],
        system_modules: set[str],
    ) -> None:
        # Defensive copies — caller mutations after construction must not leak.
        self._capabilities: frozenset[str] = frozenset(system_capabilities)
        self._modules: frozenset[str] = frozenset(system_modules)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def scan(
        self,
        scan_root: Path,
        include_globs: tuple[str, ...] = ("**/*.py", "**/*.md"),
    ) -> ScanReport:
        """Walk ``scan_root``, classify each file, extract concepts, classify gaps.

        Does NOT write anything; does NOT move/rename source files.
        """
        scan_root = Path(scan_root)
        if not scan_root.exists():
            raise FileNotFoundError(f"scan_root does not exist: {scan_root}")
        if not scan_root.is_dir():
            raise NotADirectoryError(f"scan_root is not a directory: {scan_root}")

        # Deduplicate paths across globs; deterministic ordering.
        seen: set[Path] = set()
        all_paths: list[Path] = []
        for pattern in include_globs:
            for candidate in scan_root.glob(pattern):
                if candidate.is_file() and candidate not in seen:
                    seen.add(candidate)
                    all_paths.append(candidate)
        all_paths.sort()

        artifacts: list[ArchiveArtifact] = []
        concepts: list[ExtractedConcept] = []

        for file_path in all_paths:
            try:
                blob = file_path.read_bytes()
            except OSError as exc:  # pragma: no cover — defensive
                logger.warning(
                    "archive_scanner skip path=%s reason=read_error err=%s",
                    file_path,
                    exc,
                )
                continue

            kind = self.classify_artifact(file_path)
            rel_path = file_path.relative_to(scan_root).as_posix()
            artifact = ArchiveArtifact(
                path=rel_path,
                kind=kind,
                size_bytes=len(blob),
                sha256=_sha256_bytes(blob),
            )
            artifacts.append(artifact)

            # Size gate.
            if artifact.size_bytes > MAX_FILE_BYTES:
                logger.warning(
                    "archive_scanner skip path=%s reason=size_over_limit bytes=%d",
                    rel_path,
                    artifact.size_bytes,
                )
                continue

            # Decode gate (binary files fail UTF-8).
            try:
                content = blob.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning(
                    "archive_scanner skip path=%s reason=binary_or_non_utf8",
                    rel_path,
                )
                continue

            concepts.extend(self.extract_concepts(artifact, content))

        gaps: list[GapFinding] = [self.classify_gap(c) for c in concepts]

        return ScanReport(
            scan_root=str(scan_root),
            artifacts=tuple(artifacts),
            concepts=tuple(concepts),
            gaps=tuple(gaps),
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    # ------------------------------------------------------------------ #
    # Building blocks (exposed for unit tests)
    # ------------------------------------------------------------------ #

    def classify_artifact(self, path: Path) -> ArtifactKind:
        """Classify by file suffix only — deterministic, no content sniffing."""
        suffix = path.suffix.lower()
        if suffix == ".py":
            return ArtifactKind.PYTHON_MODULE
        if suffix == ".md":
            return ArtifactKind.DESIGN_DOC
        if suffix in _SCHEMA_SUFFIXES:
            return ArtifactKind.SCHEMA
        return ArtifactKind.UNKNOWN

    def extract_concepts(
        self,
        artifact: ArchiveArtifact,
        content: str,
    ) -> list[ExtractedConcept]:
        """Extract concepts based on artifact kind.

        Python: top-level ``class`` / ``def`` lines only (no leading whitespace,
        therefore no nested classes / methods).

        Markdown: ``# Foo`` and ``## Foo`` headers, snake_case canonicalized.
        Markdown headers whose source line ALSO contains a ``class X`` /
        ``def x(`` pattern are flagged with confidence 0.8 so that
        :meth:`classify_gap` can route them to ``MISSING_IMPL``.

        Other artifact kinds: returns ``[]``.
        """
        if artifact.kind == ArtifactKind.PYTHON_MODULE:
            return self._extract_py_concepts(artifact, content)
        if artifact.kind == ArtifactKind.DESIGN_DOC:
            return self._extract_md_concepts(artifact, content)
        return []

    def classify_gap(self, concept: ExtractedConcept) -> GapFinding:
        """Classify a concept against the live system snapshot.

        Order of precedence:
          1. MISSING_IMPL  — design-doc concept (confidence == 0.8 sentinel) that
                             names a ``class``/``def`` not present in the system.
          2. EXTENDS_SUBSYSTEM — capability already present, or a system module
                                 ends with ``.{concept.name}``.
          3. IMPLIES_FEATURE   — name suffix matches a known action/verb suffix.
          4. NEW_CONCEPT       — fallthrough.
        """
        name = concept.name
        candidates = self._candidate_modules(name)

        in_caps = name in self._capabilities
        impl_exists = any(m.endswith("." + name) or m == name for m in self._modules)

        # 1. MISSING_IMPL (design-doc sentinel)
        # Confidence 0.8 is set by _extract_md_concepts ONLY when the markdown
        # header line contained a `class X` / `def x(` declaration.
        if (
            concept.confidence == 0.8
            and not in_caps
            and not impl_exists
        ):
            return GapFinding(
                concept=concept,
                gap_type=GapType.MISSING_IMPL,
                rationale=(
                    f"design doc references identifier '{name}' but neither a "
                    f"matching capability nor module exists"
                ),
                candidate_modules=candidates,
            )

        # 2. EXTENDS_SUBSYSTEM
        if in_caps:
            return GapFinding(
                concept=concept,
                gap_type=GapType.EXTENDS_SUBSYSTEM,
                rationale=f"capability '{name}' already registered in system",
                candidate_modules=candidates,
            )
        if impl_exists:
            return GapFinding(
                concept=concept,
                gap_type=GapType.EXTENDS_SUBSYSTEM,
                rationale=f"existing module already implements '{name}'",
                candidate_modules=candidates,
            )

        # 3. IMPLIES_FEATURE
        if any(name.endswith(sfx) for sfx in _FEATURE_SUFFIXES):
            return GapFinding(
                concept=concept,
                gap_type=GapType.IMPLIES_FEATURE,
                rationale=(
                    f"name '{name}' has action/verb suffix, implies a non-existent "
                    f"feature component"
                ),
                candidate_modules=candidates,
            )

        # 4. NEW_CONCEPT
        return GapFinding(
            concept=concept,
            gap_type=GapType.NEW_CONCEPT,
            rationale=f"'{name}' not found in capabilities or modules",
            candidate_modules=candidates,
        )

    # ------------------------------------------------------------------ #
    # Internal extractors
    # ------------------------------------------------------------------ #

    def _extract_py_concepts(
        self,
        artifact: ArchiveArtifact,
        content: str,
    ) -> list[ExtractedConcept]:
        out: list[ExtractedConcept] = []
        for idx, line in enumerate(content.splitlines(), start=1):
            # Top-level means line begins with `class`/`def` — no indentation.
            m_class = _PY_TOP_CLASS_RE.match(line)
            if m_class:
                out.append(
                    ExtractedConcept(
                        name=_canonicalize(m_class.group(1)),
                        source_path=artifact.path,
                        line_number=idx,
                        excerpt=_excerpt(line),
                        confidence=0.9,
                    )
                )
                continue
            m_def = _PY_TOP_DEF_RE.match(line)
            if m_def:
                out.append(
                    ExtractedConcept(
                        name=_canonicalize(m_def.group(1)),
                        source_path=artifact.path,
                        line_number=idx,
                        excerpt=_excerpt(line),
                        confidence=0.7,
                    )
                )
        return out

    def _extract_md_concepts(
        self,
        artifact: ArchiveArtifact,
        content: str,
    ) -> list[ExtractedConcept]:
        out: list[ExtractedConcept] = []
        for idx, line in enumerate(content.splitlines(), start=1):
            m_h1 = _MD_H1_RE.match(line)
            m_h2 = None if m_h1 else _MD_H2_RE.match(line)

            if not (m_h1 or m_h2):
                continue

            header_text = (m_h1 or m_h2).group(1)
            name = _canonicalize(header_text)
            if not name:
                continue

            # Sentinel: header line contains a Python class/def declaration.
            # E.g. "## class FooEngine" or "# def run_pipeline(...)".
            if _MD_INLINE_CLASS_OR_DEF_RE.search(line):
                confidence = 0.8
            else:
                confidence = 0.7 if m_h1 else 0.5

            out.append(
                ExtractedConcept(
                    name=name,
                    source_path=artifact.path,
                    line_number=idx,
                    excerpt=_excerpt(line),
                    confidence=confidence,
                )
            )
        return out

    # ------------------------------------------------------------------ #
    # Candidate module lookup
    # ------------------------------------------------------------------ #

    def _candidate_modules(self, name: str, limit: int = 5) -> tuple[str, ...]:
        """Substring match ``name`` against known modules; cap at ``limit``."""
        if not name:
            return ()
        hits = sorted(m for m in self._modules if name in m)
        return tuple(hits[:limit])
