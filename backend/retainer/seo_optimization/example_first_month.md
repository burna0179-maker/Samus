# Example First Month — SEO Optimization

A walk-through of a real first cycle, end-to-end. Customer details are
representative composites, not a single real client.

## The customer

**Jordan Reyes**, owner-operator at Acme Plumbing, Sacramento. Family
business, 18 years. Two trucks, four employees. Site is `acme-plumbing.example.com`
— WordPress, last touched by a freelancer in 2024.

Found us via Google search for "SEO help small business sacramento."
Read our public SEO Optimization page, clicked the Stripe link, paid
the first $300 on 2026-04-22. Enrolled mid-month, so the first cycle is
scheduled for 2026-05-01.

Going into the cycle, Jordan's site:

- Ranks #18 for "plumber sacramento" (page 2)
- Has no schema markup
- Has 31 open SEO issues (per the audit Stream 1 produced)
- LCP of 3.4s (mobile, failing)
- Inconsistent NAP across Yelp + GBP + Yellow Pages

Jordan's stated goal in the intake form: "More phone calls from people
who need a plumber today."

## Week of 2026-05-01 — Day 1: re-audit + diff

The cron tick on 2026-05-01 picks up Jordan's `next_cycle.json` marker
and invokes `run_monthly_cycle()`. The DAG goes:

```
audit_current_state -> diff_against_prior_month -> apply_priority_fixes -> render_and_send
```

**Step 1: audit_current_state** runs the SEO audit again against
`acme-plumbing.example.com`. Snapshot written:

```json
{
  "seo_score": 64,
  "issue_count": 31,
  "rank_by_keyword": {"plumber sacramento": 18, "emergency plumber sacramento": 24, "water heater repair sacramento": 19},
  "gsc_clicks": 0,
  "gsc_impressions": 0,
  "captured_at": "2026-05-01T06:00:00Z"
}
```

(GSC numbers are 0 because we haven't wired GSC ingestion yet — that
lands in a future iteration.)

**Step 2: diff_against_prior_month** runs but finds no prior snapshot
(first cycle). Returns `is_first_cycle: True`, surfaces all 31 issues
sorted by severity into the fix queue.

**Step 3: apply_priority_fixes** doesn't actually fix anything itself
— it dispatches the prioritized fix queue to the operator's fix log
(via `fix_log_fn`). In the default implementation, this just records
"what we said we'd do" so the customer's report isn't empty.

For a real first cycle, the operator (Alex) gets the fix queue + their
6-8 hour budget for the month. From the priority matrix:

- Schema markup (LocalBusiness + Service on 4 pages) — 2 hours
- Broken link audit + repair (Screaming Frog + manual fixes) — 1.5 hours
- Meta description rewrites on top-10 GSC pages — 1.5 hours
- NAP audit + Yelp correction — 0.5 hours
- Image compression on homepage + 5 service pages — 1 hour
- Sitemap regeneration + GSC resubmit — 0.5 hours

Total: 7 hours.

The operator records each shipped fix in the operator-task system so
Step 4 has substance.

## End of week — Day 7: report drafted

**Step 4: render_and_send** runs after the operator marks Week-1 work
done. It pulls this-month's snapshot + the fix log + renders the
Month 1 visibility report. Sent to Jordan:

Subject: "Your SEO Optimization report — 2026-05"

```
# SEO Visibility Report — 2026-05
Site: acme-plumbing.example.com
Cycle: 2026-05 (Month 1 of retainer)

Hi Jordan,

This is your first monthly cycle, so this report is the baseline
we'll measure future months against. Starting next month, the
rank-movement table will show month-over-month deltas + the GSC
delta will be a real comparison.

## Headline metrics
- SEO score: 73/100 (baseline; was 64/100 before this month's fixes)
- GSC clicks: 0 (baseline — GSC pull starts next cycle)
- Open issues at start of cycle: 31

## Fixes applied this month
- technical: Added LocalBusiness + Service schema markup to 4 pages
- technical: Repaired 9 broken internal links from 2024 site reorg
- technical: Compressed 18 images (avg 71% size reduction; LCP 3.4s -> 2.4s)
- content: Rewrote meta descriptions on top-10 impression pages
- local: Corrected stale phone number on Yelp (was tied to a 2022 number)
- technical: Regenerated sitemap.xml + resubmitted to Google Search Console

## Next month's focus
- Push "plumber sacramento" from #18 -> top 10
- Push "emergency plumber sacramento" from #24 -> top 15
- Add FAQ schema to /water-heater-repair/
- Acquire 2-3 local directory backlinks (HomeAdvisor, Angi)

— Morgan, HustleForge
```

## What Jordan sees + feels

Jordan opens the report on Monday morning at the shop. Two things land:

1. **Specific work, specific results.** The "Fixes applied" section lists
   concrete things with numbers (18 images, 71% reduction, 3.4s -> 2.4s).
   This is what makes $300 feel like real money spent well, not a
   subscription bleeding out.

2. **A plan for next month.** The "Next month's focus" lists named
   rank-target moves. Jordan can hold us accountable to those in 30 days.

If Jordan replies with anything — a question, a priority flag, an "add
this keyword" ask — it goes into the operator's task list and shapes
Month 2's priority queue.

## What happens between cycles

Between 2026-05-08 (Month 1 send) and 2026-06-01 (Month 2 cycle), the
operator does NOT work on Jordan's site. The retainer is monthly, not
continuous-engagement. The operator's job between cycles is:

- Triage any reply Jordan sends (response within 24h)
- Monitor for site outages / Google Search Console alerts (best-effort)
- Pre-load Month 2 priorities the day before the cycle runs

This boundary is what makes 6-8 hours/customer/month sustainable across
a retainer book.

## Month 2 preview

When the 2026-06-01 cron fires, the `diff_against_prior_month` step
finds Month 1's snapshot. The report now compares:

- SEO score 73 -> 78 (Month 2 work landed)
- "plumber sacramento" #18 -> #11 (the schema markup did its job)
- GSC clicks: real number from real GSC pull (~340 for the month)
- Fix log: this month's actually-applied changes, not generic queue items

That's the moment the customer feels the retainer is paying off — when
the second report shows movement against a baseline they remember.
