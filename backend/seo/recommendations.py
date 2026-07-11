"""Deterministic on-page-optimization recommendations from an :class:`AuditResult`.

Each :class:`SeoIssue` maps to (recommendation, on-page change suggestion).
Priority is derived from severity (critical=5, high=4, medium=3, low=2).
"""
from __future__ import annotations

from .models import AuditResult, OptimizationRecommendation


_SEVERITY_PRIORITY = {"critical": 5, "high": 4, "medium": 3, "low": 2}


def _suggest_on_page(issue_id: str, audit: AuditResult, kw: list[str]) -> tuple[str, str] | None:
    """Return ``(field, suggested_value)`` for issues that map to an on-page change."""
    primary_kw = kw[0] if kw else ""
    findings = audit.findings
    industry = findings.get("industry", "") or "service"

    if issue_id in ("missing_title", "title_length"):
        base = primary_kw or (findings.get("title") or industry)
        return ("title", f"{base.title()} | Trusted {industry.title()} Services")
    if issue_id in ("missing_meta_description", "meta_description_length"):
        if primary_kw:
            return ("meta_description",
                    f"Looking for {primary_kw}? Our {industry} team delivers fast, reliable "
                    f"results with transparent pricing and local expertise. Call today.")
        return ("meta_description",
                f"Trusted local {industry} services. Fast response, transparent pricing, "
                "real results -- get in touch today.")
    if issue_id in ("missing_h1", "multiple_h1"):
        return ("h1", (primary_kw or industry).title() + " You Can Trust")
    if issue_id == "missing_viewport_meta":
        return ("viewport_meta", "width=device-width, initial-scale=1")
    # --- Enrichment recommendations (2026-05-16) ---
    if issue_id == "missing_canonical":
        # Use the audited URL itself as the canonical default — operator
        # can adjust if they've consolidated to www / non-www differently.
        return ("canonical_link",
                f'<link rel="canonical" href="{audit.url}">')
    if issue_id == "missing_og_title":
        title = findings.get("title") or (primary_kw or industry).title()
        return ("og_title",
                f'<meta property="og:title" content="{title[:90]}">')
    if issue_id == "missing_og_image":
        return ("og_image",
                '<meta property="og:image" content="https://your-site.com/og-image.jpg">')
    if issue_id == "missing_schema_org":
        # Minimal LocalBusiness schema scaffold — operator fills the
        # address/telephone/openingHours. Keeps the suggestion short
        # enough to fit the on-page-changes table in the report.
        return ("schema_org_jsonld",
                '<script type="application/ld+json">'
                '{"@context":"https://schema.org","@type":"LocalBusiness",'
                '"name":"...","telephone":"...","address":{"@type":"PostalAddress",'
                '"streetAddress":"...","addressLocality":"...","addressRegion":"...",'
                '"postalCode":"..."}}</script>')
    if issue_id == "missing_local_business_schema":
        return ("schema_org_local_business",
                f'add "@type":"LocalBusiness" (or a specific subtype like '
                f'"{industry.title()}") to your existing JSON-LD block')
    # --- GEO recommendations (2026-06-11) ---
    if issue_id == "geo_ai_crawlers_blocked":
        return ("robots_txt_ai_block",
                "# Add to robots.txt:\nUser-agent: OAI-SearchBot\nAllow: /\n"
                "User-agent: ChatGPT-User\nAllow: /\n"
                "User-agent: PerplexityBot\nAllow: /\n"
                "User-agent: Claude-SearchBot\nAllow: /\n"
                "User-agent: Claude-User\nAllow: /")
    if issue_id == "geo_no_faq_schema":
        return ("schema_faqpage_jsonld",
                '<script type="application/ld+json">{"@context":"https://schema.org",'
                '"@type":"FAQPage","mainEntity":[{"@type":"Question","name":"...",'
                '"acceptedAnswer":{"@type":"Answer","text":"..."}}]}</script>')
    if issue_id == "geo_content_stale":
        return ("content_last_updated",
                "Add visible text: 'Last updated: YYYY-MM-DD' near the page title, "
                "and set dateModified in Article JSON-LD.")
    if issue_id == "geo_no_article_schema":
        return ("schema_article_jsonld",
                '<script type="application/ld+json">{"@context":"https://schema.org",'
                '"@type":"Article","headline":"...","author":{"@type":"Person","name":"..."},'
                '"datePublished":"YYYY-MM-DD","dateModified":"YYYY-MM-DD"}</script>')
    return None


def _recommendation_for(issue_id: str, severity: str, category: str,
                       audit: AuditResult, kw: list[str]) -> OptimizationRecommendation:
    primary_kw = kw[0] if kw else ""
    priority = _SEVERITY_PRIORITY.get(severity, 2)

    mapping = {
        "missing_title":
            ("title", "Add a <title> tag with the primary keyword",
             "Title tags are the highest-impact on-page SEO signal."),
        "title_length":
            ("title", "Rewrite the <title> to fit 10-70 chars",
             "Search engines truncate titles outside this range."),
        "missing_meta_description":
            ("meta_description", "Add a meta description (50-160 chars)",
             "Meta descriptions drive click-through rate from SERPs."),
        "meta_description_length":
            ("meta_description", "Tighten the meta description to 50-160 chars",
             "Out-of-range descriptions get rewritten by Google."),
        "missing_h1":
            ("heading", "Add a single descriptive <h1> heading",
             "Search engines and screen readers weight h1 heavily."),
        "multiple_h1":
            ("heading", "Reduce to one <h1>; demote the others to <h2>",
             "Multiple h1s dilute topical relevance signals."),
        "missing_viewport_meta":
            ("mobile", "Add a viewport meta tag for mobile responsiveness",
             "Mobile-first indexing penalises pages without a viewport meta."),
        "missing_local_signals":
            ("local", "Add visible phone number + street address",
             "Local pack ranking requires NAP signals on the page."),
        "mixed_content":
            ("technical", "Migrate HTTP sub-resources to HTTPS",
             "Mixed content blocks indexing and breaks the lock icon."),
        "noindex_directive":
            ("technical", "Remove the <meta robots noindex> tag if SEO is desired",
             "Noindex pages are excluded from search results entirely."),
        "fetch_failed":
            ("technical", "Resolve fetch failure (DNS / 5xx / timeout)",
             "Audit cannot proceed if the page does not return 2xx."),
        "blocked_by_robots":
            ("technical",
             "Remove the Disallow rule from robots.txt for this URL",
             "Your robots.txt is telling Googlebot + other crawlers not to "
             "index this page. As long as that rule is in place, you will "
             "not appear in any search result regardless of what's on the "
             "page itself."),
        # --- PageSpeed Insights (Cut C, 2026-05-16) ---
        "pagespeed_performance_poor":
            ("technical",
             "Improve mobile Performance score above 50 (start with images + "
             "render-blocking scripts)",
             "Google's mobile-first index uses Performance as a ranking "
             "signal. The two highest-leverage fixes for service-business "
             "sites: compress + resize images (most pages have 5x more image "
             "weight than needed), and remove render-blocking third-party "
             "scripts above the fold."),
        "pagespeed_lcp_poor":
            ("technical",
             "Reduce Largest Contentful Paint below 2500 ms (hero image is "
             "usually the culprit)",
             "LCP is the time until the page's main content is visible. "
             "Above 4 seconds is the 'poor' tier — Google penalizes it AND "
             "users bounce. The most common cause on small-business sites "
             "is an unoptimized hero image; the second is render-blocking "
             "JavaScript."),
        "pagespeed_cls_poor":
            ("technical",
             "Eliminate layout shift below 0.1 (size all images + ad slots "
             "explicitly)",
             "CLS is how much your page jumps around as it loads. Above 0.25 "
             "is 'poor' — users mis-tap buttons that move under their finger "
             "and Google ranks the page lower. The fix is almost always "
             "adding width + height attributes to images and reserving space "
             "for any embedded widgets that load late."),
        # Needs-improvement tier (added 2026-05-16 alongside the 'poor' tier
        # that shipped in Cut C). Different action language: NI fixes are
        # opportunistic, not emergencies.
        "pagespeed_performance_needs_improvement":
            ("technical",
             "Lift mobile Performance into the green band (>=90) when "
             "convenient",
             "Performance in the 50-89 range isn't actively penalized but "
             "leaves ranking + conversion headroom on the table. Highest-"
             "leverage one-pass fixes: serve images in next-gen formats "
             "(WebP / AVIF) and defer any third-party scripts not "
             "needed for the first paint."),
        "pagespeed_lcp_needs_improvement":
            ("technical",
             "Trim LCP toward 2500ms when you next touch the homepage",
             "LCP in the 2500-4000ms band means the largest above-the-fold "
             "element (usually the hero image) is loading slowly. Common "
             "fixes: preload the hero image with <link rel=\"preload\">, "
             "compress it to <100KB, and serve a responsive srcset so "
             "mobile downloads a smaller file."),
        "pagespeed_cls_needs_improvement":
            ("technical",
             "Reduce layout shift below 0.1 by reserving space for late-"
             "loading widgets",
             "CLS in the 0.1-0.25 band means content shifts under the "
             "user's tap target as they scroll. Add explicit width + "
             "height attributes to every <img> and reserve space for any "
             "iframe / ad / embed that loads after the initial paint."),
        # --- Enrichment recommendations (2026-05-16) ---
        "missing_canonical":
            ("technical",
             "Add a <link rel=\"canonical\"> tag to every page",
             "Without it, Google treats www / non-www / trailing-slash URLs as "
             "duplicates and splits your ranking power across them."),
        "missing_og_title":
            ("content",
             "Add <meta property=\"og:title\"> so social shares look intentional",
             "Without OG tags, link previews on Facebook / LinkedIn / iMessage "
             "fall back to the raw <title> or appear blank."),
        "missing_og_image":
            ("content",
             "Add <meta property=\"og:image\"> with a 1200x630 share image",
             "Social shares without thumbnails get ~3x lower click-through "
             "than shares with an image preview."),
        "missing_schema_org":
            ("technical",
             "Add LocalBusiness schema.org JSON-LD to every page",
             "Structured data is the primary mechanism Google uses to "
             "understand what your business is + where you operate. It powers "
             "rich results, knowledge-panel data, and Google Maps listings."),
        "missing_local_business_schema":
            ("local",
             "Add a LocalBusiness @type (or a specific subtype) to your "
             "JSON-LD",
             "Generic schema (WebSite, Article) doesn't trigger local-pack "
             "ranking. LocalBusiness + NAP fields is what gets you into the "
             "map listings on \"<industry> near me\" searches."),
        "low_alt_text_coverage":
            ("content",
             "Add descriptive alt text to images missing it",
             "Image search is a meaningful local-services traffic channel. "
             "Alt text is also legally required for ADA accessibility on "
             "any site offering goods or services."),
        "low_alt_text_coverage_critical":
            ("content",
             "Add alt text to all images -- coverage is below 50%",
             "More than half your images are invisible to search engines + "
             "screen-reader users. This is both an SEO and a legal-risk "
             "issue under ADA / WCAG."),
        "no_internal_links":
            ("technical",
             "Add internal links from this page to other pages on your site",
             "Internal links are how Google discovers + ranks the rest of "
             "your content. A page with no internal links is an orphan -- "
             "crawlers reaching it learn nothing about the rest of your site."),
        "no_analytics_detected":
            ("technical",
             "Install Google Analytics 4 (or GTM with a GA4 tag)",
             "Without analytics you can't measure which marketing channels "
             "actually bring in customers. Every SEO + ad decision becomes "
             "a guess. GA4 is free and installs as a single script tag."),
        "legacy_analytics_only":
            ("technical",
             "Upgrade from Universal Analytics (UA) to GA4",
             "Universal Analytics stopped processing data on July 1, 2023. "
             "Your dashboard is showing nothing current -- every reporting "
             "decision is being made on stale numbers."),
        # --- GEO / AI citation recommendations (2026-06-11) ---
        "geo_ai_crawlers_blocked":
            ("technical",
             "Allow AI search crawlers in robots.txt "
             "(OAI-SearchBot, PerplexityBot, Claude-SearchBot, ChatGPT-User, Claude-User)",
             "These are the user-agent strings used by OpenAI, Perplexity, and Anthropic "
             "when indexing your content for AI citation. Blocking them removes your site "
             "from AI-generated answers entirely, which is an invisible traffic loss "
             "that traditional SEO tools do not detect."),
        "geo_no_faq_schema":
            ("technical",
             "Add FAQPage JSON-LD schema with at least 6 question-and-answer pairs",
             "FAQPage schema is the single highest-leverage AI citation signal for "
             "small-business sites. Google AI Overview, ChatGPT Browse, and Perplexity "
             "all extract FAQ answers directly from FAQPage JSON-LD when constructing "
             "their responses. Without it, your answers must be inferred from prose, "
             "which is less reliable and cited less often."),
        "geo_content_stale":
            ("content",
             "Add a visible 'Last updated: YYYY-MM-DD' date and dateModified in Article schema",
             "AI systems treat content freshness as a quality proxy. A page with no "
             "dateModified is ranked behind a page with a recent one, even if the "
             "content is identical. Updating the date quarterly costs under 5 minutes "
             "and has a measurable impact on citation frequency."),
        "geo_no_article_schema":
            ("technical",
             "Add Article JSON-LD with author, datePublished, and dateModified to blog/article pages",
             "Article schema with dateModified is required for AI systems to assess "
             "content freshness. It also enables the author byline in Google Search "
             "results, which increases click-through rate. Missing dateModified causes "
             "your content to be treated as undated, which is treated as stale."),
    }
    area, action, rationale = mapping.get(
        issue_id,
        (category, f"Resolve {issue_id}", f"{severity} severity issue in {category}."),
    )
    if primary_kw and area in ("title", "meta_description", "heading"):
        action = f"{action} (target keyword: '{primary_kw}')"
    return OptimizationRecommendation(area=area, action=action,
                                      rationale=rationale, priority=priority)


def build_recommendations(audit: AuditResult,
                          target_keywords: list[str]) -> tuple[list[OptimizationRecommendation], dict[str, str]]:
    """Return ``(recommendations, on_page_changes)``."""
    recs: list[OptimizationRecommendation] = []
    on_page: dict[str, str] = {}
    for issue in audit.issues:
        recs.append(_recommendation_for(
            issue.id, issue.severity, issue.category, audit, target_keywords))
        sug = _suggest_on_page(issue.id, audit, target_keywords)
        if sug is not None:
            field, value = sug
            on_page.setdefault(field, value)
    recs.sort(key=lambda r: -r.priority)
    return recs, on_page
