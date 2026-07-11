"""G6 audit-pipeline tagging tests (Codex chapter 04 / ADR-009).

Asserts that ``audit_url`` tags findings with the correct
``evidence_source`` at extraction time:
  * ``fetch_failed`` -> ``http_status`` (HTTP transport outcome)
  * ``blocked_by_robots`` -> ``robots_txt`` (robots.txt parse)
  * Content-derived findings (``missing_title`` etc.) -> ``None`` because
    they are not external-facing claims under G6 — they may not be
    rendered into a Gap Report.
  * ``AuditResult.evidence_sources`` is the derived id->source map.
"""
from __future__ import annotations

import httpx

# Reuse the deeper-suite fetch stub.
from tests.test_seo_deeper import (  # type: ignore[import-untyped]
    _BAD_HTML,
    _patch_audit_fetch,
)


def test_fetch_failed_tagged_http_status(monkeypatch) -> None:
    _patch_audit_fetch(
        monkeypatch, "", raise_exc=httpx.ConnectError("boom"),
    )
    from backend.seo.audit import audit_url

    result = audit_url("https://down.example.com")

    fetch_failed = next(i for i in result.issues if i.id == "fetch_failed")
    assert fetch_failed.evidence_source == "http_status"
    assert result.evidence_sources.get("fetch_failed") == "http_status"


def test_blocked_by_robots_tagged_robots_txt(monkeypatch) -> None:
    _patch_audit_fetch(monkeypatch, _BAD_HTML)

    import backend.seo.audit as audit_mod
    monkeypatch.setattr(
        audit_mod, "_check_robots_txt",
        lambda url: ("disallows", False),
    )
    # Pin the security audit + pagespeed so the test stays deterministic.
    monkeypatch.setattr(
        audit_mod, "audit_security",
        lambda url, headers, html, http_resources: {},
    )

    from backend.seo.audit import audit_url
    result = audit_url("https://blocked.example.com")

    blocked = next(i for i in result.issues if i.id == "blocked_by_robots")
    assert blocked.evidence_source == "robots_txt"
    assert result.evidence_sources.get("blocked_by_robots") == "robots_txt"


def test_content_findings_left_untagged(monkeypatch) -> None:
    _patch_audit_fetch(monkeypatch, _BAD_HTML)

    import backend.seo.audit as audit_mod
    monkeypatch.setattr(
        audit_mod, "audit_security",
        lambda url, headers, html, http_resources: {},
    )

    from backend.seo.audit import audit_url
    result = audit_url("https://bad.example.com")

    # Content-derived findings have no verified external source and must
    # be left untagged so the Gap Report filter drops them.
    for fid in ("missing_title", "missing_meta_description", "missing_h1"):
        finding = next((i for i in result.issues if i.id == fid), None)
        if finding is None:
            continue
        assert finding.evidence_source is None
        assert fid not in result.evidence_sources


def test_evidence_sources_map_only_contains_tagged_findings(monkeypatch) -> None:
    _patch_audit_fetch(monkeypatch, _BAD_HTML)

    import backend.seo.audit as audit_mod
    monkeypatch.setattr(
        audit_mod, "audit_security",
        lambda url, headers, html, http_resources: {},
    )

    from backend.seo.audit import audit_url
    result = audit_url("https://bad.example.com")

    # Every entry in the map points to a real issue id with that source.
    for fid, src in result.evidence_sources.items():
        match = next(i for i in result.issues if i.id == fid)
        assert match.evidence_source == src
