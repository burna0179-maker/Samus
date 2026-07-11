# AI Ops Partner — Build (scope template)

**SKU**: `service_ai_ops_partner_build`
**Price**: $2,000-$5,000 one-time (quote-based, scoped per engagement)
**SLA**: 30 days from scope-confirmation reply
**Pairs with**: `retainer_ai_ops_partner` ($5,000/mo) for ongoing upkeep after the build

This is the operator playbook for delivering an AI Ops Partner build. The customer-facing email body lives in the upsell composers; this file is what the operator works from once the customer has confirmed scope.

## Engagement shape

A 30-day build with weekly customer-facing milestones. Output is a *standing operations engine* — a set of integrated automations the customer can keep using indefinitely, with a runbook per workflow and a monitoring layer that tells someone when anything breaks.

The retainer (`retainer_ai_ops_partner`) is the natural follow-on; without it the customer owns the operations engine outright but does their own triage. That's a real choice and the build is intentionally handoff-ready so the customer can self-manage if they prefer.

## Phase 1 — Discovery & blueprint (week 1)

**Operator activities**:
- Discovery call (60 min): walk the customer's current operations end-to-end. What's manual? What's broken? What's working but fragile?
- Inventory pass: list every tool / system the customer uses, plus the integration touchpoints between them.
- Automation opportunity map: rank the candidate automations by `(hours saved per week) × (failure cost) × (build feasibility)`.
- Scope confirmation: pick the 3-8 workflows that will be built. Quote the final price for the engagement.

**Deliverables to customer**:
- Current-state audit document
- Automation blueprint with prioritized build order
- Final scope-and-price confirmation (customer signs off before week 2 starts)

**Definition of done**:
- Customer has explicitly confirmed the scope list + price.
- Custom Stripe invoice generated with the negotiated price, prior-tier credit applied, and the customer's confirmation captured in the CRM.

## Phase 2 — Core build (weeks 2-3)

**Operator activities**:
- Build each automation in priority order. Test in isolation, then test integrated.
- Wire monitoring + alerts for every workflow as it ships — failure visibility is mandatory, not a Phase 4 add-on.
- Send a Friday-of-week-2 progress update with screenshots / Loom of what's running.

**Deliverables to customer**:
- Each built workflow, demoed via Loom or live call.
- Monitoring dashboard URL (read-only customer access).
- Weekly status email with what shipped + what's queued for next week.

**Definition of done**:
- Every scoped workflow runs end-to-end on real customer data.
- Every workflow has at least one alert wired (Slack / email / dashboard tile) for the most-likely failure mode.

## Phase 3 — Runbooks & handoff prep (week 4 first half)

**Operator activities**:
- Write a runbook per workflow. Structure: trigger → expected behavior → known failure modes → operator triage steps → escalation contact.
- Build the handoff package: combined runbook PDF, access credentials inventory (which logins exist for what), the monitoring URLs, and a "first 30 days" guide for the customer.
- Schedule the handoff call.

**Deliverables to customer**:
- Per-workflow runbook (markdown + PDF).
- Handoff package: single PDF combining everything, plus a working file the operator + customer share.

**Definition of done**:
- Runbooks reviewed for completeness — a stranger could triage a failure from the runbook alone.
- Handoff package shared with customer ahead of the call.

## Phase 4 — Handoff (week 4 second half)

**Operator activities**:
- Handoff call (60 min): walk the customer through every built workflow. Demo each monitoring alert. Show how to triage one common failure mode hands-on.
- Customer Q&A: capture anything that needs follow-up (small fixes, missed scope) and decide which is in-scope vs out-of-scope.
- Pitch the retainer: "Here's what month 1 of the retainer would look like. Want to start it?"

**Deliverables to customer**:
- Recorded handoff call.
- Final acceptance email — customer confirms the build matches scope and they've taken ownership.
- (If retainer started) First-week welcome email from the AI Ops Partner — Retainer onboarding flow.

**Definition of done**:
- Customer's acceptance reply in the CRM artifact log.
- 30-day SLA marked `delivered` in the customer state machine.

## Out-of-scope (document up front)

- Ongoing tuning / triage / net-new builds after week 4 — covered by the retainer SKU.
- Building against systems the customer doesn't own or can't grant access to.
- Acting as the customer's IT helpdesk during the build window. Build issues yes; "my laptop won't boot" no.
- Custom data engineering work (ETL pipelines, data warehouse builds) — separate engagement scope.

## Quoting reference

| Build complexity | Workflows in scope | Typical quote |
|---|---|---|
| Light | 3-4 simple workflows, 1-2 systems | $2,000 |
| Standard | 4-6 workflows, 3-4 systems, 1 monitoring stack | $3,000 |
| Deep | 6-8 workflows, 4+ systems, integrated monitoring + alerting | $4,000 |
| Heavy | 8+ workflows, 5+ systems, complex data flow, multi-team handoff | $5,000 |

Operator adjusts ±$500 based on integration difficulty (legacy systems, custom APIs, etc.). Final price is conversational — when in doubt, quote the bracket above and explain what would push it higher or lower.

## Credit from prior-tier purchases

If the customer reached AI Ops Partner Build via the upsell chain, their prior purchases apply as credit toward this engagement:

- Customer who paid for `service_workflow_buildout` ($2,500-$3,000): credit applies to the build quote. Operator generates the invoice with the credit deducted line-item.
- Customer who paid for `service_workflow_rescue` ($500) but skipped Buildout: $500 credit applies.

Credit caps at the build price — excess does not roll over to the retainer.
