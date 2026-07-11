"""Customer-facing markdown report for the SEO workcell.

Renders the combined audit/optimize/content output as a single markdown
file ready to deliver to a paying customer of the $149 SEO Audit & Fix
service. ASCII only (no em-dashes) for Windows console / cp1252 safety.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse

import logging

from backend.common.codex import (
    CodexUnavailable,
    ProposedAction,
    check_action,
)
from backend.common.dates import iso_now

from .evidence_source import EVIDENCE_VERIFIED_SOURCES
from .models import AuditResult, ContentResult, OptimizeResult, SeoIssue

_LOG = logging.getLogger("samus.seo.report")


_SEVERITY_BADGE = {
    "critical": "[CRITICAL]",
    "high": "[HIGH]",
    "medium": "[MEDIUM]",
    "low": "[LOW]",
    "info": "[INFO]",
}

# Ordered worst-first so the security section lists the most urgent
# findings at the top.
_SECURITY_SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")

# Plain-English, alarm-calibrated blurb for each security grade in the
# client-facing report. Punchy; leads with what the grade means for them.
_SECURITY_GRADE_BLURB = {
    "A": (
        "Your site is in strong shape. The protections below are doing their "
        "job - the goal now is simply to keep them switched on."
    ),
    "B": (
        "Your site is in good shape, but a few gaps below are worth closing "
        "before someone finds them first."
    ),
    "C": (
        "Your site has real gaps. None of this is hopeless, but the problems "
        "below give an attacker openings you do not want to leave sitting "
        "there."
    ),
    "D": (
        "Your site is exposed. The problems below are the kind that get "
        "small businesses hacked - they need your attention soon, not "
        "someday."
    ),
    "F": (
        "Your site is wide open. Treat the problems below as urgent: each one "
        "is a door an attacker can walk straight through."
    ),
}

# Plain severity words for the client-facing section - no badges, no jargon.
_SECURITY_SEVERITY_WORD = {
    "critical": "Urgent",
    "high": "Serious",
    "medium": "Worth fixing",
    "low": "Minor",
    "info": "For your information",
}

_CATEGORY_TITLE = {
    "technical": "Technical",
    "content": "Content",
    "local": "Local",
    "mobile": "Mobile",
    "reviews": "Reviews",
}

_DRAFT_FIELD_ORDER = ("title", "meta_description", "h1", "body_intro", "body_main", "cta")

_DRAFT_FIELD_TITLE = {
    "title": "Title Tag",
    "meta_description": "Meta Description",
    "h1": "H1 Heading",
    "body_intro": "Body Intro",
    "body_main": "Body Main",
    "cta": "Call to Action",
}


# ---------------------------------------------------------------------------
# Slug + path helpers
# ---------------------------------------------------------------------------


def customer_slug_from_url(url: str) -> str:
    """Derive a filesystem-safe slug from the URL hostname.

    ``https://www.acme-plumbing.example.com/`` -> ``acme-plumbing_example_com``
    """
    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = (parsed.hostname or url or "site").lower()
    if host.startswith("www."):
        host = host[4:]
    slug = host.replace(".", "_")
    # Strip anything that isn't alnum / dash / underscore.
    slug = re.sub(r"[^a-z0-9_\-]", "", slug)
    return slug or "site"


def _artifact_root() -> Path:
    """Read root at call time so SAMUS_ARTIFACT_ROOT env override works."""
    root = Path(os.getenv("SAMUS_ARTIFACT_ROOT", r"E:\Hustleforge\Samus\data\artifacts"))
    root.mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# Pure markdown rendering
# ---------------------------------------------------------------------------


def _render_cover(audit: AuditResult, customer_label: str | None) -> list[str]:
    label = customer_label or audit.url
    lines = [
        f"# SEO Audit & Fix Report -- {label}",
        "",
        f"- **Site:** {audit.url}",
        f"- **Generated:** {audit.ts}",
        f"- **SEO Score:** {audit.seo_score} / 100",
    ]
    if audit.findings:
        industry = audit.findings.get("industry") or ""
        keywords = audit.findings.get("keywords") or []
        if industry:
            lines.append(f"- **Industry:** {industry}")
        if keywords:
            lines.append(f"- **Target Keywords:** {', '.join(keywords)}")
    lines.append("")
    return lines


def _summarize(audit: AuditResult, optimize: OptimizeResult) -> str:
    issues = audit.issues
    sev_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for i in issues:
        sev_counts[i.severity] = sev_counts.get(i.severity, 0) + 1

    parts: list[str] = []
    parts.append(f"This report covers {audit.url} and was scored {audit.seo_score} out of 100.")
    if not issues:
        parts.append("No SEO issues were detected on this page.")
    else:
        sev_summary = ", ".join(f"{n} {s}" for s, n in sev_counts.items() if n > 0)
        parts.append(f"We identified {len(issues)} issues ({sev_summary}).")

    top_issue_msgs = [i.message for i in issues if i.severity in ("critical", "high")][:2]
    if top_issue_msgs:
        parts.append("Highest-impact findings: " + "; ".join(top_issue_msgs))

    if optimize.on_page_changes:
        parts.append(
            f"We prepared {len(optimize.on_page_changes)} ready-to-paste on-page "
            f"changes and {len(optimize.recommendations)} prioritized recommendations."
        )
    else:
        parts.append(f"We prepared {len(optimize.recommendations)} prioritized recommendations.")
    return " ".join(parts)


def _render_executive_summary(audit: AuditResult, optimize: OptimizeResult) -> list[str]:
    return [
        "## Executive Summary",
        "",
        _summarize(audit, optimize),
        "",
    ]


def _filter_verified_issues(
    issues: list[SeoIssue],
    url: str,
) -> tuple[list[SeoIssue], int]:
    """G6 fail-closed serialization filter (Codex chapter 04 / ADR-009).

    Drops every issue whose ``evidence_source`` is not in
    :data:`EVIDENCE_VERIFIED_SOURCES`. Returns ``(kept, dropped_count)``
    and logs each drop with its id + reason so the suppressed claim is
    auditable. This is the boundary that stops an LLM-inferred Gap
    Report claim from being rendered into a customer-facing document.
    """
    kept: list[SeoIssue] = []
    dropped = 0
    for issue in issues:
        src = issue.evidence_source
        if src in EVIDENCE_VERIFIED_SOURCES:
            kept.append(issue)
        else:
            dropped += 1
            _LOG.info(
                "G6 dropped Gap Report finding id=%s reason=%s url=%s",
                issue.id,
                "missing_evidence_source" if src is None else f"unverified_source:{src}",
                url,
            )
    return kept, dropped


def _group_by_category(issues: list[SeoIssue]) -> dict[str, list[SeoIssue]]:
    grouped: dict[str, list[SeoIssue]] = {}
    for i in issues:
        grouped.setdefault(i.category, []).append(i)
    return grouped


def _render_audit_findings(audit: AuditResult) -> list[str]:
    lines = ["## Audit Findings", ""]
    if not audit.issues:
        lines.append("No SEO issues were detected. The page passed every check.")
        lines.append("")
        return lines

    grouped = _group_by_category(audit.issues)
    for category in sorted(grouped.keys()):
        title = _CATEGORY_TITLE.get(category, category.title())
        lines.append(f"### {title}")
        lines.append("")
        for issue in grouped[category]:
            badge = _SEVERITY_BADGE.get(issue.severity, f"[{issue.severity.upper()}]")
            lines.append(f"- [ ] {badge} `{issue.id}` -- {issue.message}")
            if issue.evidence:
                lines.append(f"      Evidence: {issue.evidence}")
        lines.append("")
    return lines


def _render_recommendations(optimize: OptimizeResult) -> list[str]:
    lines = ["## Recommendations", ""]
    recs = sorted(optimize.recommendations, key=lambda r: -r.priority)
    if not recs:
        lines.append("No recommendations were produced.")
        lines.append("")
        return lines

    for idx, rec in enumerate(recs):
        marker = "★ RECOMMENDED " if idx == 0 else ""
        lines.append(f"{idx + 1}. {marker}**[priority {rec.priority}] [{rec.area}]** {rec.action}")
        lines.append(f"   - Why: {rec.rationale}")
    lines.append("")

    if optimize.on_page_changes:
        lines.append("### Ready-to-paste On-Page Changes")
        lines.append("")
        for field, value in optimize.on_page_changes.items():
            lines.append(f"- **{field}**: `{value}`")
        lines.append("")
    return lines


def _render_content_drafts(content: ContentResult) -> list[str]:
    drafts = content.page_drafts or {}
    if not drafts:
        return []
    lines = [
        "## Content Drafts",
        "",
        "The following drafts are ready to paste into your site. "
        f"({content.word_count} words total"
        + (", generated via LLM" if content.used_llm else ", templated")
        + ")",
        "",
    ]
    for field in _DRAFT_FIELD_ORDER:
        if field not in drafts:
            continue
        heading = _DRAFT_FIELD_TITLE.get(field, field)
        lines.append(f"### {heading}")
        lines.append("")
        lines.append("```")
        lines.append(drafts[field])
        lines.append("```")
        lines.append("")
    # Include any non-standard fields the LLM might return so nothing is dropped.
    extras = [k for k in drafts.keys() if k not in _DRAFT_FIELD_ORDER]
    for field in extras:
        lines.append(f"### {field}")
        lines.append("")
        lines.append("```")
        lines.append(drafts[field])
        lines.append("```")
        lines.append("")
    return lines


def _security_findings(audit: AuditResult) -> tuple[str, list[dict]] | None:
    """Pull (grade, findings) out of ``audit.findings["security"]``.

    Returns ``None`` when no security audit ran (toggle off, or audit failed)
    so callers can omit their section entirely.
    """
    security = audit.findings.get("security") if audit.findings else None
    if not isinstance(security, dict) or not security:
        return None
    grade = str(security.get("grade", "")) or "F"
    raw_findings = security.get("findings") or []
    findings = [f for f in raw_findings if isinstance(f, dict)]
    return grade, findings


def _render_security_posture(audit: AuditResult) -> list[str]:
    """Render the CLIENT-FACING "Security & Trust Posture" section.

    Reads ``audit.findings["security"]`` (the dict produced by
    ``backend.seo.security_audit.audit_security``) and renders it for a
    small-business owner: each problem is a punchy alarm headline plus a
    couple of plain "what this means for your business" sentences and a plain
    severity word - NO header names, NO acronyms, NO finding ids, NO evidence
    strings, NO fix instructions. Passing checks get a brief, calm
    "Already in good shape" list. The build-sheet detail (titles, evidence,
    remediation, ids) lives in the separate ``security_audit_technical.md``,
    rendered by :func:`render_security_technical`.

    Returns an empty list when no security audit ran. ASCII only - no
    em-dashes - for cp1252 console safety.
    """
    parsed = _security_findings(audit)
    if parsed is None:
        return []
    grade, findings = parsed

    lines = ["## Security & Trust Posture", ""]
    lines.append(f"**Security Grade: {grade}**")
    blurb = _SECURITY_GRADE_BLURB.get(grade, "")
    if blurb:
        lines.append("")
        lines.append(blurb)
    lines.append("")
    lines.append(
        "We took a careful look at how safe and trustworthy your website "
        "looks to visitors, to search engines, and to anyone with bad "
        "intentions. We only looked - nothing on your site was touched or "
        "changed."
    )
    lines.append("")

    if not findings:
        lines.append("No security findings were produced.")
        lines.append("")
        return lines

    # Split passing/info findings from real problems. Group problems
    # worst-first by severity.
    problems_by_severity: dict[str, list[dict]] = {}
    passing: list[dict] = []
    for finding in findings:
        sev = str(finding.get("severity", "info"))
        if sev == "info":
            passing.append(finding)
        else:
            problems_by_severity.setdefault(sev, []).append(finding)

    problem_total = sum(len(b) for b in problems_by_severity.values())

    if problem_total:
        lines.append(
            f"We found {problem_total} "
            + ("thing" if problem_total == 1 else "things")
            + " that need your attention. They are listed below, most "
            "important first."
        )
        lines.append("")
        for severity in _SECURITY_SEVERITY_ORDER:
            bucket = problems_by_severity.get(severity)
            if not bucket:
                continue
            for finding in bucket:
                headline = (
                    str(finding.get("client_headline", "")).strip()
                    or str(finding.get("title", "")).strip()
                )
                impact = str(finding.get("client_impact", "")).strip()
                word = _SECURITY_SEVERITY_WORD.get(severity, severity.capitalize())
                lines.append(f"### {headline}")
                lines.append("")
                lines.append(f"**How serious: {word}.**")
                lines.append("")
                if impact:
                    lines.append(impact)
                    lines.append("")
    else:
        lines.append(
            "Good news: we did not find anything on your site that needs fixing right now."
        )
        lines.append("")

    if passing:
        lines.append("### Already in good shape")
        lines.append("")
        lines.append("These checks came back clean - no action needed, just keep them as they are:")
        lines.append("")
        for finding in passing:
            headline = (
                str(finding.get("client_headline", "")).strip()
                or str(finding.get("title", "")).strip()
            )
            impact = str(finding.get("client_impact", "")).strip()
            if impact:
                lines.append(f"- **{headline}.** {impact}")
            else:
                lines.append(f"- **{headline}.**")
        lines.append("")

    return lines


def render_security_technical(audit: AuditResult) -> str:
    """Render the TECHNICAL security report (``security_audit_technical.md``).

    This is the build-sheet for whoever implements the fixes: it keeps ALL
    technical detail the client-facing section deliberately drops - exact
    titles (header names), evidence strings, attacker-impact risk, step-by-
    step remediation, finding ids, the A-F grade, and the per-category
    breakdown. The jargon-heavy format is intentional and correct here.

    Returns an empty string when no security audit ran. ASCII only - no
    em-dashes - for cp1252 console safety.
    """
    parsed = _security_findings(audit)
    if parsed is None:
        return ""
    grade, findings = parsed

    label = audit.url
    lines: list[str] = [
        f"# Security Audit -- Technical Report -- {label}",
        "",
        f"- **Site:** {audit.url}",
        f"- **Generated:** {audit.ts}",
        f"- **Security Grade:** {grade}",
        "",
        "This is the technical companion to the customer-facing SEO report's "
        '"Security & Trust Posture" section. It is written for whoever '
        "implements the fixes and keeps every detail - exact header names, "
        "raw evidence, attacker-impact risk, remediation steps, and finding "
        "ids. It is a passive review: only ordinary HTTP GET requests, one "
        "TLS handshake, and DNS lookups were used; nothing was attacked or "
        "altered.",
        "",
    ]

    if not findings:
        lines.append("No security findings were produced.")
        lines.append("")
        return "\n".join(lines)

    # Group worst-first by severity for the finding list.
    by_severity: dict[str, list[dict]] = {}
    for finding in findings:
        sev = str(finding.get("severity", "info"))
        by_severity.setdefault(sev, []).append(finding)

    counts = ", ".join(
        f"{len(by_severity[s])} {s}" for s in _SECURITY_SEVERITY_ORDER if by_severity.get(s)
    )
    if counts:
        lines.append("## Summary")
        lines.append("")
        lines.append(f"{len(findings)} findings recorded ({counts}).")
        lines.append("")

    # Per-category breakdown.
    by_category: dict[str, list[dict]] = {}
    for finding in findings:
        cat = str(finding.get("category", "other"))
        by_category.setdefault(cat, []).append(finding)
    lines.append("## Findings by category")
    lines.append("")
    for category in sorted(by_category.keys()):
        bucket = by_category[category]
        cat_counts = ", ".join(
            f"{sum(1 for f in bucket if f.get('severity') == s)} {s}"
            for s in _SECURITY_SEVERITY_ORDER
            if any(f.get("severity") == s for f in bucket)
        )
        lines.append(f"- **{category}**: {len(bucket)} ({cat_counts})")
    lines.append("")

    # Full finding detail, worst-first.
    lines.append("## Findings")
    lines.append("")
    for severity in _SECURITY_SEVERITY_ORDER:
        bucket = by_severity.get(severity)
        if not bucket:
            continue
        badge = _SEVERITY_BADGE.get(severity, f"[{severity.upper()}]")
        lines.append(f"### {badge} {severity.capitalize()}")
        lines.append("")
        for finding in bucket:
            fid = str(finding.get("id", ""))
            title = str(finding.get("title", "")) or fid
            category = str(finding.get("category", ""))
            lines.append(f"- [ ] **{title}** `{fid}`")
            if category:
                lines.append(f"      Category: {category}")
            evidence = str(finding.get("evidence", "")).strip()
            if evidence:
                lines.append(f"      Evidence: {evidence}")
            risk = str(finding.get("risk", "")).strip()
            if risk:
                lines.append(f"      Risk: {risk}")
            remediation = str(finding.get("remediation", "")).strip()
            if remediation:
                lines.append(f"      How to fix: {remediation}")
        lines.append("")

    lines.append("---")
    lines.append(f"_Generated by Samus finance/seo workcell, {iso_now()}_")
    lines.append("")
    return "\n".join(lines)


def _render_stake_header(stake_sentence: str | None) -> list[str]:
    text = (stake_sentence or "").strip()
    if not text:
        return []
    return [
        f"> *{text}*",
        "",
        "---",
        "",
    ]


def render_seo_report_markdown(
    audit: AuditResult,
    optimize: OptimizeResult,
    content: ContentResult | None,
    *,
    customer_label: str | None = None,
    stake_sentence: str | None = None,
) -> str:
    """Pure function: build the customer-facing markdown body.

    When ``stake_sentence`` is provided it renders as the FIRST block of the
    document — a single italicised line, blank line, horizontal rule, blank
    line — before the cover. Gap Reports for unattributed audits (no linked
    Opportunity) pass ``None`` and the stake block is omitted entirely.
    """
    # G6 fail-closed serialization filter (Codex chapter 04 / ADR-009).
    # Strip every issue whose source isn't deterministically verified
    # BEFORE we hand the audit to either the Codex check or the
    # downstream renderers — the validator sees only the post-filter
    # evidence_sources list, and the markdown never references a
    # filtered finding.
    verified_issues, dropped_count = _filter_verified_issues(
        audit.issues,
        audit.url,
    )
    if dropped_count:
        _LOG.info(
            "G6 Gap Report serialization filter dropped %d/%d findings (url=%s)",
            dropped_count,
            len(audit.issues),
            audit.url,
        )
    # Shallow copy with filtered issues. model_copy keeps the original
    # ``audit`` immutable from the caller's perspective.
    filtered_audit = audit.model_copy(update={"issues": verified_issues})
    # Post-filter evidence_sources list (what actually reaches the
    # rendered output). The Codex validator sees this list and — once
    # the parent flips VW-G6 to VR-G6 — an empty list will block.
    post_filter_sources = [
        i.evidence_source for i in verified_issues if i.evidence_source is not None
    ]

    # Codex Validation Layer (chapter 12 / ADR-011): Gap Report renders are
    # Codex-checked. G6 (evidence-source enum) was flipped advisory->BLOCKING by
    # ADR-012 (2026-05-30) once the post-filter EvidenceSource set shipped, so a
    # render that omits ``evidence_sources`` is now refused. This call stays
    # compliant by always declaring the post-filter source list below.
    # CodexUnavailable raises through; render refuses if the layer isn't
    # loaded (fail-closed).
    try:
        _verdict = check_action(
            ProposedAction(
                service="seo",
                capability="audit_and_report",
                action_kind="gap_report_render",
                payload={
                    "customer_label": customer_label,
                    "stake_sentence": stake_sentence,
                    "evidence_sources": post_filter_sources,
                    "url": getattr(audit, "url", None),
                },
                proposed_by="seo.render_seo_report_markdown",
                correlation_id=None,
            )
        )
    except CodexUnavailable:
        raise
    if not _verdict.allowed:
        raise RuntimeError(
            f"render_seo_report_markdown refused by Codex rule "
            f"{_verdict.violated_rule_id}: {_verdict.reason}"
        )
    for _warning in _verdict.warnings:
        _LOG.info("render_seo_report_markdown codex advisory: %s", _warning)

    parts: list[str] = []
    parts.extend(_render_stake_header(stake_sentence))
    parts.extend(_render_cover(filtered_audit, customer_label))
    parts.extend(_render_executive_summary(filtered_audit, optimize))
    parts.extend(_render_audit_findings(filtered_audit))
    parts.extend(_render_recommendations(optimize))
    # Security & Trust Posture sits after Recommendations, before Content
    # Drafts. Omitted entirely when no security audit ran.
    parts.extend(_render_security_posture(filtered_audit))
    if content is not None:
        parts.extend(_render_content_drafts(content))
    parts.append("---")
    parts.append(f"_Generated by Samus finance/seo workcell, {iso_now()}_")
    parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Filesystem writer
# ---------------------------------------------------------------------------


def write_seo_report(
    audit: AuditResult,
    optimize: OptimizeResult,
    content: ContentResult | None,
    *,
    customer_label: str | None = None,
    stake_sentence: str | None = None,
) -> Path:
    """Render the report and write it under ``<artifacts>/customers/<slug>/``.

    Writes the customer-facing ``seo_report.md``. When a passive security
    audit ran (``audit.findings["security"]`` is present), ALSO writes
    ``security_audit_technical.md`` into the same directory - the build-sheet
    that keeps the full technical detail the client report deliberately drops.
    Returns the path to ``seo_report.md``.
    """
    if customer_label is not None and customer_label.strip():
        slug = (
            customer_slug_from_url(customer_label)
            if "://" in customer_label
            else _slugify_label(customer_label)
        )
    else:
        slug = customer_slug_from_url(audit.url)

    body = render_seo_report_markdown(
        audit,
        optimize,
        content,
        customer_label=customer_label,
        stake_sentence=stake_sentence,
    )
    target_dir = _artifact_root() / "customers" / slug
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "seo_report.md"
    target.write_text(body, encoding="utf-8")

    # Companion technical security report - only when a security audit ran.
    technical = render_security_technical(audit)
    if technical:
        (target_dir / "security_audit_technical.md").write_text(
            technical,
            encoding="utf-8",
        )
    return target


def _slugify_label(label: str) -> str:
    """Slugify an arbitrary operator-supplied label (no URL parsing)."""
    s = label.strip().lower().replace(" ", "_").replace(".", "_")
    s = re.sub(r"[^a-z0-9_\-]", "", s)
    # Collapse runs of underscores and strip leading/trailing.
    s = re.sub(r"_+", "_", s).strip("_-")
    return s or "customer"
