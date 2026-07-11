# Notion CRM Skeleton — 6 databases

A simple-but-real CRM that lives entirely in Notion. Six linked
databases, ~30 min to build.

## Why Notion (vs. a real CRM)

For the first ~100 contacts, a real CRM is overkill. You spend more
time configuring it than using it. Notion lets you:

- Customize fields to your specific workflow
- Link contacts, companies, deals, and notes in a graph
- Filter views without writing queries
- Stay in the same tool you already write in

You'll outgrow Notion at around 200-500 active contacts. By then you'll
know exactly what you need from a real CRM.

## Database 1 — Contacts

| Property | Type | Notes |
|---|---|---|
| Name | Title | First + Last |
| Email | Email | |
| Phone | Phone | |
| Company | Relation → Companies | One per contact |
| Role | Text | |
| Source | Select | Inbound / Outbound / Referral / Event / Other |
| Stage | Select | Cold / Warm / Hot / Customer / Past |
| First contact date | Date | |
| Last touch date | Date | Auto-rollup from Touch Log |
| Notes | Relation → Notes | Many per contact |
| Deals | Relation → Deals | Many per contact |
| Tags | Multi-select | Free-form labels |

## Database 2 — Companies

| Property | Type | Notes |
|---|---|---|
| Name | Title | |
| Website | URL | |
| Industry | Select | |
| Size | Select | Solo / 2-10 / 11-50 / 51-200 / 200+ |
| Contacts | Relation → Contacts | Many per company |
| Deals | Relation → Deals | Many per company |
| Notes | Relation → Notes | Many per company |
| Status | Select | Prospect / Active / Inactive / Churned |

## Database 3 — Deals

| Property | Type | Notes |
|---|---|---|
| Name | Title | "Acme — Q2 SEO retainer" |
| Company | Relation → Companies | One per deal |
| Primary contact | Relation → Contacts | One per deal |
| Stage | Select | Quoted / Discussing / Verbal / Signed / Lost |
| Value (one-time) | Number ($) | |
| Value (recurring/mo) | Number ($) | |
| Expected close date | Date | |
| Actual close date | Date | |
| Lost reason | Text | Free-form, fill in if Lost |
| Notes | Relation → Notes | Many per deal |
| Tasks | Relation → Tasks | Many per deal |

## Database 4 — Tasks

| Property | Type | Notes |
|---|---|---|
| Task | Title | "Send Touch 3 to Sarah at Acme" |
| Due date | Date | |
| Status | Select | To do / Doing / Done / Snoozed |
| Related deal | Relation → Deals | Optional |
| Related contact | Relation → Contacts | Optional |
| Priority | Select | P0 / P1 / P2 |

## Database 5 — Notes

| Property | Type | Notes |
|---|---|---|
| Title | Title | "Call with Sarah, 2026-05-15" |
| Date | Date | |
| Type | Select | Call / Email / Meeting / Internal / Research |
| Related contact | Relation → Contacts | Optional |
| Related company | Relation → Companies | Optional |
| Related deal | Relation → Deals | Optional |
| Body | Text (long) | Free-form notes |

## Database 6 — Touch Log

| Property | Type | Notes |
|---|---|---|
| Touch | Title | "Touch 2 — value-add nudge" |
| Date | Date | |
| Contact | Relation → Contacts | One per touch |
| Channel | Select | Email / Phone / VM / LinkedIn / SMS / In-person |
| Direction | Select | Outbound / Inbound |
| Reply received? | Checkbox | |
| Related deal | Relation → Deals | Optional |

## The 4 saved views you'll actually use

Build these once, use them daily:

1. **Today's tasks** — Tasks filtered by Due date = Today, sorted by
   Priority.
2. **Hot deals** — Deals filtered by Stage = Quoted | Discussing |
   Verbal, sorted by Expected close date.
3. **Stale contacts** — Contacts filtered by Last touch > 30 days ago
   AND Stage != Past. Your re-engagement pile.
4. **This week's outreach** — Touch Log filtered by Date >= this week,
   grouped by Channel. Your "did I actually follow up?" check.

## How to wire the rollups

The "Last touch date" rollup on Contacts:

- Property type: Rollup
- Source: Touch Log relation
- Property: Date
- Function: Latest date

This is the one rollup that earns its keep. Everything else can be
manual.

## The weekly maintenance

15 minutes every Friday:

1. Mark all completed Tasks as Done.
2. Snooze any task you didn't get to (and pick a new due date).
3. Update any Deal that moved stages.
4. For any Contact in "Hot" stage who hasn't had a touch in 5 days,
   add a Task for next week.

That's it. Notion CRMs die not from missing features but from missing
maintenance.
