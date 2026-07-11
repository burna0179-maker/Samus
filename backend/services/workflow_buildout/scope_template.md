# Workflow System Buildout — Scope of Work

**SKU:** `service_workflow_buildout` · **Price:** $2,500–$3,000 (fixed; final price set at scope confirmation) · **Timeline:** 14 days from scope-confirmation reply.

---

## What you're buying

A multi-workflow operations system built across your existing tool stack. Unlike the 48-Hour Workflow Rescue (single workflow, 48h, hard scope ceiling), the Buildout covers an end-to-end process — typically 3-7 connected workflows that share data, handle handoffs, and replace a meaningful chunk of manual ops work.

This is a one-time engagement. Ongoing maintenance, monitoring, and new feature requests after the 30-day support window land in the AI Ops Partner retainer ($150-$500/month tier).

## What's in scope

The scope is fixed at confirmation and documented below in three sections: workflows, integrations, and deliverables. Anything outside those sections is out of scope — we'll add it on a change order, never silently.

### Workflows being built

> _Replace this block at scope-confirmation time with the actual workflow list parsed from the customer intake. Example for a typical buildout:_

1. **Lead intake** — Squarespace form / inbound email → HubSpot Contact + Deal, owner-routing by territory, Slack ping.
2. **Follow-up sequencing** — Deal-stage-change triggers → 3-touch outreach (day+1 email, day+3 task, day+7 reminder), pauses on customer reply.
3. **Closed-won handoff** — Stripe payment → onboarding email, Notion client page, kickoff Slack channel created, recurring billing wired.
4. **Cancellation salvage** — Stripe `customer.subscription.deleted` → exit-interview email, win-back coupon, Notion churn record.
5. **Weekly ops digest** — Schedule trigger every Monday → pulls metrics from HubSpot + Stripe + Notion, posts summary to Slack #leadership.

### Integrations

> _Replace at scope confirmation. Typical stack uses 4-6 tools:_

- HubSpot (CRM + Sequences)
- Stripe (payments + subscriptions)
- Slack (notifications + handoffs)
- Notion (client records + dashboards)
- Calendly (booking + kickoff scheduling)
- n8n self-hosted OR Make.com customer-account (workflow runtime)

### Deliverables

- Discovery + scope-of-work document (Day 1-2) — this doc, finalized after the kickoff call.
- Multi-workflow system built across your tool stack (Day 3-9).
- Integration testing + data flow validation (Day 10-12) — real production data, not synthetic.
- Handoff session + runbooks for each workflow (Day 13-14) — Loom walkthroughs, written runbook per workflow, on-call contact list for the 30-day window.
- 30-day post-launch operator support window. Bug fixes are free; new features get quoted as either change orders or rolled into a retainer.

## What's not in scope

- Source code custody — workflows live in your accounts (Zapier / Make / n8n / HubSpot). We do not host your business-critical automations on our infrastructure.
- Custom backend services or hosted code. If the buildout requires writing a backend API service, that's a separate engagement.
- Ongoing operations after the 30-day support window. Move to the AI Ops Partner retainer for maintenance, monitoring, and new requests.
- Manual data migration or data cleanup beyond what's needed to seed the workflows.
- Training your team on the underlying tools (HubSpot, Stripe, etc.) — we'll train them on the workflows we built.
- Compliance / legal / security audits.

## Timeline

| Day | Phase | What happens | Who |
|---|---|---|---|
| 1 | Discovery kickoff call | 60-90 min on Zoom; walk every tool, every current manual process, every priority. | Customer + operator |
| 2 | Scope confirmed (this doc) | We send the final scope.md. Customer replies `confirm` to start the build clock. | Both |
| 3-5 | Build wave 1 | Foundational workflows + integration wiring. | Operator |
| 6-9 | Build wave 2 | Dependent workflows (handoffs, escalations, digests). | Operator |
| 10-12 | Validation | Real data + customer-observed test runs per workflow. | Both |
| 13 | Handoff session | Loom walkthroughs + runbook delivery + Q&A call. | Both |
| 14 | Go-live | Workflows activated against production. SLA met. | Operator |
| 14-44 | Support window | Bug fixes, clarifications, minor adjustments. | Operator |
| 45 | Window closes | Open invitation to AI Ops Partner retainer. | Both |

## How we work

- **Async-first.** Daily progress note + screen-recorded walkthroughs at each milestone. Live calls only at kickoff, mid-build sync, and handoff.
- **Customer ownership.** All credentials, all account access stays with you. We never take admin custody — read/write API tokens with scoped permissions only.
- **Reversibility.** Every workflow we build has a documented kill-switch. You can turn it off without us.
- **No platform lock-in to us.** We don't host. We don't operate. You can take the runbooks to another shop on day 31 if you want.

## Pricing + payment

- **Fixed price:** $2,500-$3,000 (final number agreed at scope confirmation based on workflow count, integration complexity, and tool-credential complexity).
- **Payment terms:** 50% at scope confirmation, 50% at handoff. Stripe invoice, NET 7 from invoice date.
- **Refund policy:** Full refund if we cancel before Day 3. Pro-rated refund based on workflows delivered if you cancel after build starts. Zero refund after handoff.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Customer doesn't have admin access to a required tool | Identified in discovery (Day 1); blocks scope confirmation until resolved. |
| Tool API rate limits hit during build | We size workflows for normal volume; if customer's actual volume requires queue-and-batch architecture, scope expands as a change order. |
| Customer's data model changes mid-build (e.g. HubSpot field renames) | Day 12 validation re-tests every workflow against current production schema. |
| Workflow needs a feature the chosen platform doesn't support | Switch platform mid-build is in-scope cost; switch tool stack is a change order. |
| Customer adds requirements mid-build | Logged as change-order candidates; no silent absorption. Final build matches scope at confirmation, not at handoff. |

## Acceptance

This scope of work is accepted by the customer's reply email containing the word **"confirm"** (or equivalent affirmative) to the scope-confirmation email. The 14-day SLA clock starts at that moment.

— Hustleforge
