# Workflow System Buildout — 14-Day Delivery Phases

Internal operator reference. Customer-facing equivalent lives in `scope_template.md`. This document is the playbook for executing each phase — concrete activities, exit criteria, what burns the SLA if you skip it.

---

## Phase 1 — Discovery (Day 1-2)

**Goal:** End Day 2 with a finalized scope.md the customer is willing to sign with "confirm".

**Activities:**
- **Discovery call (60-90 min, Day 1).** Record + transcribe. Cover: every tool currently in use, every manual process being replaced, every handoff between people, every metric that matters to the customer.
- **Tool inventory.** Document every tool's account ownership, admin contact, API access status (token in hand vs. needs provisioning), plan tier (paid features matter — Squarespace webhooks are paid-only, HubSpot Sequences are Marketing Hub Pro+).
- **Workflow inventory.** Sketch each workflow as a 3-line trigger-action-actor description. No deep design yet — just count and rough scope.
- **Scope draft (Day 2 morning).** Generate scope.md via `python -m backend.services.cli fulfill --sku service_workflow_buildout --no-send --intake-file <intake.json>` then hand-edit the workflow list to match discovery findings.
- **Scope confirmation email (Day 2 afternoon).** Send the finalized scope.md. SLA clock starts on customer's `confirm` reply.

**Exit criteria:**
- Scope.md sent to customer with concrete workflow list (not generic placeholder).
- All credential/access blockers identified and assigned an owner.
- 50% invoice sent (or commitment to send within 24h of confirm).

**SLA risk if skipped:** Insufficient discovery → mid-build scope creep → 14-day SLA blown. Spend the 2 days; don't cut Discovery short.

---

## Phase 2 — Build Wave 1 (Day 3-5)

**Goal:** Foundational workflows built + tested in isolation. These are the workflows other workflows depend on.

**Activities:**
- **Platform setup.** Provision n8n / Make / Zapier workspace. Wire credentials for every tool with read-write scope appropriately limited (least-privilege, never admin).
- **Build the foundational 1-2 workflows.** Usually: intake (form/email → CRM) and payment-trigger (Stripe → CRM update). Anything that creates the data records other workflows will read.
- **Per-workflow runbook stub.** As you build, write the runbook.md in the customer artifact dir. Don't defer — by Day 12 you'll have forgotten the build decisions.
- **Daily progress note.** End-of-day async update to customer with screenshots of what was built today. Sets expectation that progress is visible without requiring a call.

**Exit criteria:**
- Foundational workflows execute successfully against synthetic data.
- Customer has reviewed the daily progress notes for Day 3-5 (acknowledgment, not approval).

---

## Phase 3 — Build Wave 2 (Day 6-9)

**Goal:** Dependent workflows (handoffs, escalations, recurring digests) built on top of the foundation.

**Activities:**
- **Build remaining workflows.** Sequencing matters — follow-up sequences depend on intake records existing; digests depend on metric-producing workflows running.
- **Connect handoffs.** Where Workflow A's output is Workflow B's input, validate the data shape end-to-end. Don't trust documentation; trust observed behavior.
- **Add operator-side failure notifications.** Every workflow needs a Discord/Slack alert on failure that reaches the operator (not the customer). This is your monitoring net during the 30-day support window.
- **Mid-build sync call (Day 7, 30 min).** Walk the customer through what's built so far via Loom or live screen-share. Catch course-corrections before validation phase.

**Exit criteria:**
- All workflows from scope.md exist and execute against synthetic data.
- Mid-build sync call completed. Customer has not requested an out-of-scope addition that's gone unaddressed.

---

## Phase 4 — Validation (Day 10-12)

**Goal:** Every workflow proven against real production data, with the customer observing.

**Activities:**
- **Synthetic end-to-end tests.** Fire each workflow with synthetic data that mirrors real production patterns. Document each run in `validation_log.md`.
- **Real-data validation.** Have the customer fire 1-2 real production invocations per workflow while you watch the run logs. Customer needs to see the actual downstream state (CRM record exists, payment processed, email sent), not just workflow "success" flags.
- **Failure-mode testing.** Intentionally break inputs (malformed form data, invalid email, API rate limit hit). Confirm each workflow degrades gracefully and the operator alert fires.
- **Performance check.** Run 10 invocations of each workflow within a 5-minute window. Confirm no rate limits hit, no queue backlogs, no slowdowns.
- **Runbook finalization.** Complete the runbook.md for each workflow. Required sections: trigger description, action chain (with platform node IDs), failure modes + retry behavior, troubleshooting steps, operator contact.

**Exit criteria:**
- `validation_log.md` documents at least 3 synthetic + 1 real-data run per workflow.
- Every workflow has a complete runbook.md.
- Customer has personally observed at least one real invocation succeed.

---

## Phase 5 — Handoff (Day 13-14)

**Goal:** Customer can independently operate, monitor, and troubleshoot every workflow.

**Activities:**
- **Loom walkthroughs (Day 13).** Record one Loom per workflow (3-7 min each) showing: trigger source, the platform UI, normal run behavior, where to look if it breaks, how to disable.
- **Handoff session (Day 14, 60 min).** Live call. Walk the runbook collection. Q&A. Confirm customer can navigate the platform without operator help.
- **Final invoice.** Send remaining 50% invoice with NET 7 terms.
- **30-day support window note.** Email customer with the explicit support window expiry date, the operator contact channel for the window, and a soft mention of the AI Ops Partner retainer.
- **Customer state advance.** Run mark_delivered + advance state machine to `delivered`. SLA timer stops alerting.

**Exit criteria:**
- All Looms recorded and linked in runbooks.
- Handoff session completed; customer self-reports comfortable with the system.
- Final invoice sent.
- `sla_timer.mark_delivered` called for this SKU on this customer.
- Customer state advanced to `delivered` in the CRM.

---

## 30-day support window (Day 14-44)

- **Free:** bug fixes, clarification calls (up to 2 × 30-min), workflow adjustments under 1 hour each.
- **Quoted as change order:** new workflows, integration with a new tool not in original scope, custom reporting.
- **Quoted as retainer:** anything ongoing (monitoring, monthly reviews, new feature requests > 1/month).

**Day 30 nudge:** Reach out 4 days before window expiry with a "anything broken? want to talk about ongoing support?" message. Conversion to retainer is highest in the final week of the support window, when the system has earned trust but the safety net is about to vanish.

---

## Common failure modes by phase

| Phase | Common failure | Recovery |
|---|---|---|
| Discovery | Customer can't articulate scope → scope.md is generic | Push to Day 3, demand a second discovery call; better to slip 1 day than build wrong thing for 14 |
| Build W1 | Credential blocker (customer can't get API token) | Escalate to customer's tool admin contact; if unresolved by Day 5, pause SLA + document blocker as customer-side |
| Build W2 | Mid-build customer request "while you're in there, can you also…" | Quote as change order; do not silently absorb |
| Validation | Real production data exposes a schema assumption the build got wrong | Fix is in-scope (it's a bug, not a change). Replan validation calendar to absorb the rework. |
| Handoff | Customer no-shows the handoff call | Reschedule once, then deliver runbooks + Looms async with a "we're available for questions" close-out email |
