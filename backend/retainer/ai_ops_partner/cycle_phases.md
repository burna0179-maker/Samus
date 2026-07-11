# AI Ops Partner — Monthly Cycle Phases

The retainer runs on a fixed 4-week cadence. Every cycle goes through
the same four phases in order. Customers see the rhythm; operators
work the rhythm; the report at the end of Week 4 closes the loop.

## Why a fixed cadence vs. "always on"

Customers buying ops partnership want **predictability**, not constant
availability. Always-on positions us as a fire-fighting service (bad
ground) — fixed-cadence positions us as a planned-execution partner
(good ground). The cadence also caps the operator's emotional surface
area: between cycles you're not constantly waiting for a Slack ping.

If the customer has a genuine emergency between cycles, that's a
separate conversation about whether they need a tier with on-call
included — not a quiet expansion of scope inside the retainer.

---

## Week 1 — Assess

**Goal:** Establish the truth-on-the-ground for this cycle. What
shipped from last month? What changed in the business? What's the
customer worried about right now?

### Activities

| When | Who | What | Output |
|---|---|---|---|
| Mon AM | operator | Pull prior-month metrics snapshot (autobuilt by monthly_cycle.py) | `snapshot.json` baseline |
| Mon AM | operator | Review last cycle's `next_month_focus` list | Carry-over items list |
| Mon PM | operator | Pre-call brief — what's worked, what hasn't, candidate priorities | Pre-call Notion doc |
| Tue / Wed | operator + customer | **30-minute Week 1 check-in call** (recorded if customer consents) | Call notes + transcribed action items |
| Thu | operator | Update CRM with new lead/customer changes since last cycle | CRM rows current |
| Fri | operator | Draft Week 2 prioritization options based on call + metrics | 3 candidate scopes |

### Deliverable

**A written Week-1 brief** sent to the customer via email by EOD Friday:

> "Here's where things stand at the start of cycle <month>. From our call
> Tuesday, the top three candidate priorities for this month's build are
> (1) X, (2) Y, (3) Z. I'll come back Monday with a recommendation."

### Definition of done

- Prior-month metrics snapshot loaded into the cycle
- Customer check-in call happened + notes captured
- 3 candidate priorities articulated in writing
- Carry-over backlog reviewed

---

## Week 2 — Prioritize

**Goal:** Lock the scope for this month's build. Choose one or two
deliverables that fit the time budget (~24-32 operator hours/month
depending on tier) and that the customer commits to using.

### Activities

| When | Who | What | Output |
|---|---|---|---|
| Mon | operator | Recommend one primary + one stretch deliverable to customer | Recommendation email |
| Tue / Wed | operator + customer | **20-minute Week 2 scope-lock call** (optional — skip if email exchange is sufficient) | Scope-locked decision |
| Wed PM | operator | Write the build spec — inputs, outputs, success metric, integration touchpoints | Build spec doc |
| Thu | operator | Customer signs off on spec (reply-with-OK is enough) | Sign-off |
| Fri | operator | Stand up the dev environment / branch / tracking | Dev branch ready |

### Deliverable

**A locked build spec** with explicit success criteria:

> "This month's build is the trade-in valuation auto-router. Inputs:
> form submissions to /trade-in/. Outputs: routed lead in the sales
> coordinator's queue within 5 minutes. Success metric: 70%+ of
> submissions auto-route without human touch. Stretch: 85%+."

### Definition of done

- Customer has agreed in writing to this month's scope
- Success metric is measurable + measurable BEFORE end of cycle
- Build spec is written down (operator does not work from memory)
- Dev environment is provisioned

### Things we DON'T do in Week 2

- Start building (that's Week 3). Don't bleed Week-3 hours into Week 2
  out of restlessness; the discipline is what keeps us from over-promising.
- Argue with the customer about the recommendation. If they want X and
  we recommended Y, build X. Disagreement gets logged in operator notes
  for next cycle's retro.

---

## Week 3 — Build & deploy

**Goal:** Ship the locked scope. This is the operator's heads-down week.

### Activities

| When | Who | What | Output |
|---|---|---|---|
| Mon - Wed | operator | Build the deliverable end-to-end | Working integration in dev |
| Thu AM | operator | Customer pre-deploy demo (15 min Loom or live call) | Customer go-ahead |
| Thu PM | operator | Production deploy + post-deploy smoke test | Live in prod |
| Fri | operator | Customer-facing documentation (what it does, how to use it, where to find logs) | Customer docs |

### Deliverable

**The build, deployed, with the customer trained on how to use it.**
A built thing that the customer doesn't know how to operate isn't a
delivered thing. Training can be a Loom + a 1-page doc; it doesn't
need to be elaborate.

### Definition of done

- Deployed to production
- Customer can demonstrate using it (or the workflow runs without their
  intervention)
- Success metric is being measured
- Operator-task in the CRM for monthly_cycle Week 3 is marked done

### Failure mode handling

If the build won't ship in Week 3 (scope was bigger than estimated,
upstream dep was missing, customer feedback in Week 1 was wrong):

1. Tell the customer Wednesday at the latest. Don't surprise them Friday.
2. Negotiate the smallest version that ships this week.
3. Re-scope the rest into Week 3 of next cycle (DON'T eat the overflow
   inside the same monthly budget without disclosing it).

---

## Week 4 — Report

**Goal:** Close the loop. Customer gets a written record of what
shipped, what it's doing, and what's next. Without this, the customer
loses sense of value over time and churn risk goes up.

### Activities

| When | Who | What | Output |
|---|---|---|---|
| Mon | operator | Pull this-cycle's metrics (impact of Week 3 deploy) | Metrics snapshot |
| Mon - Tue | operator | Draft the monthly report (use `monthly_template.md` as scaffold) | Report draft |
| Wed | operator | Self-review — is every claim backed by evidence? | Polished draft |
| Thu | operator | Send report (auto-sent by monthly_cycle.run_monthly_cycle Week 4 step) | Report sent |
| Fri | operator | Capture next-month's `next_month_focus` list in the cycle's plan.json | Backlog updated |

### Deliverable

**The customer-facing monthly report** (template at
`backend/retainer/ai_ops_partner/monthly_template.md`). Sent via email
with the markdown rendered inline.

### Definition of done

- Report sent to customer
- Every metric in the report is backed by a snapshot or operator log
- `next_month_focus` list is captured in plan.json for next cycle's
  Week 1 review
- Plan status flipped to "complete"

### Why Week 4 ends with the report (not "more building")

Resisting the urge to use the remaining time for "one more small fix"
is what protects the cadence. Week 4 is for closing the cycle. Anything
the operator wants to ship outside the agreed Week-3 scope rolls into
next month's prioritization — it doesn't sneak in.

---

## Cross-week disciplines

These apply across all four weeks:

- **48h response SLA on customer messages.** Anything longer kills
  trust. If you can't act on it in 48h, at least acknowledge receipt.
- **No surprise scope.** Anything outside the Week-2 locked scope gets
  a separate conversation — not silently absorbed.
- **Write down decisions.** If it's not in the cycle's plan.json or an
  email, it didn't happen. Memory-based ops partnerships fail in
  month 6 when the operator's brain runs out of cache.
- **Customer-replyable communications only.** Don't send updates from
  no-reply addresses — the whole point of the partnership is the
  customer feels like a human is on the other end.

## How a tier change happens

If the operator and customer agree the work has grown beyond the
current tier (more hours, more scope, more reactive on-call), the
upgrade conversation happens in **Week 4**, not mid-cycle. Reasons:

1. Mid-cycle re-pricing kills the predictability that's the core value.
2. End-of-cycle is when the value is most concretely visible (just
   shipped a report).
3. It gives the customer a clean break point — the upgrade takes effect
   next cycle, not retroactively.
