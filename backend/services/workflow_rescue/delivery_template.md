# 48-Hour Workflow Rescue — Operator Delivery Playbook

**SKU:** `service_workflow_rescue` · **Price:** $500 · **SLA:** 48 hours from the customer's "go" reply.

**The offer the customer bought:** *Stop Doing That Manual Task by Friday.* Tell us the one workflow draining your week and we will design, deploy, and hand it over in 48 hours. The deliverable is a production-ready workflow that saves hours every week from day one.

This playbook is the operator side of that promise. Four phases. Same shape the customer was sold on. Don't deviate from the structure — the structure is the product.

---

## Operator constraints (read before accepting a build)

- **Cap: 3 builds per week.** The blog post promises a hard limit and that scarcity is the offer. If a 4th request lands inside a calendar week (Mon-Sun, operator's local time), it goes on the wait list. Do not start a 4th. Tell the customer the next available slot is Monday.
- **Scope gates (from [fixed_scope_template_pipeline.py](../../../recovery/fixed_scope_template_pipeline.py)):** the build must stay inside `MAX_WORKFLOW_STEPS = 5`, `MAX_EXTERNAL_TOOLS = 3`, `MAX_TEMPLATES = 3`. Any of those exceeded = out of scope. Don't silently overbuild. Offer the Workflow System Buildout SKU instead.
- **Viability gate:** if Phase 1 produces no viable automation path, you do not proceed — and the customer is not charged. The blog says it plainly: *"If we cannot map a viable automation path for your workflow, you do not proceed."* Honor that or the whole offer collapses.
- **No elevated runs.** Anything the operator builds runs as a non-elevated process. Nothing requires admin on the customer's machine and nothing requires admin on yours.
- **Don't start from blank canvas.** Pull the closest match from [`example_workflows.md`](example_workflows.md). Most rescues are one of those seven patterns with config tweaks. Branching from a proven example cuts build time roughly in half.

---

## Phase 1 — Workflow Audit (Hour 0-8)

The customer has named one workflow. Your job in the first 8 hours is to verify it's the right one, confirm it has a viable automation path, and lock the scope. This is the only phase where you can still walk away.

### What to ask the customer

A 15-30 minute call (or async questionnaire if they prefer) covering:

1. **The actual task, in their words.** Have them walk you through it step by step the way they do it today. Where does it start (an email, a form, a sale, a calendar event)? What do they touch in the middle (sheets, CRM, inbox, Slack, billing)? Where does it end (a record created, a notification sent, a file filed)?
2. **The tools that are already in their stack.** Not the tools they wish they had — the ones they're paying for today. Get account-level confirmation: which Stripe, which HubSpot, which Slack workspace, which email account. The build uses their accounts, not yours.
3. **Volume and timing.** How often does this fire — 5x a day, 50x a day, 500x a day? Are there spikes (Monday morning, end of month)? Volume changes which platform you pick and whether webhooks vs. polling will hold.
4. **What breaks today.** Where do they currently lose things — forgotten Slack pings, missed leads, manual data entry errors, missed SLAs? That failure becomes Phase 3's validation target.
5. **Who else touches it.** Solo? A team of 3? A team of 30? The runbook in Phase 4 has to land for the person who'll be on the hook after handoff, not just the buyer.

### What to verify yourself

- **API access exists.** Confirm the tools they named have public APIs, webhooks, or supported connectors on n8n / Make / Zapier. If a critical tool is closed (some legacy in-house CRMs, some niche industry tools), the path may not exist.
- **The customer can grant credentials.** They have admin (or can get it the same day) for every account in the chain. If a key tool's admin is on vacation for the next two weeks, the SLA breaks before you start.
- **The downstream system can actually accept the writes.** A "create record in HubSpot" only works if the HubSpot plan they're on supports the API endpoint you need. Marketing Hub Starter vs. Pro is the difference between *works* and *doesn't*.
- **No regulated-data landmines.** If the workflow touches HIPAA / financial / PII at volume, flag it — that's a different conversation and probably not a 48-hour rescue.

### The viability decision gate (end of Hour 8)

By Hour 8 you make one of three calls:

**A. Viable.** Path exists, tools are accessible, scope fits inside 5/3/3. Write the scope.md, send it to the customer, confirm receipt with a "reply 'go' to start the SLA clock." Move to Phase 2.

**B. Out of scope, salvageable.** The work is real but it's bigger than a Rescue — 8 steps, 5 tools, multiple workflows. Send the Workflow System Buildout pitch. Don't try to cram a Buildout into a Rescue; everyone loses.

**C. Not viable.** No clean automation path (closed API, customer can't get admin, requires human judgment at every step). **Reject the build. Refund the customer.** Use the script below — verbatim.

### Reject script (use verbatim when path is not viable)

```
Hey [name],

I spent the last few hours mapping the workflow you described.
After looking at it end-to-end, I can't put together a viable
automation path for it inside the 48-hour Rescue scope. The
blocker is [one specific reason — e.g., "the tool you use for X
doesn't expose an API or webhook we can hook into," or "the
decision step in the middle requires human judgment that can't
be reliably scripted"].

Per the offer: if I can't map a viable path, you don't pay. I'm
issuing a full refund — you should see it back on your card
within 3-5 business days.

If you want, I can suggest [one alternative — a different
workflow in your business that would automate cleanly, OR a
manual SOP that would tighten the existing process without
automation]. No obligation either way.

— [operator]
```

Then issue the refund through the billing console and mark the customer state to `rejected_not_viable` in the CRM. Free the build slot for the week immediately — someone else is waiting.

### Phase 1 — definition of done

- [ ] Customer call (or async questionnaire) completed, notes captured
- [ ] All tools in chain verified for API/webhook access + customer admin
- [ ] Volume + timing profile understood
- [ ] Scope fits inside 5 steps / 3 tools / 3 templates (or, formally rejected)
- [ ] `scope.md` written and sent to customer
- [ ] Customer replied "go" → SLA clock armed
- [ ] Customer artifact dir opened at `<SAMUS_ARTIFACT_ROOT>/customers/<slug>/service_workflow_rescue/`
- [ ] Rescue posted to operator channel: `RESCUE STARTED <slug> deadline=<sla_deadline>`

---

## Phase 2 — End-to-End Build (Hour 8-32)

You now have 24 hours to build the thing. The discipline here is *not* "do your best work" — it's "stay inside the scope gates and use the templates."

### Pick the platform once, don't switch

Default priority order:

1. **n8n** — operator-hosted, lowest long-term cost, customer doesn't need a subscription to use it
2. **Make.com** — customer-hosted, friendlier UI for non-technical handoff
3. **Zapier** — customer-hosted, fall-back when one of the customer's tools only has a Zapier integration

Pick the highest-priority platform that supports every tool in the chain. Don't mix platforms inside one workflow.

### Branch from the closest template, don't start blank

Open [`example_workflows.md`](example_workflows.md). Find the example whose trigger + actions most closely match what you scoped. Branch off it. The seven worked examples cover ~85% of inbound rescues; if none of them match, you're probably building something that should have been rejected in Phase 1.

### Scope cap — these are hard ceilings, not targets

- **≤ 5 nodes total** (trigger + actions + notifications, counted end-to-end)
- **≤ 3 external tools** (counted as distinct SaaS apps you authenticate against)
- **≤ 3 templates** (counted as distinct template categories from the registry)

If you're at node 5 and need a 6th, **STOP.** The 6th node is not a small ask — it's a category change. Email the customer: *"What I'm building has grown past what the Rescue covers. We can either ship a tighter version that hits 80% of the goal in scope, or convert to the Buildout SKU with a new quote. Which do you want?"*

### Build discipline — what to test as you go

You're not unit-testing every node. You're sanity-checking three things at every stage:

1. **The trigger actually fires** when the real event happens (don't trust the platform's "test" button — it usually mocks the payload differently than production)
2. **The actions actually mutate the downstream system** (not just "the workflow's success counter went up" — go look in HubSpot / Notion / Stripe and confirm the record exists with the right shape)
3. **The credentials hold** under repeated invocations (some OAuth tokens expire after the first call if you misconfigure the scope)

Wire credentials with the customer's accounts. If they need to provision an API token, request it **once**, with exact step-by-step instructions in the email, by **Hour 16**. After Hour 24, any "waiting on customer" blocker puts the SLA at risk.

Add an error-notification hop to your operator Discord/Slack so production failures during the 30-day support window ping you without depending on the customer noticing.

### Phase 2 — definition of done

- [ ] Platform picked and confirmed compatible with all tools
- [ ] Workflow built end-to-end, all nodes connected, ≤ 5 nodes total
- [ ] Inside scope cap: ≤ 5 steps, ≤ 3 tools, ≤ 3 templates
- [ ] All credentials wired against the customer's accounts (not yours)
- [ ] Error notification routes to operator-controlled channel
- [ ] At least one smoke test passed end-to-end (synthetic data)

---

## Phase 3 — Live Validation (Hour 32-44)

The blog promises *a production-ready workflow that saves hours every week from day one.* "Production-ready" is verified, not asserted. This phase is where you prove it.

### What counts as a successful test

Three things, in this order:

1. **Three synthetic runs** with payloads that mirror real production traffic. Document each in `validation_log.md` with timestamps, inputs, and the actual downstream effect you observed. Don't trust the platform's "success" status alone — go check HubSpot, go check the Notion DB, go check the Stripe invoice. Confirm the record exists with the right shape, in the right place, with the right metadata.
2. **One real invocation, watched live.** Have the customer fire one real event (submit a real form, make a real test booking, push a real row to the sheet) while you watch the run log. This catches the things synthetics miss — timezone offsets, payload field-name drift, OAuth scope gaps that only surface against the real producer.
3. **One failure-mode test.** Force one failure (bad input, expired token, downstream tool returning 500) and confirm the error notification fires to your channel within 30 seconds.

### Customer-facing demo

Schedule a 15-30 minute walkthrough with the customer at ~Hour 40. Record it (Loom or equivalent — short, 5-10 min). On the demo:

- Show the trigger firing in real time
- Show the downstream record being created in their system
- Show the failure case + your monitoring catching it
- Show the off-switch — how they disable the workflow if they ever need to

The demo is the moment "production-ready" becomes felt, not just claimed. If the customer doesn't watch a real run complete, they don't actually believe the thing works — and they'll quietly stop trusting it within a week.

### Phase 3 — definition of done

- [ ] 3 synthetic runs documented in `validation_log.md` with timestamps + downstream verification
- [ ] 1 live customer-fired invocation observed end-to-end
- [ ] 1 failure-mode test confirmed error notification path works
- [ ] Customer walked through the system live + the recording is saved
- [ ] No unresolved bugs (anything found in validation → fix or document the workaround)

---

## Phase 4 — Team Handoff (Hour 44-48)

The customer doesn't just want the workflow running — they want to *own it*. By Hour 48 they have everything they need to operate, debug, and disable it without you.

### What gets handed off

**1. The runbook** (`runbook.md` in the customer artifact dir). Required sections:

- **Trigger** — what event fires this workflow, in plain English
- **Step-by-step action chain** — each node by name + what it does + what data it touches
- **The off-switch** — exact clicks to disable the workflow if they need to (this is non-negotiable; if they can't turn it off themselves they don't own it)
- **Failure modes + retry behavior** — what happens if step 3 fails, what happens if their CRM is down, what the platform's automatic retry policy is
- **"If this breaks, do X"** — 3-5 troubleshooting steps the customer can run before contacting you
- **Embedded Loom walkthrough** from Phase 3

**2. The access list** — every account, credential, and connection used in the build:

| Tool | Account used | Auth method | Where credential lives | Who else needs access |
|---|---|---|---|---|
| (filled in per build) | | | | |

This is the artifact that survives operator turnover on the customer's side. If their ops person quits in 6 months, the next person picks this up and knows exactly what they're inheriting.

**3. The escalation contact** — single named operator (you), preferred channel (email or Slack), 30-day support window expiry date stated explicitly.

### The handoff email

Send to the customer at ~Hour 46 with:

- Link to `runbook.md` (or paste inline as markdown if they don't have access to the artifact storage)
- Link to the Loom walkthrough
- The access list table
- The off-switch instructions (also restated here, not just in the runbook — first place they'll look in a panic)
- Explicit 30-day support window expiry date
- Soft mention of the AI Ops Partner retainer for ongoing maintenance (one Rescue almost always surfaces 2-3 more workflows worth automating)

### Mark delivered (Hour 48)

```
python -c "from backend.memory.customers import CustomerStore; from backend.services import sla_timer; \
  s=CustomerStore(); sla_timer.mark_delivered(customer_store=s, customer_id='<slug>', sku_id='service_workflow_rescue')"
```

Then advance customer state to `delivered` via the normal CRM CLI. SLA timer stops alerting once `delivered_at` is stamped.

### The completion bar

A Rescue is not "done" until **all** of these are true:

- [ ] Customer has the runbook + Loom + access list in their hands (not just sitting in your artifact dir)
- [ ] Customer can disable the workflow themselves without contacting you
- [ ] Customer has fired at least one real invocation and watched it complete
- [ ] Workflow has run cleanly under at least 3 synthetic + 1 real test
- [ ] Error notifications route to operator-controlled channel for the 30-day window
- [ ] `delivered_at` stamped on the SLA timer
- [ ] Customer state advanced to `delivered`
- [ ] The build counts against the 3-per-week cap; build slot is released for next week

If any one of those is missing, the Rescue is not delivered — even if the workflow is running. Don't mark it complete to clear the queue.

---

## Phase 5 — Post-delivery (Day +1, +7, +30)

Not part of the 48-hour clock, but part of the offer (the 30-day support window the customer paid for).

- **Day +1:** ping the customer for any first-day issues. Most issues surface on day 1, not at month-end.
- **Day +7:** review the workflow's run logs yourself (failed invocations, retry storms, timeouts). Fix anything broken silently before the customer notices.
- **Day +30:** support window closes. Send a closing note + soft pitch for the AI Ops Partner retainer. The strongest retainer pitches reference the *next* bottleneck you already spotted while watching their logs.

---

## Failure modes + recovery

| Failure | Recovery |
|---|---|
| Customer never replies "go" to scope confirm | After 48h, send a "still want this? we held a build slot for you" nudge. After 7d, refund + reclaim slot. Mark state to `churned`. |
| Scope-gate flag fires mid-build (>5 steps / >3 tools / >3 templates) | Stop. Email customer with the specific gate that fired + offer the Workflow System Buildout SKU. Do NOT build past the gate. |
| Customer's tool doesn't support a parsed action (e.g. they want Salesforce but lack admin) | Propose alternative tool from supported list OR upgrade to Buildout. Document the pivot decision in customer state event reason. |
| SLA fires `OPERATOR_ALERT_OVERDUE` at 48h with no delivery | Same-day customer contact, identify blocker, commit to firm new deadline within 48h. Apply $50 service credit if blocker was operator-side. |
| Workflow validates but produces wrong output post-delivery | Bug in the build, not scope. Fix inside the 30-day support window at no charge. |
| Customer asks for "one more small thing" mid-build | Each "one small thing" is a node. If adding it exceeds scope, defer to Buildout. If it doesn't exceed, accept it — but log every accepted change so you can see the pattern across rescues. |

---

## What a delivered Rescue looks like

In `<SAMUS_ARTIFACT_ROOT>/customers/<slug>/service_workflow_rescue/`:

- `scope.md` — the contract
- `validation_log.md` — 3 synthetic + 1 real test, with timestamps
- `runbook.md` — operator + customer reference, includes embedded Loom + access list + off-switch
- `access_list.md` (or embedded in runbook) — tool/account/credential map for handoff
- Loom link recorded in runbook
- Customer state advanced to `delivered`
- SLA timer `delivered_at` stamped
- Customer has a working production workflow they can trace, disable, and re-enable themselves — and the manual task that was killing them is done.
