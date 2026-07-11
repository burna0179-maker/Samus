"""Tests for the G6 EvidenceSource enum + verified-source frozenset.

Covers (Codex chapter 04 / ADR-009):
  * Every documented enum value is in EVIDENCE_VERIFIED_SOURCES.
  * The frozenset rejects unverified / fabricated source names.
  * SeoIssue accepts every enum value as evidence_source and rejects
    any string that isn't in the Literal.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.seo.evidence_source import (
    EVIDENCE_VERIFIED_SOURCES,
    EvidenceSource,
)
from backend.seo.models import SeoIssue


_ALL_VALUES = (
    "crawled_header",
    "cert",
    "dns",
    "redirect",
    "public_registry",
    "robots_txt",
    "sitemap",
    "http_status",
)


def test_every_documented_value_is_verified() -> None:
    for value in _ALL_VALUES:
        assert value in EVIDENCE_VERIFIED_SOURCES


def test_verified_set_is_frozen() -> None:
    assert isinstance(EVIDENCE_VERIFIED_SOURCES, frozenset)
    with pytest.raises(AttributeError):
        EVIDENCE_VERIFIED_SOURCES.add("llm_inferred")  # type: ignore[attr-defined]


def test_unverified_sources_rejected_by_set() -> None:
    # LLM-inferred / hallucinated sources must not pass the membership
    # check — this is what stops auto-defamation at the render boundary.
    for bogus in ("llm_inferred", "guess", "claude_says", "", "HTTP_STATUS"):
        assert bogus not in EVIDENCE_VERIFIED_SOURCES


def test_seo_issue_accepts_every_enum_value() -> None:
    for value in _ALL_VALUES:
        issue = SeoIssue(
            id=f"test_{value}",
            severity="medium",
            category="technical",
            message="m",
            evidence_source=value,  # type: ignore[arg-type]
        )
        assert issue.evidence_source == value


def test_seo_issue_default_is_none() -> None:
    issue = SeoIssue(
        id="x",
        severity="low",
        category="content",
        message="m",
    )
    assert issue.evidence_source is None


def test_seo_issue_rejects_unknown_source() -> None:
    with pytest.raises(ValidationError):
        SeoIssue(
            id="x",
            severity="low",
            category="content",
            message="m",
            evidence_source="llm_inferred",  # type: ignore[arg-type]
        )


def test_evidence_source_type_is_literal_str() -> None:
    # EvidenceSource is a Literal of plain str values — keeps the worker
    # IPC JSON round-trip lossless without a custom encoder.
    assert hasattr(EvidenceSource, "__args__")
    for value in EvidenceSource.__args__:  # type: ignore[attr-defined]
        assert isinstance(value, str)
        assert value in EVIDENCE_VERIFIED_SOURCES
