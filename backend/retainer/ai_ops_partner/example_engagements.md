# Example AI Ops Partner Engagements

Four representative engagement narratives covering the price band
($2,000-$5,000/mo) and the kind of customer problems the program is
designed to solve. Names + details are composites — not real client data.

---

## Engagement 1 — Riverbend Auto Group ($3,500/mo, Month 3)

**Customer:** Marcus Chen, GM at Riverbend Auto Group. Family-owned
multi-franchise dealership (Toyota + Ford) in Sacramento. 47 employees,
$28M annual revenue. The business is healthy; the back-office is held
together with 12 disconnected systems.

**Why they signed:** Marcus's sales coordinators spent 2-3 hours/day
manually triaging trade-in valuations because the form went into a
shared inbox with no routing. The trade-ins that didn't get touched in
4 hours converted at half the rate of the fast ones. Marcus's
question on the discovery call: "I don't need a bigger CRM, I need
this one to actually work. Can you fix that?"

**What we've shipped in 3 cycles:**

- **Month 1:** CRM duplicate-contact merge (1,432 records consolidated)
  + service appointment reminder cadence (no-show rate 18% -> 11%).
- **Month 2:** Daily inventory feed monitor across 4 manufacturer
  feeds + Slack alerts for staleness.
- **Month 3:** Trade-in valuation auto-router (87% bypass the manual
  triage queue, response time 4h -> 5min).

**What the engagement looks like:** Marcus is hands-off between cycles.
Weekly check-in is 30 minutes on Tuesday morning. Most of the
communication is email — Marcus replies quickly when scope decisions
come up, otherwise leaves us alone to ship. Operator hours are right
at 32/month and have not crept up.

**Why this engagement is healthy:** The customer has a clear "before"
state (manual triage queue), a measurable "after" state (auto-router
% + response time), and the report writes itself because the numbers
tell the story. The retainer feels worth it because every cycle has
a named win.

**Risk to monitor:** Marcus is starting to ask about us taking over the
Q3 marketing automation work — that's outside the current scope. The
Week 4 conversation next month is "do we expand the retainer, or scope
that as a separate project?"

---

## Engagement 2 — Henley & Vance Real Estate ($2,000/mo, Month 1)

**Customer:** Sarah Henley, broker-owner at Henley & Vance Real Estate.
Boutique agency, 8 agents, ~80 transactions/year in the Sacramento
metro. New customer — first cycle just landed.

**Why they signed:** Sarah's marketing coordinator quit unexpectedly,
and the agency had three open automation projects mid-flight: a buyer
nurture sequence in HubSpot, an MLS-to-website inventory sync, and a
post-close referral campaign. None of the three were close to
shipping. Sarah's question on the discovery call: "Can you take the
half-built stuff and actually finish it, then keep us moving forward?"

**Month 1 narrative:**

- **Week 1 assessment** revealed the three half-built projects + a
  fourth, undisclosed problem: their MLS sync had been failing silently
  for 3 weeks and the website was showing stale inventory. Day-2
  hotfix was the inventory sync (a 4-hour repair that became Week 1's
  first deliverable + a goodwill anchor).
- **Week 2** prioritized the buyer nurture sequence as the build
  target (highest revenue impact + cleanest path to ship). Sarah
  signed off in writing on Wednesday.
- **Week 3** shipped the buyer nurture: 6-touch email sequence
  triggered by web form, integrated with HubSpot's lead-scoring,
  routing hot leads (score >70) to the on-call agent's phone via SMS.
- **Week 4 report** showed the buyer nurture is live + producing 4
  qualified leads in the first 5 days post-deploy. MLS sync uptime
  is 100% since the Week 1 hotfix.

**What the engagement looks like:** Sarah is more involved than Marcus
— she asks follow-up questions on every Week 1 email, attends every
check-in call live, and has Slack-DM'd twice with "quick question"
asks. The operator has to be disciplined about NOT absorbing the
quick-question scope inside the cycle without flagging it. So far,
two of them were genuine 10-minute answers; one became a Week 4
backlog item.

**Why this engagement is at the lower price point:** Smaller business,
smaller scope per cycle. The hours-budget is ~22-24/month vs.
Riverbend's 32. Cadence is identical; deliverable size is smaller.

**Risk to monitor:** Sarah's "quick question" frequency is high. If it
keeps trending up, the Week-4 conversation will be either (a) move to
a higher tier with explicit reactive-time included, or (b) coach the
customer to batch questions for the weekly check-in.

---

## Engagement 3 — Pinnacle Print Co. ($5,000/mo, Month 8)

**Customer:** David Okonkwo, owner of Pinnacle Print Co. Commercial
printer, 22 employees, ~$4M revenue. Long-running customer (Month 8)
— the only one currently at the top of the price band.

**Why they're at $5,000/mo:** Two things drove the price up:

1. Engagement scope expanded from "single-system fixes" (Month 1-2) to
   "own the integration layer between the press scheduling system,
   the customer portal, and the QuickBooks invoicing pipeline" (Month
   3+). This is now ~45 operator hours/month of build + maintenance.
2. They added on-call support during their high-volume Q4 window —
   the operator commits to a 4-hour response window for production
   incidents from October through January. Outside Q4 it reverts to
   the standard cadence.

**Recent shipped highlights:**

- **Month 6:** Job-status SMS notifications for customers (45+ daily
  triggers; saved ~6 hours/week of "where's my order?" calls).
- **Month 7:** Auto-invoice generation on press-complete event;
  eliminated a 4-day lag between job completion and bill being sent.
  Days-sales-outstanding dropped from 38 to 22.
- **Month 8 (current):** Building a customer self-serve reorder portal
  — pulls customer's print history from the press scheduling system,
  one-click reorder, auto-routes to production queue. Targeting Q4
  launch.

**What the engagement looks like:** David delegates fully — the
operator works directly with his production manager, Janelle, who
makes day-to-day scope calls. David weighs in at Q1 / Q3 planning
conversations. This is the maturity profile of a healthy AI Ops
Partner engagement.

**Why this customer renews:** Their P&L now visibly depends on the
integration layer the retainer maintains. Churning us means
re-staffing or going back to manual ops. The customer doesn't
re-evaluate the retainer cost annually because the value is constant
and measurable.

**Risk to monitor:** Single-point-of-failure on the operator (Alex).
If Alex is sick during a Q4 incident, the engagement has no fallback.
Mitigation queued: document the integration layer architecture so a
contractor could spin up in 48h.

---

## Engagement 4 — Brightside Pediatric Dental ($2,500/mo, Month 2)

**Customer:** Dr. Priya Shankar, dentist-owner at Brightside Pediatric
Dental. Two-location practice in Sacramento + Roseville, 18 employees
across both. Patient-base of ~3,200 active families.

**Why they signed:** Priya had a specific pain point: appointment
cancellations and no-shows. Her front desk staff was spending 6+ hours
a week on rescheduling phone calls. The practice management software
(Dentrix) had basic reminders but no intelligent rebooking — when a
slot opened up, it sat empty unless front-desk manually called the
waitlist. Priya's question: "Can you make the empty-slot fill itself?"

**Month 1 narrative:**

- **Week 1 assessment** mapped the cancellation -> open-slot ->
  refill loop. Discovered the practice already had a waitlist in
  Dentrix but nobody was using it. Real bottleneck: no integration
  between the cancellation event and the waitlist notification.
- **Week 2** scoped a smart-rebooking automation: when an appointment
  cancels within 48 hours of the slot, scan the waitlist for
  patients with matching insurance + provider preferences + last-seen
  date, send a one-tap SMS booking link to the top 3 matches.
- **Week 3** shipped it. Integrated via Dentrix's REST API +
  Twilio for SMS + a small Postgres table for the matching logic.
- **Week 4 report** showed 12 cancellations in the first 18 days,
  9 of those rebooked via the auto-fill (75%). Front desk reclaimed
  ~4.5 hours/week. Patient wait-list satisfaction also went up
  because waitlisted patients suddenly got served.

**Month 2 (current cycle):** Building a 30-day appointment-need
forecaster — pulls treatment plans from Dentrix, predicts when each
patient should be back, generates a "patients-to-contact" list for
the front desk every Monday. Goal: stop relying on patients to
remember to book follow-ups.

**What the engagement looks like:** Priya has limited time (she's
seeing patients all day) but is highly engaged when she gets her
weekly call. Her practice manager, Felipe, is the operational
counterpart day-to-day. Communication is mostly via Felipe; the
operator pings Priya only for scope decisions.

**Why this engagement is at the mid-band:** Specific problem domain
(dental practice ops), measurable success metric (no-show / rebook
rate), but the integration surface is smaller than Riverbend or
Pinnacle. ~28 operator hours/month.

**Risk to monitor:** Dentrix API has rate limits we're close to.
If the practice grows another 20% in volume, we'll hit them and
need to either negotiate up the API limit or batch our calls
differently. Track + raise in Q3 planning.

---

## Patterns across the four

What these engagements have in common — and what makes the AI Ops
Partner Program a real product, not a generic consulting offer:

1. **Each has a named pain point at signup.** Marcus: trade-in
   triage. Sarah: stalled half-built projects. David: integration
   layer. Priya: empty appointment slots. The discovery call is
   useless if you can't get a one-sentence answer to "what's the
   thing you'd most want fixed first?"

2. **Each has a measurable success metric within Month 1.** Not "we
   feel better about ops" — "no-show rate dropped 7 points" or
   "response time went from 4 hours to 5 minutes." Without this,
   the customer can't justify renewing in Month 6.

3. **Each has a clear scope boundary.** What we own vs. what stays
   with the customer's existing team. Ambiguity here is the #1
   cause of operator-hour creep and unhappy customers.

4. **The cadence is the product.** Customers don't pay $3,500/mo for
   chaos; they pay for "I know what's happening this month and what's
   going to be shipped." The 4-week rhythm IS what they're buying.

5. **The first cycle's hotfix discovery is normal.** Sarah's MLS
   sync, Riverbend's hidden duplicate-contact mess, Brightside's
   unused waitlist — in 3 of 4 engagements, Week 1 revealed an
   urgent fix that wasn't on the customer's radar. Be ready for
   that without billing it as scope expansion (it's part of the
   discovery work).
