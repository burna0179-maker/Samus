# SEO Optimization — Fix Priority Matrix

How to decide which fixes to apply in a given cycle. The retainer has a
**fixed time budget** (~6-8 hours of operator work per customer per
month at $300/mo), so we pick fixes that maximize SEO-score-delta per
hour invested.

## The matrix

Each fix gets two scores 1-5:

- **Impact** — likely SEO score improvement + traffic effect
- **Effort** — operator hours needed (5 = quick, 1 = multi-day)

Multiplied together gives a **Priority** score 1-25. Apply highest first.

| Severity tier | Examples | Impact | Effort | Priority | Apply? |
|---|---|---|---|---|---|
| Critical regression | indexing broken; site demoted | 5 | varies | 25 | Always — escalate if effort >2 |
| Schema markup | LocalBusiness, Service, FAQ, Breadcrumb | 4 | 5 (quick) | 20 | Always priority 1 in month 1 |
| Broken internal links | 404s from stale redirects | 3 | 5 | 15 | Always — batch with a single crawl |
| Meta description rewrites | top-10 impression pages | 4 | 4 | 16 | Always in month 1, refresh quarterly |
| Image compression | LCP regression | 4 | 4 | 16 | When LCP > 2.5s |
| Canonical tag fixes | paginated / faceted URLs | 3 | 4 | 12 | When GSC shows duplicate content warnings |
| New content (long-form) | competitor counter-content | 5 | 1 | 5 | Only with explicit customer ask + buy-in on scope |
| Backlink outreach | local directories | 4 | 2 | 8 | Month 2+ — slow burn |
| Core Web Vitals tuning | INP, CLS, FID | 4 | 2 | 8 | When PSI flags critical |
| FAQ section adds | high-impression pages | 4 | 4 | 16 | Captures rich snippets — high ROI |
| H1 / title rewrites | top-5 keyword pages | 3 | 5 | 15 | Refresh every 2 months |
| Internal linking | underperforming pages | 3 | 4 | 12 | Always — easy wins |
| Mobile usability | tap targets, viewport | 3 | 4 | 12 | When PSI flags |
| GBP optimization | Google Business Profile | 4 | 3 | 12 | Month 1 + when reviews drop |
| Sitemap / robots.txt | submission, crawl errors | 2 | 5 | 10 | When GSC flags |

## Decision rules

**Rule 1: Critical regressions break the priority order.**
If the site is suddenly demoted (rank drop of 5+ on a top-3 keyword,
indexing collapse, manual action), drop everything and diagnose first.

**Rule 2: Month 1 is the foundation.**
First-cycle priorities are non-negotiable in this order:

1. Schema markup (every service page gets LocalBusiness + Service)
2. Broken internal links (full crawl + fix)
3. Meta descriptions on top-10 impression pages
4. NAP consistency check (Yelp, GBP, directory listings)
5. Sitemap submission to GSC

If those eat the month's budget, that's fine — the customer's score
should jump 8-15 points in month 1 alone from this work.

**Rule 3: Month 2-3 is the visibility climb.**
Customer should expect to see rank movement now. Priorities shift to:

- FAQ sections (rich snippet capture)
- Image compression + LCP work (mobile rankings)
- Internal linking restructure
- Continued meta refinement based on GSC CTR data

**Rule 4: Month 4+ is competitor-aware.**
By now the diff-against-prior-month has 3 cycles of baseline. Focus
shifts to competitor-reactive plays:

- Counter their new content with deeper content
- Acquire backlinks they don't have
- Optimize for "people also ask" they're winning
- Add structured data they're missing

**Rule 5: New content is a customer-buy-in decision.**
Long-form content (1,500+ words) takes 4-8 hours and bursts the monthly
budget. We DON'T ship it inside the standard $300/mo cycle without an
explicit customer ask. If the customer agrees and wants to pay for one
big piece, it becomes a Week 3-4 deliverable for that month and the
report frames it as "this cycle's strategic investment."

## What we DON'T do at $300/mo

To stay sustainable, the following are explicitly out of scope at this
tier (operator should respond "let's talk about scaling up" if asked):

- Custom blog content writing (high-touch, ~$200/post elsewhere)
- Link building campaigns beyond local directory submissions
- Paid SEO tools beyond what the operator already pays for (Ahrefs/SEMrush)
- Site migrations / replatforms
- Multi-language / hreflang setup
- E-commerce / Shopify / WooCommerce schema beyond basic Product markup
- Local SEO at scale (>3 locations)
- Penalty recovery work

Customers wanting any of the above should be upsold to a custom-scope
engagement (one-shot SEO project) or moved to a higher retainer tier
when one exists.

## Fix-log discipline

Every fix applied goes into the visibility report's "Fixes applied this
month" section with **specific evidence**:

- BAD: "Updated meta descriptions"
- GOOD: "Rewrote meta descriptions on the 8 highest-impression pages to
  include action verbs and city qualifier"

Specific evidence is what makes the customer feel the $300/month landed.
Vague claims signal a generic checklist; specifics signal real work.
