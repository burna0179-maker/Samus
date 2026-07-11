"""Gap Report stake_sentence header renders when provided, omitted when not."""

from __future__ import annotations

from backend.seo.models import AuditResult, OptimizeResult
from backend.seo.report import render_seo_report_markdown


_VALID = (
    "Alex picked you because your Yuba City HVAC ranks for fewer keywords "
    "than two of your neighbors combined."
)


def _audit() -> AuditResult:
    return AuditResult(
        url="https://acme.test/",
        ts="2026-05-29T12:00:00Z",
        seo_score=72,
        issues=[],
        findings={},
    )


def _optimize() -> OptimizeResult:
    return OptimizeResult(
        url="https://acme.test/",
        ts="2026-05-29T12:00:00Z",
        on_page_changes={},
        recommendations=[],
    )


def test_report_with_stake_renders_header_block():
    body = render_seo_report_markdown(
        _audit(),
        _optimize(),
        None,
        customer_label="Acme",
        stake_sentence=_VALID,
    )
    lines = body.splitlines()
    assert lines[0] == f"> *{_VALID}*"
    # Followed by blank, "---", blank.
    assert lines[1] == ""
    assert lines[2] == "---"
    assert lines[3] == ""
    # Then the existing cover heading.
    assert any(line.startswith("# SEO Audit & Fix Report") for line in lines)


def test_report_without_stake_omits_block():
    body = render_seo_report_markdown(
        _audit(),
        _optimize(),
        None,
        customer_label="Acme",
    )
    assert not body.startswith("> ")
    assert body.lstrip().startswith("# SEO Audit & Fix Report")


def test_report_empty_stake_omits_block():
    body = render_seo_report_markdown(
        _audit(),
        _optimize(),
        None,
        customer_label="Acme",
        stake_sentence="   ",
    )
    assert not body.startswith("> ")
