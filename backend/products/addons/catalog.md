# Automation Add-Ons Catalog

Narrowly-scoped micro-services for operators who need ONE specific
thing fixed without buying a full engagement. Each add-on is a
single deliverable, fixed price, ~3-5 day turnaround.

These are the operator's "I just need help with this one thing" tier.
The deliverable in every case is a structured markdown brief shipped
to your inbox, plus 1 round of clarifying Q&A.

## The 5 add-ons

### 1. Stripe Webhook Hardening Brief — $49.99

`addon_stripe_webhook_hardening`

For: operators running a Stripe-billed product whose webhook handler
is a single happy-path receiver with no signature verification, no
replay protection, and no DLQ.

You get: a drop-in code skeleton for HMAC-SHA256 verification +
replay-window guard + idempotency-key dispatch + DLQ writer + a
30-minute integration plan that gets you from "vulnerable" to
"hardened" in one session.

Scope boundary: does not include building your fulfillment
dispatcher itself, migrating Stripe API versions, or rotating
webhook secrets on a schedule.

### 2. Email Deliverability Audit Brief — $79.99

`addon_email_deliverability_audit`

For: operators whose transactional or marketing email is landing
in spam, who don't know why, and who don't want to commit to a
30-day deliverability rehab engagement to find out.

You get: a 4-axis audit (SPF/DKIM/DMARC, sending reputation, content
red flags, warmup posture) + a prioritized P0/P1/P2 fix list + a
30-minute self-audit you can run before our brief lands.

Scope boundary: does not include actually executing a long-term
reputation rehab (that's a separate 30-60 day engagement), email
provider migration, or copy / template authoring.

### 3. Automation Health-Check Brief — $49.99

`addon_automation_health_check`

For: operators running 5-50 Zapier / Make / n8n scenarios who
suspect they have silent failures, runaway runs, or cost-per-outcome
problems but haven't sat down to do the inventory.

You get: a 5-axis inventory framework (inventory, failure
visibility, cost-per-outcome, redundancy, lock-in risk) + a triage
matrix + a 30-day action plan to address Red-axis findings.

Scope boundary: does not include actually executing the migration
plan, custom new-scenario authoring, or vendor contract negotiation.

### 4. CRM Hygiene Sweep Brief — $49.99

`addon_crm_hygiene_sweep`

For: operators whose CRM has accumulated 12-24 months of duplicates,
stale records, missing fields, and orphan-owner records that are
quietly destroying forecast accuracy and follow-up discipline.

You get: a 4-axis cleanup framework (dedupe, stale records, field
completeness, ownership integrity) + a CSV per axis identifying
specific defects + a 4-hour self-cleanup you can run before our
brief lands.

Scope boundary: does not include building new CRM automation,
importing external data, or designing custom field schemas.

### 5. Operator Dashboard Setup Brief — $99.99

`addon_dashboard_setup`

For: operators who are checking 14 different dashboards (or zero
dashboards) and want a single-page daily-check view that loads in
under 3 seconds and tells them whether anything is on fire.

You get: a 15-tile (5x3 grid) dashboard layout + a source-to-sheet
wiring blueprint (5 feeder Zaps / Make scenarios) + the Looker /
Google Sheets / Notion template + the Friday review checklist.

Scope boundary: does not include custom BI tool builds, predictive
analytics, or multi-user permission system design.

## How to order

Each add-on is a Stripe checkout — the SKU IDs above are the
canonical identifiers used by the fulfillment chain.

Delivery cycle:

1. Stripe webhook fires `checkout.session.completed`.
2. Fulfillment chain runs `fulfill_digital_product(sku_id=...)`.
3. Customer record created / updated; state advanced to
   `in_delivery`.
4. Markdown brief rendered to
   `<SAMUS_ARTIFACT_ROOT>/customers/<slug>/<sku>.md`.
5. Email sent with the brief inlined in the body — no PDF, no
   marketing wrapper, just the document.
6. State advanced to `delivered`.

Typical end-to-end: under 60 seconds from Stripe webhook to inbox.

## What's NOT an add-on

Some things look like they should be add-ons but aren't. A few
that get asked for and don't qualify:

- **"Just take a look at my marketing"** — too unscoped. Use a
  paid discovery call instead.
- **"Build me a Stripe webhook from scratch"** — out of scope; the
  add-on hardens an existing one. New build is a separate engagement.
- **"Audit my GA4"** — different topic; not on the menu yet.
- **"Fix my [specific live problem]"** — emergency engagements
  aren't add-ons. Book a 60-min troubleshooting call.

## Adding a new add-on

Each add-on requires three things to ship:

1. A new `AddOnConfig` entry in `backend/products/registry.py`
   (sku_id, display_name, price, description, deliverable title,
   deliverable body, email subject).
2. Listing here in `catalog.md`.
3. A Stripe product + price (handed off to Stream 4's catalog
   wiring — not in this stream's scope).

The fulfillment chain handles everything else — no code changes
needed per new add-on.

## Refund policy

If an add-on brief doesn't address the specific problem you
purchased it for, reply to the delivery email within 14 days with
the specific gap. We'll either ship a v2 brief at no charge or
refund in full. Never both.

---

*Each add-on includes one round of clarifying Q&A. For anything
beyond that, book a paid discovery call.*
