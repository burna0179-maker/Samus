# SEO Implementation — Operator Delivery Playbook

**SKU:** `service_seo_implementation` · **Price:** TBD per engagement (set at scope confirmation based on fix count + site complexity) · **SLA:** 7 days from scope-confirmation reply.

This is the post-audit fix-application engagement. It consumes the prioritized fix list from a delivered `SEO Audit` (the existing fulfill.py SEO chain) and turns recommendations into either (a) applied changes on the customer's site or (b) a versioned change set + apply-instructions when we don't have site access.

---

## Pre-requisites

- **A delivered SEO Audit** for the customer's site. Path: `<SAMUS_ARTIFACT_ROOT>/customers/<slug>/seo_report.md` (written by `backend.seo.service.audit_and_report`). If no audit exists, run the audit first — never apply fixes blind.
- **One of:** site CMS admin access (Wordpress / Webflow / Squarespace / Shopify), OR Git push access to a static site repo, OR explicit "we'll apply your change set ourselves" agreement (in which case our deliverable is a change set + instructions, not applied changes).
- **Google Search Console verified** for the property. Required for reindex submissions in Phase 3.

---

## Phase 1 — Triage (Day 1)

1. **Pull the audit deliverable.** Open `<SAMUS_ARTIFACT_ROOT>/customers/<slug>/seo_report.md` and the underlying `AuditResult` JSON in the JSONL ledger at `SAMUS_SEO_AUDIT_PATH`. The recommendations from `optimize_page` are your starting fix list.
2. **Classify each fix** using the 4-category taxonomy in [`fix_categories.md`](fix_categories.md):
   - `technical` — crawlability, indexability, site speed, mobile, schema
   - `on_page` — title tags, meta descriptions, H1s, internal linking, URL structure
   - `content` — thin content, duplicate content, keyword targeting, content gaps
   - `local` — NAP consistency, Google Business Profile, local schema, citation cleanup
3. **Prioritize by impact × effort.** Use the priority matrix in `fix_categories.md`. Fix everything in P1 (high impact × low effort) before any P3 (low impact × high effort).
4. **Build the fix manifest.** Create `<SAMUS_ARTIFACT_ROOT>/customers/<slug>/service_seo_implementation/fix_manifest.md` with one row per fix:
   ```
   | id | category | priority | description | target_url | current_value | proposed_value | applied | reindex_required |
   ```
5. **Send fix manifest preview to customer.** Customer needs to approve the manifest before we touch their site. Especially important for content changes (rewriting titles/H1s — customer voice matters).

## Phase 2 — Apply (Day 2-5)

6. **Pick the application method based on access:**
   - **CMS admin path:** log in, apply changes through CMS UI, capture before/after screenshots in `<customer>/service_seo_implementation/screenshots/`.
   - **Git push path:** create a single PR per category (technical / on-page / content / local). Each PR contains the changes + a checklist in the PR description matching the fix manifest.
   - **Change-set delivery path:** generate a `change_set.md` with each fix expressed as "find this exact string → replace with this string" instructions, paired with a `change_set.zip` containing modified files when applicable. Customer applies; we don't.
7. **Apply in dependency order:**
   - Technical fixes first (crawlability blockers — fixing on-page content while pages are noindexed is wasted work).
   - On-page second (titles, metas, H1s — these get reindexed immediately).
   - Content third (rewrites — bigger crawler cycles).
   - Local last (GBP + citations — independent of site changes).
8. **One commit / one CMS save = one fix.** Atomic changes. If you have to roll back, you want surgical undo.
9. **Mark each fix `applied=true` in the manifest** as you go. The manifest is the audit trail.

## Phase 3 — Reindex + verify (Day 5-6)

10. **Submit changed URLs to GSC.** For each URL touched, request indexing via Search Console UI (or URL Inspection API). Quota: 10/day via UI, 200/day via API. If quota exceeded, queue + finish next day — don't skip.
11. **Re-run the SEO Audit** against changed pages. Compare before/after `seo_score`. Document delta in `before_after.md`.
12. **Spot-check via incognito browser.** Confirm changes are visible (CMS caches sometimes lie). Confirm no regressions on adjacent pages.
13. **Local-fix verification.** Google Business Profile changes take 24-72h to propagate; citation site changes vary. Document expected propagation window in `before_after.md`.

## Phase 4 — Handoff (Day 6-7)

14. **Send delivery email** with:
    - Fix manifest with all applied=true rows highlighted
    - before/after audit scores per page
    - Screenshots from Phase 2
    - GSC reindex submission proof (timestamps + URL list)
    - The expected timeline for ranking impact (4-8 weeks for most changes, 8-12 weeks for content changes)
    - Soft upsell to SEO Automation SKU (monitoring/alerting setup) and/or SEO Optimization retainer (ongoing fix cycles)
15. **Mark delivered.** `sla_timer.mark_delivered(customer_id=<slug>, sku_id="service_seo_implementation")` + advance customer state to `delivered`.

---

## Pricing guidance (operator → customer at scope confirmation)

| Site size / complexity | Typical fix count | Suggested price |
|---|---|---|
| 1-10 page brochure / local business | 5-15 fixes | $500-$800 |
| 10-50 page agency / SaaS site | 15-40 fixes | $800-$1,500 |
| 50-500 page content/blog site | 40-100 fixes | $1,500-$3,000 |
| 500+ page e-commerce / large CMS | 100+ fixes | quote as Buildout-style engagement |

Price is set at scope confirmation, not at purchase — that's why `price_usd_cents` is None in the SKU registry. Send the customer a Stripe invoice after the fix manifest is approved.

## Failure modes + recovery

| Failure | Recovery |
|---|---|
| Customer can't / won't grant CMS access | Pivot to change-set delivery path. Adds 1 day to SLA for instruction-writing. |
| Audit deliverable doesn't exist for this customer | STOP. Run the audit first. Don't implement blind. |
| Fix manifest preview rejected by customer | Iterate within Phase 1 (Day 1). If we're still iterating on Day 3, the engagement is mis-scoped — either re-scope as Buildout or refund. |
| Reindex quota exhausted | Submit via API + spread over multiple days. SLA exception documented in the customer state event. |
| Before/after audit shows no score improvement | The audit's score is a heuristic, not Google's rank. Document why each fix matters in the delivery email (link to fix_categories.md). |
| Customer expects ranking jump within the SLA | Set expectation up-front (in scope.md): SLA covers fix application. Ranking impact is 4-12 weeks downstream. |

## Artifact dir structure (per customer)

```
<SAMUS_ARTIFACT_ROOT>/customers/<slug>/service_seo_implementation/
├── scope.md                # auto-written by fulfill_service
├── fix_manifest.md          # built in Phase 1, marked off in Phase 2
├── before_after.md          # Phase 3 score deltas
├── screenshots/             # before/after captures (CMS path)
├── change_set.md            # if change-set delivery path used
├── change_set.zip           # if change-set delivery path used
├── reindex_log.md           # URLs submitted + GSC response timestamps
└── delivery_email.md        # final email content (for the audit trail)
```
