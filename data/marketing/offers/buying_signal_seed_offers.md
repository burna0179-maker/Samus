# Buying-Signal Warm Follow-Up — Seed Offer Templates

_Seed/template email copy for the `buying_signal` warm follow-up sequence
(`backend/outreach/sequences.py` → `BUYING_SIGNAL_SEQUENCE`). A prospect lands
here when a voice call (or a warm email reply) surfaces real intent. These are
**drafts for review** — sending is dormant until a composer is wired
(`dispatch_due_buying_signal` is fail-closed without one)._

**Status:** drafts, reviewed 2026-06-30. Sign-off = "The HustleForge team".
Touch-3 urgency = soft (permission-to-close, no fake scarcity).

## Brand voice guardrails (from `briefs/hustleforge/brand_brief.md`)
- Direct, practical, systems-focused, plain-language.
- Only cite dollar amounts that are on the public site ($149, $500, $2,500–3,000 are fine).
- **Don't say:** internal agent/infra names, specific dates, "we run this on ourselves",
  hype words (game-changing, set-and-forget, no-oversight, revolutionary).
- **Do say:** workflow systems, connect what you already use, measurable outcomes, real execution,
  defined outcome (not an open-ended retainer).

## Per-prospect customization tokens
| Token | Source | Fallback |
|---|---|---|
| `{{first_name}}` | call contact / CRM | "there" |
| `{{company_name}}` | enrollment record | "your business" |
| `{{pain_point}}` | call `lead_summary.pain_points` (what they actually named) | a generic line for the offer |
| `{{offer_name}}` / `{{checkout_link}}` / price | validated SKU from the call → `backend/catalog/registry.py` | default = SEO Audit ($149) |
| `{{scoping_cta}}` | for quote-based offers (no checkout link) → renders the booking line below | "reply with a couple of times that work" |
| `{{booking_link}}` | Google Calendar appointment-schedule link (PENDING setup) | — |

Each touch maps to a sequence template id: `buying_signal_recap` (day 0),
`buying_signal_nudge` (day 1), `buying_signal_hold` (day 3).

---

## Offer 1 — SEO Audit ($149, one-time)
- SKU `seo_audit` · Stripe `price_1TKtvJBdo4u4gpS7PJf01xN` · LIVE
- Checkout: https://buy.stripe.com/6oU00i3Sq1ay1dId5W8so0d
- Deliverable: full technical SEO audit (Core Web Vitals, keyword roadmap, prioritized fix list), written report in 24h.

### Touch 1 — Recap (day 0) · `buying_signal_recap`
**Subject:** `{{company_name}} — here's that audit link`
```
Hi {{first_name}},

Good talking just now. As promised, here's the link to get your SEO Audit started:

{{checkout_link}}

Quick why-it-matters for {{company_name}}: you mentioned {{pain_point}}. The audit
pinpoints exactly what's holding the site back — the technical issues, Core Web
Vitals, and the keyword gaps competitors are ranking on — and you get a written
report with a prioritized fix list in 24 hours. $149, one time, no retainer.

On the checkout page, drop your site URL — that's the only thing the team needs
to know which site to look at.

— The HustleForge team
```

### Touch 2 — Nudge (day 1) · `buying_signal_nudge`
**Subject:** `One question on the audit, {{first_name}}?`
```
Hi {{first_name}},

Quick one on the SEO Audit link from yesterday — I want to make sure nothing's
in your way.

If there's a single question holding you back, reply here and I'll answer it
straight. Otherwise the link's still good:

{{checkout_link}}

— The HustleForge team
```

### Touch 3 — Hold / permission-to-close (day 3) · `buying_signal_hold`
**Subject:** `Should I close this out, {{first_name}}?`
```
Hi {{first_name}},

Last note on this one. If you want the audit done this week, here's the link —
$149, one report, 24-hour turnaround:

{{checkout_link}}

If now's not the time, no problem at all — just reply "later" and I'll close it
out so I'm not crowding your inbox.

— The HustleForge team
```

---

## Offer 2 — 48-Hour Workflow Rescue ($500, one-time)
- SKU `service_workflow_rescue` · Stripe `price_1TAj1nBdo4u4gpS7Z4fGaYT0` · LIVE
- Checkout: https://buy.stripe.com/cNi14m74C1ay9Ke0ja8so0c
- Deliverable: one workflow scoped, built, and handed off running inside 48 hours.

### Touch 1 — Recap (day 0) · `buying_signal_recap`
**Subject:** `{{company_name}} — your Workflow Rescue link`
```
Hi {{first_name}},

Good talking just now. As promised — here's the link to kick off your 48-Hour
Workflow Rescue:

{{checkout_link}}

From what you said, the workflow eating your week is {{pain_point}}. We scope it,
build it, and hand it back running inside 48 hours — $500, one time. On checkout,
describe that workflow in a line or two; that's what the build starts from.

— The HustleForge team
```

### Touch 2 — Nudge (day 1) · `buying_signal_nudge`
**Subject:** `One question on the Workflow Rescue, {{first_name}}?`
```
Hi {{first_name}},

Quick one on the Workflow Rescue link from yesterday — want to make sure nothing's
in your way.

If there's a single question holding you back, reply here and I'll answer it
straight. Otherwise the link's still good:

{{checkout_link}}

— The HustleForge team
```

### Touch 3 — Hold / permission-to-close (day 3) · `buying_signal_hold`
**Subject:** `Should I close this out, {{first_name}}?`
```
Hi {{first_name}},

Last note on the Workflow Rescue. If that workflow is still costing you the week,
this is the quick way to hand it off — $500, scoped, built, and running in 48 hours:

{{checkout_link}}

If now's not the time, no problem at all — just reply "later" and I'll close it
out so I'm not crowding your inbox.

— The HustleForge team
```

---

## Offer 3 — Workflow System Buildout ($2,500–3,000, scoped)
- SKU `service_workflow_buildout` · Stripe `price_1TAj1pBdo4u4gpS7JBbZ5SWJ` · LIVE
- **No self-serve checkout link** — quote-based; CTA drives to a scoping call.
- `{{scoping_cta}}` renders the booking line (Google Calendar appointment schedule link, setup pending).
- Deliverable: connect fragmented systems into one execution layer; scoped, defined outcome.

### Touch 1 — Recap (day 0) · `buying_signal_recap`
**Subject:** `{{company_name}} — next step on the Workflow System Buildout`
```
Hi {{first_name}},

Good talking just now. As promised, here's the next step on the Workflow System
Buildout.

From what you described, the real issue is {{pain_point}} — work breaking at the
handoffs between tools that don't talk to each other. The Buildout connects what
you already use into one execution layer, so those steps run without someone
pushing them along. It's scoped to your systems — typically $2,500–3,000 — with
a defined outcome, not an open-ended retainer.

The next step is a short scoping call: we map your tools and the handoffs costing
you the most, and you leave with a scoped plan whether or not you move forward.
{{scoping_cta}}

— The HustleForge team
```

### Touch 2 — Nudge (day 1) · `buying_signal_nudge`
**Subject:** `One question on the Buildout, {{first_name}}?`
```
Hi {{first_name}},

Quick one on the Workflow System Buildout from yesterday. The scoping call is just
to map your systems — no prep on your end, and you'll leave knowing exactly what
we'd connect first and what it would cost.

If there's a question in the way, reply here and I'll answer it straight.
Otherwise: {{scoping_cta}}

— The HustleForge team
```

### Touch 3 — Hold / permission-to-close (day 3) · `buying_signal_hold`
**Subject:** `Should I close this out, {{first_name}}?`
```
Hi {{first_name}},

Last note on the Buildout. If connecting your systems is still worth doing, the
scoping call is the easy first step — about 20 minutes, and you walk away with a
scoped plan either way.

{{scoping_cta}}

If the timing's off, no problem at all — just reply "later" and I'll close it out
so I'm not crowding your inbox.

— The HustleForge team
```

---

## Wiring notes (for when sending is armed)
- These become the source for the `composer(message, record) -> (subject, body)`
  passed to `dispatch_due_buying_signal(dry_run=False, composer=...)`. The composer
  selects the offer by the enrollment's validated SKU (default SEO Audit), fills
  the tokens from the enrollment record + call `lead_summary`, and returns the
  send-ready subject/body for the touch's `template_id`.
- `{{scoping_cta}}` / `{{booking_link}}` resolve once the Google Calendar
  appointment schedule is set up (booking page on the operator's calendar; a Samus
  watcher will fire the sequence `meeting_booked` branch on booking).
- Pricing mirrors LIVE Stripe (see `backend/catalog/stripe_sync_report.md` for
  spec-vs-live discrepancies). Only amounts on the public site are quoted in copy.
