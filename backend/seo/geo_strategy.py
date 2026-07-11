"""GEO (Generative Engine Optimization) audit and citation strategy engine.

Analyses a customer site and produces a GEO readiness score (0-100) plus
specific, evidence-sourced findings in five areas:

  1. robots_txt_audit    -- AI search crawler access (OAI-SearchBot, PerplexityBot, etc.)
  2. schema_gap_audit    -- missing Tier 1 schemas (FAQPage, HowTo, Article+dateModified)
  3. content_freshness   -- pages with no visible Last Updated date / dateModified schema
  4. topical_authority   -- 10-article cluster plan matched to business + keywords
  5. geo_score           -- 0-100 composite GEO readiness score

Every finding carries an evidence_source. Integrates with existing
AuditResult from backend.seo.audit (schema_types, schema facts, robots facts).

No LLM calls. No new dependencies (httpx, BeautifulSoup already present).
ASCII-only output.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import httpx

from backend.common import safe_fetch
from backend.common.dates import iso_now
from backend.seo.models import AuditResult

_LOG = logging.getLogger("samus.seo.geo_strategy")

_HTTP_TIMEOUT = 10.0

# ---------------------------------------------------------------------------
# AI search crawler user-agent strings
# ---------------------------------------------------------------------------

AI_CRAWLERS: dict[str, str] = {
    "OAI-SearchBot": "OpenAI search indexer",
    "ChatGPT-User": "ChatGPT user-browsing agent",
    "PerplexityBot": "Perplexity AI indexer",
    "Claude-SearchBot": "Anthropic Claude search indexer",
    "Claude-User": "Anthropic Claude user-browsing agent",
}

# Tier 1 schemas that most directly raise AI citation eligibility.
TIER1_SCHEMAS: tuple[str, ...] = ("FAQPage", "HowTo", "Article", "BlogPosting")

# Schema types that imply dateModified is relevant (Article, BlogPosting)
ARTICLE_SCHEMA_TYPES: frozenset[str] = frozenset({"Article", "BlogPosting", "NewsArticle"})

# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


@dataclass
class GeoFinding:
    """A single GEO audit finding with evidence provenance."""

    id: str
    severity: str  # "critical" | "high" | "medium" | "low" | "pass"
    category: str  # "robots_txt" | "schema" | "freshness" | "topical" | "score"
    message: str
    recommendation: str  # paste-ready or specific action
    evidence: str = ""
    evidence_source: str = ""  # matches EvidenceSource literals where applicable


@dataclass
class TopicalClusterPlan:
    """A 10-article cluster plan for topical authority building."""

    cluster_name: str
    primary_keyword: str
    articles: list[str] = field(default_factory=list)  # 10 question-formatted titles
    publish_cadence: str = "2 articles per month (5 months to complete cluster)"


@dataclass
class GeoAuditResult:
    """Full GEO audit result for a customer site."""

    url: str
    geo_score: int  # 0-100
    findings: list[GeoFinding]  # all findings
    robots_findings: list[GeoFinding]  # subset: robots_txt category
    schema_findings: list[GeoFinding]  # subset: schema category
    freshness_findings: list[GeoFinding]  # subset: freshness category
    topical_plan: TopicalClusterPlan | None
    robots_txt_snippet: str  # paste-ready robots.txt block
    ts: str = ""


# ---------------------------------------------------------------------------
# robots.txt audit
# ---------------------------------------------------------------------------


def _fetch_robots_txt(base_url: str) -> tuple[str, str]:
    """Fetch robots.txt for the site. Returns (text, evidence_source_tag)."""
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        return "", "robots_txt"
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        r = safe_fetch.safe_get(
            robots_url,
            timeout=_HTTP_TIMEOUT,
            headers=dict(safe_fetch.BROWSER_HEADERS),
        )
        if r.status_code == 200:
            return r.text or "", "robots_txt"
        return "", "robots_txt"
    except (httpx.HTTPError, safe_fetch.SsrfBlockedError, Exception) as exc:  # noqa: BLE001
        _LOG.debug("robots fetch failed url=%s: %s", robots_url, exc)
        return "", "robots_txt"


def robots_txt_audit(base_url: str) -> list[GeoFinding]:
    """Check whether AI search crawlers are blocked in robots.txt.

    Returns a list of GeoFinding records, one per blocked crawler plus
    a summary pass finding if all are accessible.
    """
    import urllib.robotparser

    robots_text, ev_src = _fetch_robots_txt(base_url)

    findings: list[GeoFinding] = []

    if not robots_text:
        findings.append(
            GeoFinding(
                id="geo_robots_fetch_failed",
                severity="medium",
                category="robots_txt",
                message="Could not fetch robots.txt; unable to verify AI crawler access.",
                recommendation=(
                    "Ensure robots.txt is accessible at the site root. "
                    "Add explicit User-agent allow rules for AI crawlers."
                ),
                evidence="robots.txt returned no content",
                evidence_source=ev_src,
            )
        )
        return findings

    rp = urllib.robotparser.RobotFileParser()
    rp.parse(robots_text.splitlines())

    blocked: list[str] = []
    for bot, description in AI_CRAWLERS.items():
        allowed = rp.can_fetch(bot, base_url)
        if not allowed:
            blocked.append(bot)
            findings.append(
                GeoFinding(
                    id=f"geo_ai_crawler_blocked_{bot.lower().replace('-', '_')}",
                    severity="critical",
                    category="robots_txt",
                    message=(
                        f"AI crawler '{bot}' ({description}) is blocked by robots.txt. "
                        f"This prevents your content from being cited by this AI system."
                    ),
                    recommendation=(
                        f"Add the following to robots.txt:\nUser-agent: {bot}\nAllow: /"
                    ),
                    evidence=f"robots.txt Disallow applies to {bot}",
                    evidence_source=ev_src,
                )
            )

    if not blocked:
        findings.append(
            GeoFinding(
                id="geo_ai_crawlers_allowed",
                severity="pass",
                category="robots_txt",
                message="All major AI search crawlers are allowed by robots.txt.",
                recommendation="No action required.",
                evidence="OAI-SearchBot, ChatGPT-User, PerplexityBot, Claude-SearchBot, Claude-User all accessible",
                evidence_source=ev_src,
            )
        )

    return findings


# ---------------------------------------------------------------------------
# Schema gap audit
# ---------------------------------------------------------------------------


def schema_gap_audit(audit: AuditResult) -> list[GeoFinding]:
    """Check which Tier 1 GEO schemas are missing from the page.

    Uses AuditResult.findings['schema_types'] populated by the existing
    backend.seo.audit pipeline.
    """
    schema_types: list[str] = audit.findings.get("schema_types") or []
    schema_set: set[str] = set(schema_types)
    findings: list[GeoFinding] = []

    # FAQPage check
    if "FAQPage" not in schema_set:
        findings.append(
            GeoFinding(
                id="geo_no_faq_schema",
                severity="high",
                category="schema",
                message=(
                    "FAQPage schema is missing. AI systems (Google AI Overview, "
                    "ChatGPT, Perplexity) extract FAQ answers directly from FAQPage "
                    "JSON-LD. Without it, your answers are less likely to be cited."
                ),
                recommendation=(
                    "Add FAQPage JSON-LD with at least 6 questions and 40-60 word "
                    "answers to your key pages. Use backend.seo.schema_builder.faq_page()."
                ),
                evidence="FAQPage not present in page JSON-LD blocks",
                evidence_source="crawled_html",
            )
        )
    else:
        findings.append(
            GeoFinding(
                id="geo_faq_schema_present",
                severity="pass",
                category="schema",
                message="FAQPage schema is present.",
                recommendation="No action required.",
                evidence="FAQPage detected in JSON-LD",
                evidence_source="crawled_html",
            )
        )

    # HowTo check
    if "HowTo" not in schema_set:
        findings.append(
            GeoFinding(
                id="geo_no_howto_schema",
                severity="medium",
                category="schema",
                message=(
                    "HowTo schema is missing. For service or process-oriented pages, "
                    "HowTo schema significantly raises AI citation probability for "
                    "'how to' queries."
                ),
                recommendation=(
                    "Add HowTo JSON-LD to pages describing a process or service "
                    "delivery. Use backend.seo.schema_builder.how_to()."
                ),
                evidence="HowTo not present in page JSON-LD blocks",
                evidence_source="crawled_html",
            )
        )

    # Article / BlogPosting with dateModified check
    has_article_type = bool(schema_set & ARTICLE_SCHEMA_TYPES)
    if not has_article_type:
        findings.append(
            GeoFinding(
                id="geo_no_article_schema",
                severity="high",
                category="schema",
                message=(
                    "Article schema with datePublished and dateModified is missing. "
                    "AI systems use these fields to assess content freshness. "
                    "Missing dateModified causes your content to be treated as stale."
                ),
                recommendation=(
                    "Add Article JSON-LD with headline, author, datePublished, "
                    "and dateModified to blog/article pages. "
                    "Use backend.seo.schema_builder.article()."
                ),
                evidence="Article/BlogPosting not present in page JSON-LD blocks",
                evidence_source="crawled_html",
            )
        )
    else:
        findings.append(
            GeoFinding(
                id="geo_article_schema_present",
                severity="pass",
                category="schema",
                message="Article-type schema is present.",
                recommendation="Ensure dateModified is updated whenever content changes.",
                evidence=f"Article-type schema detected: {', '.join(schema_set & ARTICLE_SCHEMA_TYPES)}",
                evidence_source="crawled_html",
            )
        )

    return findings


# ---------------------------------------------------------------------------
# Content freshness audit
# ---------------------------------------------------------------------------

_LAST_UPDATED_RE = re.compile(
    r"(?:last\s+updated|updated\s+on|last\s+modified|published\s+on|date)"
    r"[:\s]*"
    r"(\w+\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})",
    re.IGNORECASE,
)

_DATE_MODIFIED_JSON_RE = re.compile(r'"dateModified"\s*:\s*"([^"]+)"', re.IGNORECASE)


def content_freshness_audit(audit: AuditResult, html: str = "") -> list[GeoFinding]:
    """Detect pages with no visible Last Updated date or dateModified schema.

    Uses AuditResult.findings plus optional raw HTML for visible date detection.
    """
    findings: list[GeoFinding] = []

    # Check for dateModified in JSON-LD via raw HTML if provided
    has_date_modified_schema = False
    if html:
        has_date_modified_schema = bool(_DATE_MODIFIED_JSON_RE.search(html))
    else:
        # Fall back to checking article schema types; if article schema exists
        # we flag it as unknown but present
        has_date_modified_schema = bool(
            set(audit.findings.get("schema_types") or []) & ARTICLE_SCHEMA_TYPES
        )

    has_visible_date = False
    if html:
        has_visible_date = bool(_LAST_UPDATED_RE.search(html))

    if not has_date_modified_schema and not has_visible_date:
        findings.append(
            GeoFinding(
                id="geo_content_stale",
                severity="high",
                category="freshness",
                message=(
                    "No visible 'Last Updated' date and no dateModified schema found. "
                    "AI systems heavily weight content freshness. Pages without a "
                    "modification date are ranked lower for citation than fresh content."
                ),
                recommendation=(
                    "Add a visible 'Last updated: YYYY-MM-DD' line near the page title. "
                    "Add dateModified to Article JSON-LD. "
                    "Refresh content quarterly at minimum and update the date each time."
                ),
                evidence="No Last Updated text pattern; no dateModified in JSON-LD",
                evidence_source="crawled_html",
            )
        )
    elif not has_date_modified_schema:
        findings.append(
            GeoFinding(
                id="geo_no_date_modified_schema",
                severity="medium",
                category="freshness",
                message=(
                    "Visible date found on page but dateModified is not set in Article "
                    "schema. Machine-readable freshness signals are more reliable for "
                    "AI systems than visible text."
                ),
                recommendation=(
                    "Add dateModified to Article JSON-LD matching your visible date. "
                    "Use backend.seo.schema_builder.article(date_modified='YYYY-MM-DD')."
                ),
                evidence="Visible date detected; dateModified schema absent",
                evidence_source="crawled_html",
            )
        )
    else:
        findings.append(
            GeoFinding(
                id="geo_freshness_ok",
                severity="pass",
                category="freshness",
                message="Content freshness signals detected.",
                recommendation="Keep dateModified updated on every content refresh.",
                evidence="dateModified schema or visible date found",
                evidence_source="crawled_html",
            )
        )

    return findings


# ---------------------------------------------------------------------------
# Topical authority gaps
# ---------------------------------------------------------------------------

_CLUSTER_TEMPLATES: dict[str, list[str]] = {
    "default": [
        "What is {kw} and why does it matter for {industry}?",
        "How does {kw} work step by step?",
        "What are the biggest {kw} mistakes {industry} businesses make?",
        "How much does {kw} cost for a small business?",
        "What results can you expect from {kw} in the first 90 days?",
        "How do you choose the right {kw} partner?",
        "What is the difference between {kw} and traditional SEO?",
        "How do you measure {kw} success?",
        "Which {kw} tactics have the highest ROI for local businesses?",
        "How do you build a {kw} strategy that compounds over time?",
    ],
}


def topical_authority_gaps(
    primary_kw: str,
    industry: str = "",
    secondary_kws: list[str] | None = None,
) -> TopicalClusterPlan:
    """Return a 10-article cluster plan for building topical authority.

    Titles are question-formatted for maximum AI extractability.
    """
    secondary_kws = secondary_kws or []
    cluster_name = f"{primary_kw.title()} for {industry.title() or 'Local Businesses'}"
    templates = _CLUSTER_TEMPLATES.get(industry.lower(), _CLUSTER_TEMPLATES["default"])

    articles = [t.format(kw=primary_kw, industry=industry or "local service") for t in templates]

    # Replace last 2 with secondary keyword variations if available
    if secondary_kws:
        for i, skw in enumerate(secondary_kws[:2]):
            idx = len(articles) - 1 - i
            articles[idx] = f"How does {skw} complement your {primary_kw} strategy?"

    return TopicalClusterPlan(
        cluster_name=cluster_name,
        primary_keyword=primary_kw,
        articles=articles,
    )


# ---------------------------------------------------------------------------
# GEO score
# ---------------------------------------------------------------------------


def geo_score(findings: list[GeoFinding]) -> int:
    """Compute 0-100 GEO readiness score from a flat findings list.

    Deductions: critical=-15, high=-10, medium=-5. Passes add +0 (floor 0).
    """
    score = 100
    for f in findings:
        if f.severity == "critical":
            score -= 15
        elif f.severity == "high":
            score -= 10
        elif f.severity == "medium":
            score -= 5
    return max(0, min(100, score))


# ---------------------------------------------------------------------------
# robots.txt snippet builder (delegated to schema_builder module)
# ---------------------------------------------------------------------------


def build_robots_txt_ai_block() -> str:
    """Generate a robots.txt snippet allowing all AI search crawlers.

    Returns a paste-ready multi-line string.
    """
    lines = ["# AI search crawler access (GEO)"]
    for bot in AI_CRAWLERS:
        lines.append(f"User-agent: {bot}")
        lines.append("Allow: /")
        lines.append("")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Full audit entry point
# ---------------------------------------------------------------------------


def run_geo_audit(
    audit: AuditResult,
    primary_kw: str = "",
    secondary_kws: list[str] | None = None,
    industry: str = "",
    html: str = "",
) -> GeoAuditResult:
    """Run a full GEO audit against an existing SEO AuditResult.

    Parameters
    ----------
    audit:        Completed AuditResult from backend.seo.audit.audit_url()
    primary_kw:   Primary target keyword for topical cluster planning
    secondary_kws: Supporting keywords
    industry:     Business industry label (e.g. "plumbing", "dental")
    html:         Raw page HTML for freshness detection (optional)
    """
    secondary_kws = secondary_kws or []

    robots = robots_txt_audit(audit.url)
    schema = schema_gap_audit(audit)
    freshness = content_freshness_audit(audit, html=html)

    topical: TopicalClusterPlan | None = None
    if primary_kw:
        topical = topical_authority_gaps(primary_kw, industry, secondary_kws)

    all_findings = robots + schema + freshness
    score = geo_score(all_findings)
    snippet = build_robots_txt_ai_block()

    return GeoAuditResult(
        url=audit.url,
        geo_score=score,
        findings=all_findings,
        robots_findings=robots,
        schema_findings=schema,
        freshness_findings=freshness,
        topical_plan=topical,
        robots_txt_snippet=snippet,
        ts=iso_now(),
    )
