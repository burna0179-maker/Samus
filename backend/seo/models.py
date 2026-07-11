"""Pydantic models for the SEO workcell."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .evidence_source import EvidenceSource


class SeoIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    severity: Literal["critical", "high", "medium", "low"]
    category: Literal["technical", "content", "local", "mobile", "reviews"]
    message: str
    evidence: str = ""
    # G6 (Codex chapter 04 / ADR-009). Set ONLY when the finding was
    # extracted from a deterministically verifiable source (HTTP header,
    # TLS cert, DNS, redirect, robots.txt, sitemap, HTTP status). Findings
    # whose source is LLM inference, optimization heuristics, or content
    # rules leave this ``None`` and are dropped from the customer-facing
    # Gap Report by the serialization filter in ``backend.seo.report``.
    evidence_source: EvidenceSource | None = None


class SecurityFinding(BaseModel):
    """A single passive security/trust-posture observation about a site.

    Distinct from :class:`SeoIssue`: a security finding carries a one-sentence
    attacker-impact ``risk`` and a concrete, client-actionable ``remediation``
    string that ``SeoIssue`` has no shape for. ``severity`` adds the ``info``
    tier (a recommendation, not a defect) that the SEO checklist never emits.

    A finding speaks to TWO audiences. The technical fields (``title``,
    ``evidence``, ``risk``, ``remediation``, ``id``) feed
    ``security_audit_technical.md`` - the build-sheet for whoever implements
    the fix. The client fields (``client_headline``, ``client_impact``) feed
    the "Security & Trust Posture" section of the customer-facing
    ``seo_report.md`` - punchy, plain-language, jargon-free copy a
    small-business owner can read and act on.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    severity: Literal["critical", "high", "medium", "low", "info"]
    category: Literal["headers", "tls", "email_auth", "platform", "exposure", "cookies"]
    title: str
    evidence: str = ""
    risk: str = ""
    remediation: str = ""
    # Client-facing copy (customer report). ``client_headline`` is a punchy
    # 6-10 word alarm hook with no trailing period; ``client_impact`` is 2-3
    # plain, vivid, jargon-free sentences on the business consequence.
    client_headline: str = ""
    client_impact: str = ""


class AuditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # ``url`` is attacker-influenceable (operator / prospect / intake supplied)
    # and is fetched server-side by backend.seo.audit — an SSRF sink (finding
    # H3). Reject anything that is not an http(s) URL at the validation
    # boundary so a ``file://`` / ``gopher://`` / scheme-less value never
    # reaches the fetch path. The resolved-IP egress allowlist (private /
    # loopback / metadata ranges) is enforced in backend.common.safe_fetch at
    # fetch time, since DNS must be resolved fresh against rebind.
    url: str
    keywords: list[str] = Field(default_factory=list)
    industry: str = ""
    # Phase 5 — optional CRM linkage. When supplied, the audit-completion
    # path fires a best-effort artifact registration to samus-crm so the
    # operator can pull "all SEO deliverables for prospect X" later. The
    # audit pipeline runs identically whether or not this is set.
    prospect_id: str = ""

    @field_validator("url")
    @classmethod
    def _url_must_be_http(cls, value: str) -> str:
        """Reject non-http(s) schemes before the URL reaches the fetch path."""
        candidate = (value or "").strip()
        scheme = candidate.split("://", 1)[0].lower() if "://" in candidate else ""
        if scheme not in ("http", "https"):
            raise ValueError("url must be an http:// or https:// URL")
        return candidate


class AuditResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    seo_score: int = Field(ge=0, le=100)
    issues: list[SeoIssue] = Field(default_factory=list)
    findings: dict[str, Any] = Field(default_factory=dict)
    ts: str
    # G6 source map: finding-id -> EvidenceSource. Populated by the audit
    # emitter when the source is known deterministically; consumed by the
    # Gap Report serializer + Codex validator (chapter 12 / ADR-009).
    # Findings absent from this map carry no verified provenance and are
    # filtered out of the customer-facing Gap Report.
    evidence_sources: dict[str, EvidenceSource] = Field(default_factory=dict)


class OptimizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    audit_data: AuditResult
    target_keywords: list[str] = Field(default_factory=list)


class OptimizationRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    area: str
    action: str
    rationale: str
    priority: int = Field(ge=1, le=5)


class OptimizeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    recommendations: list[OptimizationRecommendation] = Field(default_factory=list)
    on_page_changes: dict[str, str] = Field(default_factory=dict)
    ts: str


class ContentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    optimization_data: OptimizeResult
    target_keywords: list[str] = Field(default_factory=list)
    tone: str = "professional"


class ContentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    page_drafts: dict[str, str] = Field(default_factory=dict)
    word_count: int = 0
    used_llm: bool = False
    # Real USD cost of the content-draft LLM call, priced from the Anthropic
    # response usage block (strategy-integration build, Unit 4). 0.0 on the
    # templated path. Surfaced through audit_and_report so process_discovery
    # can fold it into the prospect's per-prospect LLM spend.
    llm_cost_usd: float = 0.0
    ts: str
