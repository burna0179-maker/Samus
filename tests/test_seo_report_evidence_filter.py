"""G6 serialization filter tests (Codex chapter 04 / ADR-009).

Asserts that ``render_seo_report_markdown``:
  * Renders findings whose ``evidence_source`` is in the verified set.
  * Drops findings with ``evidence_source=None`` or an out-of-set value.
  * Logs each drop with the finding id + reason.
  * Leaves the input ``AuditResult`` unmutated (model_copy semantics).
"""
from __future__ import annotations

import logging

from backend.seo.models import (
    AuditResult,
    OptimizationRecommendation,
    OptimizeResult,
    SeoIssue,
)
from backend.seo.report import render_seo_report_markdown


def _mixed_audit() -> AuditResult:
    # Two tagged-verified, one tagged-but-unverified (None), and one
    # whose tag would never be accepted by the Literal so we represent
    # the "unverified" case as None — the type system forbids the other.
    issues = [
        SeoIssue(
            id="fetch_failed", severity="critical", category="technical",
            message="Could not fetch page.", evidence="",
            evidence_source="http_status",
        ),
        SeoIssue(
            id="blocked_by_robots", severity="critical", category="technical",
            message="robots.txt blocks crawlers.",
            evidence="https://acme.example.com/robots.txt",
            evidence_source="robots_txt",
        ),
        SeoIssue(
            id="missing_title", severity="high", category="content",
            message="Page is missing a <title> tag.", evidence="",
            evidence_source=None,
        ),
        SeoIssue(
            id="missing_h1", severity="high", category="content",
            message="Page has no <h1>.", evidence="",
            evidence_source=None,
        ),
    ]
    return AuditResult(
        url="https://acme.example.com",
        seo_score=42,
        issues=issues,
        findings={"industry": "plumbing", "keywords": []},
        evidence_sources={
            "fetch_failed": "http_status",
            "blocked_by_robots": "robots_txt",
        },
        ts="2026-05-30T00:00:00Z",
    )


def _empty_optimize() -> OptimizeResult:
    return OptimizeResult(
        url="https://acme.example.com",
        recommendations=[
            OptimizationRecommendation(
                area="content", action="x", rationale="y", priority=3,
            ),
        ],
        on_page_changes={},
        ts="2026-05-30T00:00:00Z",
    )


def test_unverified_findings_dropped_from_rendered_output(caplog) -> None:
    audit = _mixed_audit()
    optimize = _empty_optimize()
    caplog.set_level(logging.INFO, logger="samus.seo.report")

    body = render_seo_report_markdown(audit, optimize, None)

    # Verified findings survive into the rendered markdown.
    assert "fetch_failed" in body
    assert "blocked_by_robots" in body
    # Unverified findings (evidence_source=None) are filtered out.
    assert "missing_title" not in body
    assert "missing_h1" not in body


def test_drop_events_logged_with_id_and_reason(caplog) -> None:
    audit = _mixed_audit()
    optimize = _empty_optimize()
    caplog.set_level(logging.INFO, logger="samus.seo.report")

    render_seo_report_markdown(audit, optimize, None)

    drop_messages = [
        rec.getMessage() for rec in caplog.records
        if "G6 dropped" in rec.getMessage()
    ]
    # Two unverified findings, two drop events.
    assert len(drop_messages) == 2
    ids_logged = " ".join(drop_messages)
    assert "missing_title" in ids_logged
    assert "missing_h1" in ids_logged
    assert "missing_evidence_source" in ids_logged


def test_all_verified_findings_renders_fully() -> None:
    audit = AuditResult(
        url="https://acme.example.com",
        seo_score=60,
        issues=[
            SeoIssue(
                id="fetch_failed", severity="critical", category="technical",
                message="Could not fetch page.", evidence="",
                evidence_source="http_status",
            ),
        ],
        findings={},
        evidence_sources={"fetch_failed": "http_status"},
        ts="2026-05-30T00:00:00Z",
    )
    body = render_seo_report_markdown(audit, _empty_optimize(), None)
    assert "fetch_failed" in body


def test_all_unverified_findings_renders_no_findings_section_body() -> None:
    # When every finding is unverified the renderer still produces a
    # document (cover + summary + recommendations) but the Audit
    # Findings section reports zero issues — the fail-closed contract.
    audit = AuditResult(
        url="https://acme.example.com",
        seo_score=80,
        issues=[
            SeoIssue(
                id="missing_title", severity="high", category="content",
                message="m", evidence_source=None,
            ),
        ],
        findings={},
        evidence_sources={},
        ts="2026-05-30T00:00:00Z",
    )
    body = render_seo_report_markdown(audit, _empty_optimize(), None)
    assert "missing_title" not in body
    assert "## Audit Findings" in body


def test_render_does_not_mutate_input_audit() -> None:
    audit = _mixed_audit()
    original_ids = [i.id for i in audit.issues]
    render_seo_report_markdown(audit, _empty_optimize(), None)
    assert [i.id for i in audit.issues] == original_ids
    assert len(audit.issues) == 4
