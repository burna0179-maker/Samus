# Workflow System Buildout — Example Systems

Four realistic multi-workflow builds with full architecture sketches. Use as reference for scoping new engagements and as a starting point for the actual workflow design at Phase 2.

---

## §1. The "All-In Service Business" buildout

**Customer profile:** Local home-services business (HVAC, roofing, electrical, plumbing) doing $500K-$2M/yr, 1-2 office staff drowning in admin.

**Bottleneck quote:** "Phones ring all day, jobs get lost in text threads, invoices go out late, follow-ups don't happen. I need the whole back-office automated so my office manager can focus on real work."

### Workflows (5)

```
┌─────────────────────────┐
│ 1. Inbound lead intake  │  Squarespace form / Google Ads call → ServiceTitan job
│                         │  + Slack ping to dispatcher channel
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ 2. Job-scheduled        │  ServiceTitan status='scheduled' webhook → customer
│    confirmation         │  SMS + email with arrival window + tech name
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ 3. Job-completed        │  ServiceTitan status='completed' → Stripe invoice
│    invoicing            │  generated + emailed + Notion job-record updated
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ 4. Payment received     │  Stripe payment_intent.succeeded → review-request
│    review request       │  SMS (24h delay) + Google Business Profile link
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ 5. Weekly ops digest    │  Mon 7am → Owner Slack DM: jobs done last week,
│                         │  revenue, AR aging, jobs scheduled this week
└─────────────────────────┘
```

**Tools:** ServiceTitan (or Housecall Pro), Stripe, Twilio (SMS), Gmail, Slack, Notion, Squarespace (= 7 tools).

**Why this is a Buildout, not a Rescue:** 5 distinct workflows, 7 tools, shared data model across workflows. Each individual workflow is gate-compliant; the system is not.

**Operator notes:**
- ServiceTitan webhooks have a 5-minute delay during peak hours — design SLAs accordingly.
- Stripe invoice generation should pull line items from ServiceTitan job-records (don't duplicate the job pricing in two systems).
- Review-request SMS at 24h is the conversion-rate sweet spot (anything immediate feels pushy; anything past 48h misses the post-job satisfaction window).
- Weekly digest: owner-only DM, not a channel — anything financial in a shared channel violates owner privacy expectations.

---

## §2. The "Agency Client-Onboarding Engine" buildout

**Customer profile:** Boutique agency (marketing / design / consulting) with 10-30 active clients, billing $5K-$25K/mo per client, manual onboarding eats 4-6h per new client.

**Bottleneck quote:** "Every new client gets a 4-page Notion onboarding doc that nobody reads, a kickoff call we have to manually schedule, and a Slack channel I create by hand. By the time they're properly set up it's been a week."

### Workflows (4)

```
┌─────────────────────────┐
│ 1. Signed contract →    │  PandaDoc 'completed' webhook → Notion client page
│    project setup        │  + Slack channel created (#client-{name}) + Linear
│                         │  project created with template tasks
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ 2. Kickoff scheduling   │  Calendly link sent in client welcome email →
│                         │  on-booking: kickoff agenda Notion page populated +
│                         │  agency team Calendar invites
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ 3. Monthly retainer     │  Day 1 of month: Stripe subscription auto-bills,
│    cycle                │  failed-payment branch → ops Slack + customer DM,
│                         │  successful-payment → Notion "active" badge update
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ 4. Health-check         │  Weekly Friday: scan Linear for stale tasks per
│    digest               │  client, scan Slack channel for low-activity weeks,
│                         │  generate at-risk-clients report → CEO Slack DM
└─────────────────────────┘
```

**Tools:** PandaDoc, Notion, Slack, Linear, Calendly, Stripe, Gmail (= 7 tools).

**Why this is a Buildout:** Cross-workflow data dependencies (project setup creates the Notion page that all subsequent workflows reference), failure handling matters (failed payments must reach ops without spamming the channel), and the health-check digest requires multi-tool aggregation.

**Operator notes:**
- PandaDoc → Notion: pull the client name from contract fields, not from email metadata (legal name vs. brand name mismatch is a real problem).
- Slack channel naming: enforce a convention (#client-{lowercase-name}-{yyyy}) so the channel list is searchable. Don't let humans pick.
- Stripe failed-payment branch: 3-day dunning window before churning, with retry on day 1 and day 3, then ops escalation.
- Health-check digest: "stale" thresholds need owner input (some clients are intentionally low-touch — encode their cadence as Notion property).

---

## §3. The "SaaS Trial-to-Paid Conversion Engine" buildout

**Customer profile:** Early-stage SaaS (B2B, $50-$500 MRR per customer), 100-500 free trials/month, low trial-to-paid conversion because onboarding follow-up is manual and inconsistent.

**Bottleneck quote:** "We have 200 trials a month and convert maybe 8%. Our follow-up is whoever-remembers-emailing-them, which is nobody. I want every trial to get the same conversion-optimized follow-up sequence and the high-intent ones to escalate to a sales rep."

### Workflows (6)

```
┌─────────────────────────┐
│ 1. Trial signup         │  Product trigger 'user.created' → HubSpot Contact +
│                         │  Customer.io subscriber + Slack #signups ping
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ 2. Day-1 activation     │  Schedule trigger: 24h after signup. Check product
│    check                │  for activation event (key user action). If yes →
│                         │  fast-track sequence; if no → onboarding sequence
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ 3. Engagement scoring   │  Daily: pull product analytics → score each trial
│                         │  on engagement (sessions, key events, team members).
│                         │  Tag in HubSpot. Score > 70 → workflow #4.
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ 4. High-intent          │  Score > 70 → create HubSpot Task on assigned rep
│    escalation           │  + Slack DM to rep + Calendly booking link inserted
│                         │  in next outbound email
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ 5. Trial-end conversion │  Day-12 of 14-day trial → personalized "ready to
│    push                 │  upgrade?" email with usage stats embedded
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ 6. Churn-detection      │  Failed payment / cancellation → exit-interview
│                         │  survey + win-back coupon (24h delay) + Notion
│                         │  churn record for post-mortem
└─────────────────────────┘
```

**Tools:** Customer.io, HubSpot, Stripe, Slack, Calendly, Notion + customer's product analytics API (= 7 tools).

**Why this is a Buildout:** Cross-workflow state (activation score, intent tier, trial day), branching logic (fast-track vs. onboarding sequence), and human-in-the-loop escalation (sales rep handoff) all require coordination beyond what a single workflow can express.

**Operator notes:**
- Engagement scoring is the hard part — it needs the customer's actual product analytics, not generic email-open metrics. Spend Discovery time understanding what "engaged" means for THEIR product.
- HubSpot Task assignment: round-robin by territory if reps aren't pre-assigned. Avoid the "lowest workload" algorithm — it punishes high performers.
- Trial-end email at Day 12: use the customer's actual product usage stats inline (sessions count, key features used, team members invited). Generic "your trial ends Friday" emails convert at ~1%; usage-stat-personalized convert at ~6-8%.
- Win-back coupon: 30% off first month is the conversion sweet spot. Higher discounts attract bad fits.

---

## §4. The "Real-Estate Lead-Engine + Listing-Sync" buildout

**Customer profile:** Real-estate brokerage with 15-50 agents, leads scattered across portal aggregators (Zillow, Realtor.com, Trulia), agent assignments done manually by office manager.

**Bottleneck quote:** "Leads come from 5 different portals. Office manager copies them into our CRM and assigns to whichever agent is up on the rotation. By the time the agent calls, the lead has talked to 3 other agents. Speed-to-lead is killing us."

### Workflows (5)

```
┌─────────────────────────┐
│ 1. Multi-portal lead    │  Email-parse trigger (5 portals → forwarded to
│    intake               │  intake@) → Follow Up Boss CRM Contact + Deal
│                         │  with portal source tagged
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ 2. Speed-to-lead        │  CRM new-contact webhook (within seconds of intake)
│    assignment           │  → territory-based agent lookup → SMS to agent +
│                         │  Twilio call connecting agent to lead
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ 3. Listing sync         │  MLS feed (poll every 15 min) → push new/updated
│                         │  listings to broker website + social media post
│                         │  (Buffer schedule) + email subscribers
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ 4. Showing scheduled    │  ShowingTime booking → SMS to agent + buyer with
│    notifications        │  showing details + property address + lockbox code
│                         │  + Slack channel ping to broker channel
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ 5. Closed-deal          │  Follow Up Boss deal closed → commission split
│    accounting           │  calculated + accounting CSV row appended +
│                         │  Slack #closed channel celebration ping
└─────────────────────────┘
```

**Tools:** Follow Up Boss (CRM), Twilio, MLS API or feed, Buffer (social), Mailchimp, ShowingTime, Slack, accounting CSV/Sheet (= 8 tools).

**Why this is a Buildout:** Speed-to-lead workflow alone needs <60-second end-to-end latency (form-parse → CRM → SMS → call-bridge), which requires careful pipeline design. Listing sync runs on a poll schedule with debouncing. Commission accounting has real money implications — needs validation + audit trail.

**Operator notes:**
- Email-parse intake: every portal has a slightly different format. Spend Discovery time getting sample emails from each, build per-portal parser configs.
- Speed-to-lead: SMS + Twilio call-bridge in <60s is the conversion-rate cliff. Lead responding to first contact is 391% more likely to convert than 30-min response.
- MLS feeds vary by region (RETS vs. RESO Web API vs. portal-specific). This is the single highest-effort integration in any RE buildout; quote accordingly.
- Commission split logic: pull split percentages from the deal's agent record, NOT from a hardcoded map. Splits change all the time.
- Accounting CSV: append-only, with a daily reconciliation report. Never let workflow code mutate accounting data.
