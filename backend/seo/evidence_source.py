"""Evidence-source enum for Guardrail G6 (Codex chapter 04 / ADR-009).

Every external-facing claim in a Gap Report must carry an
``evidence_source`` so LLM-inferred vulnerability claims cannot be
serialized as auto-defamation. The enum values name only sources that
can be verified deterministically at extraction time — anything the LLM
inferred has no valid value here and is rejected at the serialization
boundary in :mod:`backend.seo.report`.

This is intentionally a ``Literal`` (a static set of strings, validated
by Pydantic at model-construction time) rather than an ``enum.Enum``:
``SeoIssue`` already round-trips through JSON for the worker IPC + the
audit-finding ledger, and a Literal stays a plain ``str`` on the wire
with no custom encoder.
"""
from __future__ import annotations

from typing import Literal


EvidenceSource = Literal[
    "crawled_header",
    "crawled_html",
    "crawled_meta",
    "cert",
    "dns",
    "redirect",
    "public_registry",
    "robots_txt",
    "sitemap",
    "http_status",
]


# Runtime membership set for the serialization filter in
# ``backend.seo.report.render_seo_report_markdown``. Anything NOT in this
# frozenset (including ``None``) fails the fail-closed check and is
# dropped from the customer-facing output.
#
# ``crawled_html`` covers deterministic HTML body parsing (mixed-content
# sub-resources, structured-data presence, image alt-text), and
# ``crawled_meta`` covers deterministic <head> parsing (title tag, meta
# description, canonical, hreflang). Both are NOT LLM-inferred — they
# are direct readings of the rendered DOM — so they belong in the
# verified set per G6's intent (rejecting LLM-inferred claims, not
# deterministic parser output).
EVIDENCE_VERIFIED_SOURCES: frozenset[str] = frozenset({
    "crawled_header",
    "crawled_html",
    "crawled_meta",
    "cert",
    "dns",
    "redirect",
    "public_registry",
    "robots_txt",
    "sitemap",
    "http_status",
})


__all__ = [
    "EvidenceSource",
    "EVIDENCE_VERIFIED_SOURCES",
]
