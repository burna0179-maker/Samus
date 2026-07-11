"""Tests for the passive archive scanner.

Covers:
  - classify_artifact across .py / .md / .sql / unknown suffixes
  - extract_concepts for Python (top-level class/def, skip nested)
  - extract_concepts for Markdown (h1/h2, snake_case canonicalization)
  - classify_gap for EXTENDS_SUBSYSTEM / IMPLIES_FEATURE / NEW_CONCEPT / MISSING_IMPL
  - scan walks subdirectories, skips binary and >1 MiB files
  - ScanReport timestamp is ISO8601
  - Scanner is read-only (no mutation of the source tree)
  - candidate_modules substring matching
  - sha256 hashing is deterministic
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

from backend.archive.scanner import (
    ArchiveArtifact,
    ArchiveScanner,
    ArtifactKind,
    ExtractedConcept,
    GapType,
    ScanReport,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def empty_scanner() -> ArchiveScanner:
    """Scanner with no known capabilities or modules."""
    return ArchiveScanner(system_capabilities=set(), system_modules=set())


@pytest.fixture
def loaded_scanner() -> ArchiveScanner:
    """Scanner that knows about a handful of capabilities + modules."""
    return ArchiveScanner(
        system_capabilities={"discover", "score", "qualify"},
        system_modules={
            "backend.fulfillment.dag",
            "backend.fulfillment.worker",
            "backend.outreach.engine",
            "backend.optimizer.bandit",
        },
    )


# --------------------------------------------------------------------------- #
# classify_artifact
# --------------------------------------------------------------------------- #


def test_classify_artifact_py(empty_scanner: ArchiveScanner) -> None:
    assert empty_scanner.classify_artifact(Path("foo.py")) is ArtifactKind.PYTHON_MODULE


def test_classify_artifact_md(empty_scanner: ArchiveScanner) -> None:
    assert empty_scanner.classify_artifact(Path("doc.md")) is ArtifactKind.DESIGN_DOC


def test_classify_artifact_sql(empty_scanner: ArchiveScanner) -> None:
    assert empty_scanner.classify_artifact(Path("schema.sql")) is ArtifactKind.SCHEMA
    assert empty_scanner.classify_artifact(Path("conf.yaml")) is ArtifactKind.SCHEMA
    assert empty_scanner.classify_artifact(Path("conf.yml")) is ArtifactKind.SCHEMA
    assert empty_scanner.classify_artifact(Path("data.json")) is ArtifactKind.SCHEMA


def test_classify_artifact_unknown(empty_scanner: ArchiveScanner) -> None:
    assert empty_scanner.classify_artifact(Path("notes.txt")) is ArtifactKind.UNKNOWN
    assert empty_scanner.classify_artifact(Path("README")) is ArtifactKind.UNKNOWN
    assert empty_scanner.classify_artifact(Path("image.png")) is ArtifactKind.UNKNOWN


# --------------------------------------------------------------------------- #
# extract_concepts — Python
# --------------------------------------------------------------------------- #


def _py_artifact(path: str = "mod.py", size: int = 100) -> ArchiveArtifact:
    return ArchiveArtifact(
        path=path,
        kind=ArtifactKind.PYTHON_MODULE,
        size_bytes=size,
        sha256="0" * 64,
    )


def test_extract_concepts_py_finds_class_and_def(empty_scanner: ArchiveScanner) -> None:
    content = "import os\n\nclass FooBar:\n    pass\n\ndef run_pipeline():\n    return 1\n"
    concepts = empty_scanner.extract_concepts(_py_artifact(), content)
    names = {c.name: c for c in concepts}

    assert set(names) == {"foobar", "run_pipeline"}
    # Confidence per heuristic: class=0.9, def=0.7.
    assert names["foobar"].confidence == 0.9
    assert names["run_pipeline"].confidence == 0.7
    # Line numbers are 1-based.
    assert names["foobar"].line_number == 3
    assert names["run_pipeline"].line_number == 6


def test_extract_concepts_py_skips_nested_class(empty_scanner: ArchiveScanner) -> None:
    """A class defined inside another class (indented) must NOT be extracted."""
    content = (
        "class Outer:\n"
        "    class Inner:\n"
        "        pass\n"
        "\n"
        "    def method(self):\n"
        "        return 1\n"
        "\n"
        "def top_level():\n"
        "    pass\n"
    )
    concepts = empty_scanner.extract_concepts(_py_artifact(), content)
    names = {c.name for c in concepts}

    assert "outer" in names
    assert "top_level" in names
    # Nested class + method are indented → not top-level → not extracted.
    assert "inner" not in names
    assert "method" not in names


# --------------------------------------------------------------------------- #
# extract_concepts — Markdown
# --------------------------------------------------------------------------- #


def _md_artifact(path: str = "doc.md", size: int = 100) -> ArchiveArtifact:
    return ArchiveArtifact(
        path=path,
        kind=ArtifactKind.DESIGN_DOC,
        size_bytes=size,
        sha256="0" * 64,
    )


def test_extract_concepts_md_finds_h1_and_h2(empty_scanner: ArchiveScanner) -> None:
    content = "# Top Title\n\nSome body text.\n\n## Subsection\n\n### Deeper (h3 — ignored)\n"
    concepts = empty_scanner.extract_concepts(_md_artifact(), content)
    names = {c.name: c for c in concepts}

    assert set(names) == {"top_title", "subsection"}
    assert names["top_title"].confidence == 0.7
    assert names["subsection"].confidence == 0.5


def test_extract_concepts_md_canonicalizes_header_to_snake_case(
    empty_scanner: ArchiveScanner,
) -> None:
    content = "# My Cool Feature!\n"
    concepts = empty_scanner.extract_concepts(_md_artifact(), content)
    assert len(concepts) == 1
    assert concepts[0].name == "my_cool_feature"


# --------------------------------------------------------------------------- #
# classify_gap
# --------------------------------------------------------------------------- #


def test_classify_gap_extends_subsystem_when_capability_exists(
    loaded_scanner: ArchiveScanner,
) -> None:
    concept = ExtractedConcept(
        name="discover",
        source_path="x.py",
        line_number=1,
        excerpt="def discover():",
        confidence=0.7,
    )
    finding = loaded_scanner.classify_gap(concept)
    assert finding.gap_type is GapType.EXTENDS_SUBSYSTEM
    assert "capability" in finding.rationale


def test_classify_gap_extends_subsystem_when_module_exists(
    loaded_scanner: ArchiveScanner,
) -> None:
    concept = ExtractedConcept(
        name="dag",
        source_path="x.py",
        line_number=1,
        excerpt="class Dag:",
        confidence=0.9,
    )
    finding = loaded_scanner.classify_gap(concept)
    assert finding.gap_type is GapType.EXTENDS_SUBSYSTEM
    assert "module" in finding.rationale


def test_classify_gap_implies_feature_for_engine_suffix(
    loaded_scanner: ArchiveScanner,
) -> None:
    concept = ExtractedConcept(
        name="lead_scoring_engine",
        source_path="x.py",
        line_number=1,
        excerpt="class LeadScoringEngine:",
        confidence=0.9,
    )
    finding = loaded_scanner.classify_gap(concept)
    assert finding.gap_type is GapType.IMPLIES_FEATURE


def test_classify_gap_new_concept_for_unknown(loaded_scanner: ArchiveScanner) -> None:
    concept = ExtractedConcept(
        name="totally_novel_thing",
        source_path="x.py",
        line_number=1,
        excerpt="class TotallyNovelThing:",
        confidence=0.9,
    )
    finding = loaded_scanner.classify_gap(concept)
    assert finding.gap_type is GapType.NEW_CONCEPT


def test_classify_gap_missing_impl_for_md_class_reference(
    loaded_scanner: ArchiveScanner,
) -> None:
    """Markdown header line that names a class/def + the impl is absent."""
    content = "## class GhostService\n"
    concepts = loaded_scanner.extract_concepts(_md_artifact(), content)
    assert len(concepts) == 1
    # Sentinel: extracted at confidence 0.8 because line contains "class X".
    assert concepts[0].confidence == 0.8

    finding = loaded_scanner.classify_gap(concepts[0])
    assert finding.gap_type is GapType.MISSING_IMPL


# --------------------------------------------------------------------------- #
# scan() walking + skip rules
# --------------------------------------------------------------------------- #


def test_scan_walks_subdirectories(loaded_scanner: ArchiveScanner, tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b").mkdir()
    (tmp_path / "top.py").write_text("class TopLevel:\n    pass\n", encoding="utf-8")
    (tmp_path / "a" / "mid.py").write_text("def mid_helper():\n    pass\n", encoding="utf-8")
    (tmp_path / "a" / "b" / "deep.md").write_text("# Deep Idea\n", encoding="utf-8")

    report = loaded_scanner.scan(tmp_path)
    paths = {a.path for a in report.artifacts}
    assert paths == {"top.py", "a/mid.py", "a/b/deep.md"}

    concept_names = {c.name for c in report.concepts}
    assert concept_names == {"toplevel", "mid_helper", "deep_idea"}


def test_scan_skips_binary_files(loaded_scanner: ArchiveScanner, tmp_path: Path) -> None:
    # A non-UTF8 byte sequence inside a .py file — must be recorded but
    # produce zero concepts (decode failure handled silently).
    binary_bytes = b"\xff\xfe\x00\x01class Foo:\xff\n"
    (tmp_path / "weird.py").write_bytes(binary_bytes)
    (tmp_path / "ok.py").write_text("class Ok:\n    pass\n", encoding="utf-8")

    report = loaded_scanner.scan(tmp_path)
    artifact_paths = {a.path for a in report.artifacts}
    assert artifact_paths == {"weird.py", "ok.py"}

    # Only the UTF-8 file produced a concept.
    concept_paths = {c.source_path for c in report.concepts}
    assert concept_paths == {"ok.py"}


def test_scan_skips_files_over_1mib(loaded_scanner: ArchiveScanner, tmp_path: Path) -> None:
    # >1 MiB Python file with a real class declaration — must NOT yield a concept.
    padding = "# pad\n" * (200_000)  # ~1.2 MiB
    big_content = "class HugeClass:\n    pass\n" + padding
    big_path = tmp_path / "huge.py"
    big_path.write_text(big_content, encoding="utf-8")
    assert big_path.stat().st_size > 1024 * 1024

    (tmp_path / "small.py").write_text("class Small:\n    pass\n", encoding="utf-8")

    report = loaded_scanner.scan(tmp_path)
    paths = {a.path for a in report.artifacts}
    assert paths == {"huge.py", "small.py"}

    concept_names = {c.name for c in report.concepts}
    # HugeClass must NOT appear; Small must.
    assert concept_names == {"small"}


def test_scan_report_has_iso8601_timestamp(loaded_scanner: ArchiveScanner, tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("class X:\n    pass\n", encoding="utf-8")
    report = loaded_scanner.scan(tmp_path)

    # Format: YYYY-MM-DDTHH:MM:SSZ
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", report.timestamp)
    # Parseable as an actual datetime.
    parsed = datetime.strptime(report.timestamp, "%Y-%m-%dT%H:%M:%SZ")
    assert parsed.year >= 2024


# --------------------------------------------------------------------------- #
# Read-only invariant
# --------------------------------------------------------------------------- #


def test_scanner_is_read_only(loaded_scanner: ArchiveScanner, tmp_path: Path) -> None:
    """After scan(), every source file must be byte-identical and same mtime."""
    file_a = tmp_path / "a.py"
    file_b = tmp_path / "sub" / "b.md"
    file_a.write_text("class A:\n    pass\n", encoding="utf-8")
    file_b.parent.mkdir()
    file_b.write_text("# Heading\n", encoding="utf-8")

    snapshot = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (file_a, file_b)}

    report = loaded_scanner.scan(tmp_path)
    assert isinstance(report, ScanReport)

    # Source files still present + unchanged.
    for path, (original_bytes, original_mtime_ns) in snapshot.items():
        assert path.exists(), f"scanner deleted source file: {path}"
        assert path.read_bytes() == original_bytes, f"scanner mutated bytes of: {path}"
        assert path.stat().st_mtime_ns == original_mtime_ns, f"scanner touched mtime of: {path}"

    # No stray output directories created by the scanner.
    children = {p.name for p in tmp_path.iterdir()}
    assert children == {"a.py", "sub"}


# --------------------------------------------------------------------------- #
# candidate_modules
# --------------------------------------------------------------------------- #


def test_candidate_modules_substring_match(loaded_scanner: ArchiveScanner) -> None:
    concept = ExtractedConcept(
        name="dag",
        source_path="x.py",
        line_number=1,
        excerpt="class Dag:",
        confidence=0.9,
    )
    finding = loaded_scanner.classify_gap(concept)
    # "backend.fulfillment.dag" contains "dag".
    assert "backend.fulfillment.dag" in finding.candidate_modules
    # All candidates must contain the substring.
    for cand in finding.candidate_modules:
        assert "dag" in cand


def test_candidate_modules_capped_at_five() -> None:
    modules = {f"backend.foo.bar_{i}_target" for i in range(20)}
    scanner = ArchiveScanner(system_capabilities=set(), system_modules=modules)
    concept = ExtractedConcept(
        name="target",
        source_path="x.py",
        line_number=1,
        excerpt="class Target:",
        confidence=0.9,
    )
    finding = scanner.classify_gap(concept)
    assert len(finding.candidate_modules) == 5


# --------------------------------------------------------------------------- #
# sha256 determinism
# --------------------------------------------------------------------------- #


def test_sha256_deterministic(loaded_scanner: ArchiveScanner, tmp_path: Path) -> None:
    body = "class Sha:\n    pass\n"
    (tmp_path / "a.py").write_text(body, encoding="utf-8")

    report_1 = loaded_scanner.scan(tmp_path)
    report_2 = loaded_scanner.scan(tmp_path)

    art_1 = {a.path: a.sha256 for a in report_1.artifacts}
    art_2 = {a.path: a.sha256 for a in report_2.artifacts}
    assert art_1 == art_2
    # And it's a real SHA-256 hex digest.
    assert re.fullmatch(r"[0-9a-f]{64}", art_1["a.py"])
