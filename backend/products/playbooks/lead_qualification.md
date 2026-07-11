# Lead Qualification Workflow Playbook

**Audience:** Solo operators and small sales teams handling 20–500 inbound
leads per month who are losing real money to two failure modes:
(1) spending hours on leads who were never going to buy, and
(2) ghosting leads who would have bought if anyone had returned the call
in the first 24 hours.

**What this playbook installs:** A rubric, a routing map, a script bank,
and a weekly review ritual that — together — let one person triage 100+
leads per week without making it personal and without leaving money on
the table.

---

## §1. The problem this solves

There is a specific kind of pain that every operator who's grown past
their first 20 customers will recognize: the inbound lead pile that grows
faster than you can work it, all of them looking *kind of* qualified,
none of them obviously the right "yes" to spend your next hour on.

The result is one of three failure modes, and they all cost money:

1. **The first-come-first-served trap.** You work leads in the order they
   arrive. The first lead of the morning gets 90 minutes; the lead that
   came in at 3pm — who happened to be a much better fit — gets a
   one-line reply and never converts.
2. **The squeaky-wheel trap.** You over-invest in the leads who keep
   emailing because they're easy to identify, and under-invest in the
   leads who needed *one* nudge to convert.
3. **The "everyone's a maybe" trap.** Without an explicit
   disqualification rule, you treat every lead as a 30% probability and
   spread yourself across the whole pile. Real conversion math says
   leads cluster bimodally — either ~70% or ~5%. Treating them all the
   same destroys both buckets.

Lead qualification is the systematic answer. It's a small amount of
discipline that recovers a large amount of revenue.

## §2. The diagnostic — figure out what you actually have

Before you write a rubric, you need to know what your lead pile actually
looks like. Pull the last 90 days of inbounds — by any channel: form
fills, demo requests, cold-email replies, referrals, DMs, the lot.

For each lead, capture five fields. You can do this in a spreadsheet in
under two hours for a typical small-business pile:

| Field | Why |
|---|---|
| Source | Different sources have wildly different conversion math |
| First-touch date | How fast you responded matters more than what you said |
| Closed-won? Y/N | Ground truth |
| Closed-won amount | Lets you compute revenue-per-lead by source |
| Reason for loss (free text) | Surfaces the disqualification rules you should have had |

Now compute three numbers:

1. **Source mix:** What % of your leads come from each source?
2. **Conversion rate by source:** Which sources actually pay?
3. **Time-to-first-response correlation:** Bucket leads by
   <1hr / 1-24hr / >24hr response. Conversion drops cliff-style
   somewhere — usually around the 24hr mark for cold/inbound and
   around the 1hr mark for high-intent (e.g. pricing-page visitors).

These three numbers are your baseline. Every change you make to the
qualification workflow will be measured against them.

## §3. The qualification rubric

The rubric scores every lead on a 0-10 scale across three axes. The
total is your routing key.

### Axis A — Fit (0-4)

Does the lead's *situation* match the customers you've successfully
closed before? Score by checking against a written ICP (ideal customer
profile). A worked example for a $500-$5k B2B service offer:

| Signal | Points |
|---|---|
| Company size in your sweet spot (e.g. 5-50 employees) | +2 |
| Industry on your worked-before list | +1 |
| Geography in a timezone you can serve | +1 |
| Obvious red flag (e.g. competitor, student, "looking for free advice") | -3 (cap at 0) |

### Axis B — Intent (0-3)

How strong is the *behavioral* signal that they're actually in market?

| Signal | Points |
|---|---|
| Specific question about your offering ("can you do X for Y?") | +2 |
| Pricing-page visit before form fill | +1 |
| Referral from a customer (named referrer) | +2 (caps Axis B at 3) |
| Vague question ("tell me more about what you do") | +0 |
| Mass-form-fill / boilerplate ("Sounds interesting!") | +0 |

### Axis C — Authority (0-3)

Can the person on the form actually buy?

| Signal | Points |
|---|---|
| Founder / owner / VP-or-above title | +3 |
| Stated budget or "we have budget for this" | +2 |
| "Just gathering info for my boss" | +1 |
| No name / generic email (sales@, info@) | +0 |

**The routing buckets:**

| Total score | Bucket | What happens |
|---|---|---|
| 8-10 | **Hot** | Personal call within 1 hour, calendar link in first reply |
| 5-7 | **Warm** | Personal call within 24 hours, qualifier email immediately |
| 2-4 | **Cold** | Auto-reply with self-serve resource + 30-day nurture sequence |
| 0-1 | **Disqualify** | Polite "this isn't a fit" auto-reply with a referral |

## §4. The disqualification script

The single most underused tool in lead qualification is the polite "no."
A clean disqualification message protects two things: your time, and the
prospect's relationship to your brand. Done well, a disqualified prospect
referring you in 18 months is worth more than them dragging out 4 calls
that close nothing.

A template that works:

> Hi [name],
>
> Thanks for reaching out about [their topic]. To be straight with you:
> based on [the specific reason: company size, geography, what they're
> trying to do, etc.], we're not the right fit for this engagement. We've
> seen the [specific thing they need] pattern best handled by [referral —
> a competitor, an open-source tool, a freelancer network, etc.].
>
> If your situation changes — for example if you [specific trigger
> condition that would make them qualified] — please loop back. Genuinely
> happy to help when that day comes.
>
> [Your name]

Two non-obvious rules:

1. **Be specific about the reason.** A vague "we're not a fit" reads as
   "you weren't interesting enough." A specific reason (e.g. "we only
   work with teams of 5+ engineers") reads as professional discipline
   and is *more likely* to generate a referral.
2. **Always offer a referral.** Even if the referral is "this open-source
   project does roughly what we do for free," the gesture earns you long-
   term goodwill. The cost is one sentence; the upside is years.

## §5. Step-by-step workflow

The day-to-day workflow is five steps. The first three happen
automatically (Zapier / Make / native form integration). The last two
are the human work.

```
[1] Form submission / inbound email arrives
    └─> Webhook → CRM creates Lead record

[2] Auto-scoring rule fires on Lead create
    └─> Score = sum(fit, intent, authority)
    └─> Bucket = Hot / Warm / Cold / Disqualify
    └─> Routing assignment based on bucket

[3] Auto-reply sent based on bucket
    ├─ Hot: "I'll call you within the hour" + calendar link
    ├─ Warm: Qualifier email with 3 questions, calendar link
    ├─ Cold: Self-serve resource, "we'll be in touch within a week"
    └─ Disqualify: Polite no + referral (per script above)

[4] Human reviews Hot+Warm leads within SLA
    └─> Personal call attempt
    └─> Outcome logged: connected / voicemail / no-answer

[5] Friday review (30 min): pipeline + scoring calibration
    └─> Review every Hot+Warm from the past week
    └─> Were the scores right? Adjust the rubric
    └─> Were the disqualifications correct? Adjust the rules
```

Two SLAs that aren't negotiable:

- **Hot leads: 1 hour to first human reply.** This is the single highest-
  leverage discipline in the entire playbook. The data is unambiguous —
  the conversion rate of a Hot lead replied to in <1hr is 5-10x the
  conversion rate of the same lead replied to in 24hrs.
- **All leads: some kind of reply within 24 hours.** Even the auto-reply
  counts. Silence is the highest-cost outcome.

## §6. Tools & templates

You don't need any special tool to run this playbook. A list of what
works for a solo operator with no dedicated stack:

| Function | Lightweight option | Pro-grade option |
|---|---|---|
| Lead capture | Google Form → Sheet | HubSpot / Pipedrive forms |
| Auto-scoring | Sheet formula triggered on row-add | CRM workflow with scoring rule |
| Routing | Zapier branch by score | CRM workflow with assignment rule |
| Auto-reply | Gmail filter + canned response | Marketing automation tool |
| Calendar booking | Cal.com / Calendly | Native CRM scheduler |
| Disqualify queue | Separate sheet tab | CRM segment + suppression list |

The lightweight column total cost: free.
The pro-grade column total cost: $20-$80/month/user.

Start lightweight. Upgrade only when one specific friction point in
the lightweight stack is provably costing you a deal a month.

## §7. Metrics & KPIs

Track four numbers, weekly:

1. **Lead → Hot conversion rate.** % of inbound that scores 8+.
   If this is below 10%, your form is too broad; tighten it.
   If it's above 40%, your scoring is too generous; sharpen it.
2. **Hot lead first-response time (median).** Target: under 60 minutes
   business hours. Anything over 2 hours is a process failure.
3. **Hot → Customer conversion rate.** Industry-typical: 20-40% for
   warm-inbound B2B; 5-15% for paid-traffic-driven B2C. Set your own
   baseline from your 90-day diagnostic and track movement.
4. **Disqualify rate.** % of leads explicitly disqualified.
   Healthy: 20-40%. Less than 10% means you're working leads that
   shouldn't be in your pipeline.

The single chart that matters: weekly Hot-conversion-rate, with a
horizontal target line. If the line trends down for 4 weeks, the
rubric needs a tune-up.

## §8. The 30/60/90-day rollout

### Days 1–30: Install the basics

- Run the 90-day diagnostic (§2). You'll spend ~3 hours of one
  Saturday on this and it will pay back forever.
- Write your ICP — one page, bullet-form, plus three counter-examples
  (the kind of lead you do NOT want).
- Build the rubric in a spreadsheet (you can promote to CRM-native
  later). Every new lead gets manually scored for the first 30 days
  so you can calibrate before automating.
- Stand up the disqualification script. Use it. Track how many
  disqualifies turn into thank-you replies — that's your goodwill
  meter.

### Days 31–60: Automate the boring parts

- Migrate the manual scoring into a CRM workflow (or sheet formula).
- Set up the auto-reply chain by bucket.
- Connect a calendar booking link to your Hot-bucket auto-reply.
- Start the weekly 30-min review on Fridays. Don't skip it.

### Days 61–90: Tune and harden

- Compute your current Hot-conversion-rate. Compare it to the 30-day
  baseline. The improvement is your ROI.
- Review every disqualification from the past 60 days. Were any of them
  wrong? Did any of them come back as customers? Tighten the rule that
  was wrong.
- Add one source-specific rule based on what you learned. Example: if
  leads from "Twitter DMs" convert at 35% but score low on the generic
  rubric, give them a +2 source bonus.

By day 90 you should be triaging 100 leads/week in under 4 hours of
human time, with a defensible reason behind every Hot, every Cold, and
every Disqualify. That's the deliverable.

## §9. Common failure modes

Three patterns we see go wrong repeatedly:

1. **Scoring the lead but not acting on the score.** A rubric without
   SLAs is performance art. The whole point is to make Hot leads
   trigger a specific action *in a specific window*. If you score
   without enforcing, the discipline dies in 30 days.
2. **Disqualifying too aggressively, too fast.** If you cut your rubric
   so tight that you disqualify 60% of leads in month one, you'll
   panic and abandon the playbook. Start with the rubric we gave you,
   tune *down* the threshold if you have to, but don't tune so aggressively
   that you starve your own pipeline.
3. **Not reviewing.** Without the Friday 30-minute review, the rubric
   ossifies and stops matching reality. Lead patterns shift quarterly;
   the rubric needs to shift with them.

---

## §10. What's NOT in this playbook

Three things this playbook intentionally does not cover. Each is its
own engagement:

- **Lead generation itself.** This playbook assumes you have leads
  coming in. It does not teach you how to drive volume.
- **Sales pitch / close mechanics.** Once a Hot lead is on your
  calendar, what you say is out of scope here. (See the Sales Follow-Up
  Workflow Playbook for the cadence between meetings.)
- **CRM-tool selection.** We give you the framework; we don't pick
  HubSpot vs. Pipedrive vs. Notion for you.

---

*One round of clarifying Q&A is included with this playbook. Email back
with anything that's unclear or that doesn't map cleanly to your
specific stack.*
