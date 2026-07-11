# Client Onboarding Workflow Playbook

**Audience:** Operators and services-business owners closing 2-20 new
clients per month who are losing time, goodwill, and renewal revenue
because the gap between "contract signed" and "first value delivered"
is too long, too messy, and too inconsistent.

**What this playbook installs:** A repeatable onboarding flow that takes
a freshly-signed client from welcome email to first delivered milestone
in 7 calendar days or less, without making it feel rushed and without
making you sound like a SaaS company that forgot they have humans on the
other end.

---

## §1. The problem this solves

Onboarding is where 70% of the trust you built during the sale either
compounds or evaporates. The mechanics of how this happens are simple:

- A client signs because they were sold on a *future state* — the
  outcome they want.
- The longer the gap between signing and a first concrete deliverable,
  the more the client's certainty about that future state decays.
- Decayed certainty looks like: late payments, scope creep, "let me
  check with my partner before we move forward," and — worst of all — a
  silent decision to not renew because the energy of the sale never got
  re-ignited.

The cure is structural. Onboarding has to be designed so that within
the first 7 days, the client has experienced at minimum:

1. A real human acknowledging them by name and confirming what they
   bought.
2. Visibility into what happens next, who's responsible, and when.
3. At least one tangible deliverable — even if it's small.
4. A scheduled future moment to look forward to (the kickoff, the
   milestone, the review).

This playbook builds all four into a workflow that runs the same way
every time, whether you're onboarding your 3rd client or your 30th.

## §2. The diagnostic — figure out what your current onboarding actually does

Before you redesign, audit what's happening today. Pull your last 10
closed clients and answer these questions for each:

| Question | Capture |
|---|---|
| Days between contract signed and first welcome message | Number |
| Days between welcome and first deliverable | Number |
| Did they pay the first invoice on or before the due date? | Y/N |
| Did they ask "what's next?" before you told them? | Y/N |
| Did they renew? (or are they likely to?) | Y/N/TBD |

Compute the medians, then compute a correlation: clients who paid on
time, didn't have to ask "what's next?", and got a deliverable inside
7 days renew at dramatically higher rates than clients who experienced
silence + delay. This is the lever you're about to pull.

## §3. The onboarding workflow — 5 stages, 7 days, 1 spreadsheet

The whole thing fits on one page. Five stages, each with a defined
trigger, owner, action, and acceptance criterion.

### Stage 1 — Welcome (within 1 hour of contract signature)

**Trigger:** Contract signed (DocuSign / PandaDoc / your e-sign tool
fires a webhook).

**Owner:** You (or your highest-touch person).

**Action:**

1. Personal email from a human inbox — no "Hello @{first_name}" auto-
   template, no marketing wrapper. Just a real person saying:
   - Thank you, by name.
   - Confirm what they bought, by name.
   - One sentence about what happens next.
   - A scheduled future moment ("I'll send you the access checklist
     within 24 hours, and we'll kickoff on [specific date/time]").
2. Add the client to the active-clients tracker (a sheet tab or CRM
   pipeline stage).

**Acceptance criterion:** Email sent inside 1 hour. The client should
feel they bought from a human who is awake and paying attention.

### Stage 2 — Access & expectation setup (within 24 hours)

**Trigger:** 24 hours since Stage 1.

**Owner:** You.

**Action:** Send the **Access Checklist + Kickoff Prep** email
(template in §6). This contains:

- Exactly what you need from the client (logins, brand assets,
  questionnaire, contact for billing).
- A read-only project timeline showing milestones.
- The kickoff call calendar invite (already on their calendar at this
  point — you put it there in Stage 1).
- A "what to expect in week 1" one-paragraph summary.

**Acceptance criterion:** Client has acknowledged receipt and started
returning items. Track in your sheet.

### Stage 3 — Kickoff call (Day 3-5)

**Trigger:** Pre-scheduled calendar event.

**Owner:** You.

**Action:** 30-45 minute call with a written agenda (template in §6).
Three things must happen:

1. **Restate the outcome.** Get the client to articulate, in their own
   words, what success looks like at the 30 / 60 / 90 day mark. Capture
   this verbatim — it's your renewal-conversation ammunition.
2. **Walk the timeline.** Show them the milestone calendar. Confirm
   no scheduling conflicts.
3. **Identify the unknowns.** What's NOT clear? What information do
   you still need? Anything that comes out here is a stage-2-checklist
   addition for next time.

**Acceptance criterion:** Client leaves the call able to (a) name their
own outcome, (b) name the next milestone, and (c) name the person they
contact when stuck.

### Stage 4 — First deliverable (Day 5-7)

**Trigger:** Kickoff complete + access materials received.

**Owner:** You (or the delivery team if you have one).

**Action:** Ship *one* tangible deliverable to the client. This is the
single most under-prioritized step in the whole playbook. The
deliverable does NOT need to be the "main" deliverable — it needs to
be **visible, on-time, and proof that the engine is running**.

Examples that work for various service offers:

- For SEO: the audit findings report (even if implementation hasn't
  started).
- For workflow consulting: the current-state process map.
- For coaching: the 90-day learning plan document.
- For automation builds: a screenshot of the staging environment with
  one happy-path scenario working.

**Acceptance criterion:** Deliverable in the client's inbox by Day 7,
with a one-sentence "what's next" note attached.

### Stage 5 — Week-2 check-in (Day 10-14)

**Trigger:** 7 days after the first deliverable shipped.

**Owner:** You.

**Action:** A short (15-min or async) check-in. Two things:

1. "How did the first deliverable land?" — explicitly ask for criticism.
2. Restate the upcoming milestones and confirm we're tracking.

**Acceptance criterion:** Client has voiced any concerns *before* they
fester into "we need to talk" emails three weeks later.

## §4. The "next-30-days" nurture sequence

After the 14-day formal onboarding ends, the client enters a low-touch
nurture that maintains the relationship without burning your time. Five
touches over the next 30 days:

| Day | Touch | What's in it |
|---|---|---|
| 17 | Async progress update | Bullet list of what's happened, what's coming |
| 22 | "Small win" share | One specific data point, screenshot, or quote |
| 27 | Resource share | An article, tool, or insight relevant to their goal |
| 32 | Milestone-1 delivery | Whatever the 30-day deliverable contractually is |
| 37 | Review call | 30-min retrospective on the first month, set the next 30 |

This is *also* a template — same one every client gets, lightly
customized. The discipline is showing up on schedule, not the
creativity of each touch.

## §5. The stakeholder one-pager

Most service engagements involve at least one stakeholder besides the
person who signed: a business partner, a finance person, a "champion
at the executive level," or a team that will use the deliverable but
wasn't on the sales call.

Build a one-page document — half spec, half status report — that the
signing client can forward to anyone without explanation. It should
contain:

1. **What we're doing for [client company]** — 2-sentence summary.
2. **Why it matters** — 2-sentence business impact.
3. **Timeline** — milestones with dates.
4. **Who's involved** — names + roles on both sides.
5. **What's needed from your team** — 3-5 bullets.
6. **Current status** — RAG (red/amber/green) per workstream, dated.

Send the v1 in Stage 2. Update it after every milestone. The forwardable
artifact buys you political cover with the client's organization without
needing a single internal meeting.

## §6. Tools & templates

### Template: Welcome email (Stage 1)

```
Subject: Welcome to [your company] — [client first name]

Hey [name],

Thanks for trusting us with [the specific work]. We're excited to get
started.

Quick orientation:

  1. I'll send you a short access checklist within the next 24 hours —
     things like logins, brand assets, and a quick questionnaire so we
     hit the ground running.
  2. Our kickoff call is on [day, date, time, timezone] — calendar
     invite is on its way. Plan for ~45 minutes.
  3. By [date, 7 days from now], you'll have our first deliverable in
     hand. We move fast on purpose.

Reply to this email if you need anything before then. I read every
message myself.

— [Your name]
[Your direct phone number]
```

### Template: Access checklist (Stage 2)

```
Subject: [Client company] kickoff — quick prep

Hey [name],

Two things to get rolling. Both take ~15 minutes total:

ACCESS WE NEED:
  □ [Specific login or access item #1]
  □ [Specific login or access item #2]
  □ [Specific access item #3]
  → Easiest way: reply with creds or use [your password-sharing tool]

CONTEXT WE NEED:
  □ Brand assets (logo, colors, fonts) — link or attachments
  □ Billing contact (name + email) for invoicing
  □ The attached 5-minute questionnaire

Your project timeline:
  → Day 0  (today): you sign + we send this email
  → Day 3-5: kickoff call (already on your calendar)
  → Day 7: first deliverable in your inbox
  → Day 14: first check-in
  → Day 30: milestone 1
  → Day 90: full retrospective + next phase planning

Anything missing or unclear, just reply.

— [Your name]
```

### Template: Kickoff agenda (Stage 3)

```
[Client company] × [Your company] — Kickoff Call

Attendees: [names]
Date: [date]
Duration: 45 min

Agenda:

  1. Quick recap of what we're building together (5 min)
  2. What does success look like at 30 / 60 / 90 days?
     → [client articulates in their own words] (10 min)
  3. Walk the timeline + milestones (10 min)
  4. Risks, unknowns, and where you'd expect to get stuck (10 min)
  5. Confirm communication cadence (5 min)
  6. Open Q&A (5 min)

Outcomes to leave with:
  - Shared understanding of "done"
  - Confirmed milestone calendar
  - Agreed comm cadence (where, how often)
  - List of unknowns to chase down this week
```

## §7. Metrics & KPIs

Track four numbers, monthly:

1. **Median days to first deliverable.** Target: 7.
   If it creeps above 10, your access checklist is too vague.
2. **Median days to first invoice paid.** Target: 5.
   Late first payments correlate strongly with future churn.
3. **First-month "are we on track?" sentiment.** Ask the client
   verbatim at the Day-14 check-in: "Honest 1-10, are we on track from
   your side?" Track the average.
4. **Stakeholder one-pager forward count.** If the client is forwarding
   it, you have champion-energy. Ask at the Day-37 review: "Has anyone
   on your team asked about us? Have you shared the status doc?"

The single chart that matters: 90-day churn rate. The whole point of
this playbook is that it should be near-zero.

## §8. The 30/60/90-day rollout (for *your* business, installing this)

### Days 1–30: Audit + design

- Run the diagnostic (§2) on your last 10 closed clients.
- Write your version of the 5 templates in §6.
- Build the active-clients tracker (a sheet tab is fine to start).
- Run the new onboarding manually for your next 2 clients.

### Days 31–60: Automate the predictable touches

- Wire up the Stage-1 trigger (e-sign webhook → email send).
- Build the Stage-2 access checklist as a templated email with an
  auto-attached prep questionnaire.
- Move the active-clients tracker into your CRM (or keep the sheet,
  whatever has lower friction for you).

### Days 61–90: Tune + harden

- Compute your new median-days-to-first-deliverable. Compare to the
  baseline. Celebrate.
- Run a retrospective with one client from the new cohort. What felt
  good? What felt mechanical? Adjust.
- Add the stakeholder one-pager into your default Stage-2 send.

## §9. Common failure modes

Three patterns that destroy onboardings:

1. **Treating the welcome email as marketing.** A SaaS-style welcome
   email with brand colors and an unsubscribe link tells the new client
   that they've been moved from "prospect" to "list member." This is
   exactly backwards. The welcome must read like a human turned
   around in their chair to greet them.
2. **No first deliverable in week 1.** Even when you're contractually
   not obligated to ship anything until day 30, ship *something* in
   week 1 — the audit, the plan doc, the screenshot of the staging
   environment. Visibility = trust.
3. **Skipping the Day-14 check-in because "things are going well."**
   This is when you find out things aren't going well. If you skip it,
   the first signal you'll get that something's off is when the
   renewal conversation goes sideways three months later.

---

## §10. What's NOT in this playbook

- **Contracts & MSA templates.** Out of scope; talk to a lawyer.
- **Pricing & packaging design.** Different engagement — this assumes
  you've already sold a known package.
- **Client portal software selection.** This playbook works with a
  spreadsheet or a $300/month tool; we don't pick for you.

---

*One round of clarifying Q&A is included with this playbook. Email back
with anything that's unclear or that doesn't map cleanly to your
specific delivery model.*
