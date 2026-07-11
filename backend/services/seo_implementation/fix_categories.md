# SEO Implementation — Fix Taxonomy + Priority Scoring

Four categories, each with priority bands. Use this to triage the audit's recommendation list into a Phase 1 fix manifest. Priority = `impact × (1/effort)` — high-impact + low-effort fixes ship first.

---

## Category 1: Technical

Fixes here unblock everything else. Page can't rank if Google can't crawl/render/index it. Always P1 unless the fix is genuinely high-effort.

| Fix | Typical impact | Typical effort | Priority band |
|---|---|---|---|
| Missing/broken sitemap.xml | High (crawl coverage) | Low (regenerate from CMS) | **P1** |
| robots.txt blocking important paths | Critical (de-indexes pages) | Low (1-line edit) | **P1** |
| noindex meta tag on pages that should rank | Critical (de-indexes pages) | Low (remove tag) | **P1** |
| Slow page load (>3s LCP) | High (rank + UX) | Medium (image opt, code split) | **P2** |
| Mobile usability issues (viewport, tap targets) | High (mobile-first index) | Low-medium (CSS fixes) | **P1-P2** |
| Missing structured data / schema | Medium (rich results) | Low-medium (JSON-LD insertion) | **P2** |
| Broken internal links (404s) | Medium (crawl waste + UX) | Low (audit + redirect) | **P1** |
| HTTPS misconfigurations (mixed content) | High (trust signal) | Low-medium (cert + redirect) | **P1** |
| Duplicate URLs (trailing slash, www vs non-www, http vs https) | High (rank dilution) | Low (canonical + redirect) | **P1** |
| Missing/wrong canonical tags | Medium (rank dilution) | Low (template fix) | **P2** |
| JavaScript-rendered content not in HTML | High (Google may not render) | High (SSR / pre-render setup) | **P3** |
| Crawl budget waste (paginated archives, faceted nav) | Medium (large sites only) | Medium (robots + meta) | **P2** |

## Category 2: On-Page

Rewriting title tags, meta descriptions, H1s, and URL slugs. Bread-and-butter SEO. Almost always P1-P2 — low effort per fix, immediate Google reindex.

| Fix | Typical impact | Typical effort | Priority band |
|---|---|---|---|
| Missing or default `<title>` tags | High (CTR + relevance) | Low (CMS edit) | **P1** |
| `<title>` too long (truncated in SERP) | Medium (CTR loss) | Low | **P1** |
| Missing meta description | Medium (CTR loss) | Low | **P1** |
| Generic meta description (same on every page) | Medium (CTR loss) | Low | **P2** |
| Missing or multiple `<h1>` tags | Medium (relevance signal) | Low | **P1** |
| Keyword-stuffed titles/H1s | Medium-high (penalty risk + UX) | Low | **P1** |
| Image alt text missing | Low-medium (accessibility + image search) | Low (bulk edit) | **P2** |
| Internal linking gaps (orphan pages, no related-content links) | Medium-high (rank distribution) | Medium | **P2** |
| Bad URL structure (UUIDs, deep paths, query-param URLs) | Medium (CTR + crawl) | High (requires redirects) | **P3** |
| Missing breadcrumb navigation | Low-medium (rich results) | Medium | **P3** |

## Category 3: Content

Rewriting body content. Highest leverage but highest effort and longest lag-to-impact (8-12 weeks for content changes to show ranking impact). Requires customer collaboration on voice/messaging.

| Fix | Typical impact | Typical effort | Priority band |
|---|---|---|---|
| Thin content (<300 words on commercial pages) | High (rank floor) | High (rewrite) | **P2** |
| Duplicate content (multiple pages targeting same query) | High (rank dilution) | Medium (consolidate + canonical) | **P2** |
| Missing target keyword in body / H1 / title | Medium (relevance) | Low (insert phrase) | **P1** |
| Keyword stuffing in body | Medium-high (penalty risk) | Medium (rewrite) | **P2** |
| Missing FAQ / People-Also-Ask coverage | Medium-high (rich results + long-tail) | Medium (add FAQ block) | **P2** |
| Content out-of-date (stats, dates, screenshots) | Low-medium (trust + UX) | Medium-high (refresh) | **P3** |
| Missing internal content (queries the site doesn't address) | High (long-term traffic) | Very high (write new content) | **P3 or move to retainer** |
| Bad readability (long paragraphs, no subheads, walls of text) | Low-medium (UX + dwell time) | Medium (restructure) | **P3** |

## Category 4: Local (Local SEO only — skip for non-local businesses)

For brick-and-mortar / service-area businesses targeting "near me" / city-specific queries.

| Fix | Typical impact | Typical effort | Priority band |
|---|---|---|---|
| Google Business Profile incomplete | Critical (local pack rank) | Low (fill profile fields) | **P1** |
| NAP inconsistency across citations (Yelp, BBB, Yellow Pages, etc.) | High (local trust signal) | High (citation cleanup service or manual) | **P2** |
| Missing LocalBusiness schema | Medium (rich results) | Low (JSON-LD insertion) | **P2** |
| Wrong business category on GBP | High (local rank) | Low | **P1** |
| No GBP photos | Medium (GBP CTR) | Low-medium (collect + upload) | **P2** |
| No reviews / low review count | High (local rank + conversion) | High (review acquisition is ongoing) | **P3 or move to retainer** |
| Bad reviews unanswered | Medium (perception) | Low-medium (write responses) | **P2** |
| Service-area definition wrong on GBP | Medium (visibility) | Low | **P1** |
| Missing location pages for service-area business | High (multi-city queries) | High (write per-city content) | **P3 or move to retainer** |
| Bing Places + Apple Maps unclaimed | Low-medium (10-15% of local search) | Low | **P2** |

---

## Priority bands explained

- **P1 (ship Day 2):** Critical + low effort. These are the "you'd be silly not to fix this" items. Always include in scope.
- **P2 (ship Day 3-4):** High-impact + medium effort. Default in scope unless customer's fix manifest gets too long.
- **P3 (ship Day 5 if time / quote separately):** Medium-impact + high effort, OR low-impact + low effort. Default OUT of scope for the 7-day SLA; quote separately or move to retainer.
- **Move to retainer:** Anything ongoing (review acquisition, content writing program, citation maintenance). One-shot Implementation engagement is wrong vehicle.

## Manifest priority assignment

When building the Phase 1 fix manifest, assign priority by this matrix:

|                        | Effort: Low | Effort: Medium | Effort: High |
|------------------------|-------------|----------------|--------------|
| **Impact: Critical**   | P1          | P1             | P2           |
| **Impact: High**       | P1          | P2             | P3           |
| **Impact: Medium**     | P2          | P3             | Out / retainer |
| **Impact: Low**        | P3          | Out / retainer | Out          |

Engagement size guidance — typical fix counts that fit a 7-day SLA:

- **Small site (1-10 pages):** 10-15 P1+P2 fixes
- **Medium site (10-50 pages):** 20-35 P1+P2 fixes
- **Large site (50+ pages):** 35-60 P1+P2 fixes, P3s explicitly deferred

If the audit surfaces >60 fixes and customer wants them all done, re-scope as multiple engagements OR upsell to a retainer with monthly fix cycles.
