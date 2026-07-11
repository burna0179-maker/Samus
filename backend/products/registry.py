"""Digital-product registry — the 7 LIVE SKUs on hustleforge.tech.

These products are billed today but have no fulfillment code (Stream 1 of the
2026-05-16 rebuild ships that loop). Registration follows the recovered
``ProductConfig`` pattern: each SKU is a dataclass instance appended to the
module-level ``PRODUCTS`` list and indexed by ``sku_id`` in ``_BY_ID``.

Stream 4 owns the Stripe catalog map (``backend/catalog/registry.py``). We
intentionally leave ``stripe_product_id`` / ``stripe_price_id`` as ``None``;
Stream 4 will populate them by joining on ``sku_id``.

Add-on micro-products live in their own table (``ADDONS``) — they share the
fulfillment chain but have a slimmer config because the deliverable is a
single short markdown report instead of a multi-file bundle.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Literal, Optional


ProductKind = Literal["playbook", "pack", "addon"]


@dataclass(frozen=True)
class ProductConfig:
    """One sellable digital SKU.

    ``artifact_relpath`` is the path *inside* ``backend/products/`` where the
    source content lives (a single .md for playbooks, a directory for packs,
    unused for add-ons). The fulfillment chain copies / renders this into the
    customer's artifact directory.
    """
    sku_id: str
    display_name: str
    price_usd_cents: int
    description: str
    kind: ProductKind
    artifact_relpath: str
    email_subject: str
    stripe_product_id: Optional[str] = None
    stripe_price_id: Optional[str] = None


@dataclass(frozen=True)
class AddOnConfig:
    """One add-on micro-product. Deliverable is a short markdown brief."""
    sku_id: str
    display_name: str
    price_usd_cents: int
    description: str
    deliverable_title: str
    deliverable_body: str
    email_subject: str
    stripe_product_id: Optional[str] = None
    stripe_price_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Playbook SKUs — single-file markdown deliverables
# ---------------------------------------------------------------------------

PLAYBOOK_LEAD_QUAL = ProductConfig(
    sku_id="playbook_lead_qual",
    display_name="Lead Qualification Workflow Playbook",
    price_usd_cents=39000,
    description=(
        "A 30/60/90 playbook for triaging inbound leads — qualification "
        "rubric, disqualification scripts, routing rules, and the KPI "
        "dashboard skeleton."
    ),
    kind="playbook",
    artifact_relpath="playbooks/lead_qualification.md",
    email_subject="Your Lead Qualification Workflow Playbook",
)

PLAYBOOK_CLIENT_ONBOARDING = ProductConfig(
    sku_id="playbook_client_onboarding",
    display_name="Client Onboarding Workflow Playbook",
    price_usd_cents=49000,
    description=(
        "End-to-end client onboarding — kickoff scripts, access checklist, "
        "milestone gates, the first-30-days nurture sequence, and the "
        "stakeholder one-pager template."
    ),
    kind="playbook",
    artifact_relpath="playbooks/client_onboarding.md",
    email_subject="Your Client Onboarding Workflow Playbook",
)

PLAYBOOK_SALES_FOLLOWUP = ProductConfig(
    sku_id="playbook_sales_followup",
    display_name="Sales Follow-Up Workflow Playbook",
    price_usd_cents=39000,
    description=(
        "The 7-touch follow-up cadence — channel mix, message templates, "
        "objection branches, the 'second-no' reactivation play, and the "
        "weekly review checklist."
    ),
    kind="playbook",
    artifact_relpath="playbooks/sales_followup.md",
    email_subject="Your Sales Follow-Up Workflow Playbook",
)

# ---------------------------------------------------------------------------
# Pack SKUs — multi-file bundles (README + templates)
# ---------------------------------------------------------------------------

PACK_CREATOR_QUICKSTART = ProductConfig(
    sku_id="pack_creator_quickstart",
    display_name="Creator QuickStart Pack",
    price_usd_cents=15000,
    description=(
        "Day-one operator kit for solo creators — content calendar template, "
        "Notion CRM skeleton, weekly review checklist, brand voice prompt, "
        "and the first 14 ready-to-post hooks."
    ),
    kind="pack",
    artifact_relpath="packs/creator_quickstart",
    email_subject="Your Creator QuickStart Pack",
)

PACK_CONTENT_FUNNEL = ProductConfig(
    sku_id="pack_content_funnel",
    display_name="Full Content Funnel Kit",
    price_usd_cents=30000,
    description=(
        "TOFU/MOFU/BOFU asset bundle — short-form hooks, long-form outline "
        "framework, lead-magnet skeleton, nurture email sequence, and the "
        "weekly funnel-health scorecard."
    ),
    kind="pack",
    artifact_relpath="packs/content_funnel",
    email_subject="Your Full Content Funnel Kit",
)

PACK_AUTHORITY_ACCELERATOR = ProductConfig(
    sku_id="pack_authority_accelerator",
    display_name="Authority Accelerator Pack",
    price_usd_cents=50000,
    description=(
        "Position-as-the-authority bundle — 12-month thought-leadership "
        "editorial plan, podcast pitch template, signature-framework "
        "worksheet, speaker-bureau one-pager, and PR outreach sequence."
    ),
    kind="pack",
    artifact_relpath="packs/authority_accelerator",
    email_subject="Your Authority Accelerator Pack",
)

# ---------------------------------------------------------------------------
# Add-on deliverable bodies
# Defined as module constants (not inline) so the AddOnConfig calls stay
# readable.
# ---------------------------------------------------------------------------

_STRIPE_HARDENING_BODY = """\
# Stripe Webhook Hardening — Implementation Brief

## Why this matters

Stripe will replay any webhook that doesn't return 2xx within 30 seconds.
Without HMAC verification + idempotency keys + replay-window enforcement,
you will eventually double-charge, double-fulfill, or accept a forged
event. This brief gives you the exact code shape for a hardened receiver.

## The four gates

### 1. HMAC-SHA256 signature verification

Reject every request whose `Stripe-Signature` header does not validate
against your endpoint's webhook secret (`whsec_...`). Use
`hmac.compare_digest` — never `==` — for the comparison.

### 2. Replay window guard

The `t=` timestamp in the signature header must be within 300 seconds of
your server clock. Replays older than that are dropped with a 400.

### 3. Idempotency key on dispatch

Compute `task_id = f"stripe-{session_id}"` and pass it as the
idempotency_key when you dispatch fulfillment. The gateway responds 409
for a duplicate — that's the all-clear: the first delivery is already in
flight.

### 4. Dead-letter routing

A failed dispatch must return a non-2xx so Stripe retries — but it should
also log the raw event into a DLQ table so the operator can replay manually
if the upstream service is down for hours.

## The verifier shape

Your verifier must **raise** on every failure mode (missing secret,
malformed header, expired timestamp, mismatched signature) — never return
a boolean. A boolean verifier is a fail-open footgun: one caller that
forgets to check the return value, or treats a falsy value as "skip", and
forged events sail straight through. Raise-on-failure makes "I didn't
verify" impossible to ignore — the request simply doesn't reach your
handler.

The four mechanics the verifier performs:

1. Reject immediately if the webhook secret or the `Stripe-Signature`
   header is empty.
2. Parse `t=<ts>` and every `v1=<hex>` entry out of the header.
3. Reject if `abs(now - t)` exceeds the 300-second replay window.
4. Recompute `HMAC-SHA256("{t}.{raw_body}", secret)` and compare it
   against each `v1` entry with `hmac.compare_digest` (constant-time —
   never `==`). No match in constant time -> raise.

A reference raise-on-failure implementation already ships in this
codebase: `backend/finance/webhook.py::verify_stripe_signature`. Copy
that one — do not hand-roll a boolean variant.

## 30-minute integration plan

1. Add a raise-on-failure verifier (per the shape above) to your receiver.
2. Wrap your `/webhooks/stripe` route in a guard that returns 400 on
   verification failure.
3. Generate `task_id` from `session.id` and pass as `idempotency_key` to
   your fulfillment dispatcher.
4. Add a DLQ writer in the `except` branch of your dispatch call — log
   the full event JSON plus the failure reason.
5. Run `stripe trigger checkout.session.completed` from the Stripe CLI
   twice in a row. You should see the first dispatch run, the second
   return 409 idempotent-replay.

## What's NOT in scope

- Building the fulfillment dispatcher itself.
- Migrating from a v1 to a v2 Stripe API version.
- Setting up the Stripe CLI / webhook secret rotation policy.

Out-of-scope items can be scoped as a follow-on engagement.
"""

_EMAIL_DELIVERABILITY_BODY = """\
# Email Deliverability Audit — Operator Brief

## What we check

1. **Authentication trifecta** — SPF record valid + no soft-fail; DKIM
   selectors present and signing; DMARC policy at minimum `p=none` with
   `rua=` reporting set up.
2. **Sending-domain reputation** — Google Postmaster Tools score; recent
   bounce + spam-complaint rates; subdomain isolation (marketing vs.
   transactional vs. cold).
3. **Content-side red flags** — image-to-text ratio, link shorteners,
   spammy phrases in subjects, missing list-unsubscribe header.
4. **Warmup posture** — if you're sending from a new domain or just
   migrated providers, are you ramping volume gradually or cold-dumping?

## The fix list (prioritized)

### P0 — fix this week

- Set DMARC to at minimum `p=none; rua=mailto:dmarc@yourdomain.com` if
  you don't have it at all. You can't see what's failing without it.
- Move cold outreach off your transactional sending domain. Get a
  separate cold-domain (e.g. `mail.yourbrand.tech`) with its own SPF +
  DKIM. Reputation contamination from cold spam-complaints is the #1
  cause of transactional mail landing in junk.
- Add a `List-Unsubscribe` header (both mailto and one-click forms) to
  every marketing send. Gmail and Yahoo now require this for bulk
  senders.

### P1 — fix this month

- Move DMARC from `p=none` to `p=quarantine` after 30 days of reporting
  shows your legitimate traffic is signing correctly.
- Set up Google Postmaster Tools + Microsoft SNDS so you can see your
  reputation, not guess it.
- Pre-warm any new sending IPs/domains: start at 50 sends/day for week 1,
  double weekly until you hit your target volume.

### P2 — fix this quarter

- Move from shared sender pool to dedicated IP if you're sending over
  100k/month.
- Implement BIMI (Brand Indicators for Message Identification) once DMARC
  is at `p=quarantine` or stricter — adds your logo to the inbox preview.

## The 30-minute audit you can run yourself

1. Pull the last 30 days of your bulk-send stats from your provider
   (SendGrid / Postmark / SES / etc.). Look for the spam-complaint rate.
   **Above 0.1% is a red flag; above 0.3% is an emergency.**
2. Send a test email to a Gmail address you control. Open the headers
   (Show Original). Confirm: SPF=PASS, DKIM=PASS, DMARC=PASS.
3. Visit https://www.mail-tester.com, send to the address it gives you,
   refresh the page. Target 9/10 or better.

## What's NOT in scope

- Email provider migration (SendGrid → Postmark, etc.).
- Drafting your actual email copy / templates.
- Long-term reputation rehab if your domain is already on blocklists —
  that's a 30-60-day engagement, not an audit.

We can scope the migration or rehab as follow-on work once the audit
results land.
"""

_AUTOMATION_HEALTH_BODY = """\
# Automation Health-Check — Operator Brief

## Why your automations need a checkup

Most operator automations are write-once-pray-forever. Six months in,
they've quietly broken in three places, billed you for 40k orphan
runs, and nobody's noticed because the alerts go to an email nobody
checks. This brief gives you the 5-axis inventory + a triage plan.

## The 5 axes

### 1. Inventory

List every active scenario across every platform (Zapier, Make, n8n,
Pipedream, native app integrations). Note: trigger, action chain,
estimated monthly runs.

### 2. Failure visibility

For each scenario: where does a failure surface? An email? A Slack
channel? A dashboard nobody opens? **If the answer is "nowhere",
that's your first P0.**

### 3. Cost-per-outcome

Compute: `monthly_runs * cost_per_run / business_outcomes`. A $50/month
Zapier scenario that triggers 20k times to produce 12 leads is a
$4.16-per-lead infrastructure tax — and that's before the human time
to investigate the inevitable breakage.

### 4. Redundancy / single-points-of-failure

Find scenarios where a single token expiry or a single rate-limit hit
breaks a downstream chain. Map the blast radius.

### 5. Lock-in risk

Score each scenario by: how long to rebuild on a different platform?
If your top-3 highest-value scenarios are all on the same vendor and
you're stuck with them, that's a strategic risk to surface.

## The triage matrix

| Axis | Green | Yellow | Red |
|---|---|---|---|
| Failure visibility | Alerted in real-time | Logged but no alert | Silent failures |
| Cost-per-outcome | <$1 / outcome | $1-$5 / outcome | >$5 / outcome |
| Redundancy | Has failover | Manual retry only | Hard dependency |
| Lock-in | <1 day to rebuild | 1-5 days | >5 days |

**Anything in the Red column gets a fix proposed in your delivery.**

## The 30-day action plan you'll receive

1. **Week 1** — instrument failure alerts on every Red-axis scenario.
2. **Week 2** — kill any scenario that costs more than $5 / outcome
   unless the outcome is unambiguously worth it.
3. **Week 3** — add an idempotency guard or retry wrapper to your
   top-3 highest-volume scenarios.
4. **Week 4** — write the migration plan for any Red-lock-in
   scenarios so you have an escape hatch before you need it.

## What's NOT in scope

- Actually executing the migration plan — that's a follow-on build.
- Custom scenario authoring beyond patching what's already there.
- Vendor negotiation (we'll flag costs, not redo your contracts).
"""

_CRM_HYGIENE_BODY = """\
# CRM Hygiene Sweep — Operator Brief

## The hidden tax of a dirty CRM

A CRM with 20% duplicate contacts, 30% stale records, and incomplete
qualification fields silently destroys forecast accuracy, follow-up
discipline, and rep productivity. Cleanup is a one-time investment
that pays for itself within the first month.

## The 4-axis sweep

### 1. Dedupe

Find every duplicate contact / company / deal. Match on: normalized
email, normalized domain, fuzzy name+phone, and (for deals) same-contact
+ overlapping open dates.

**Auto-merge candidates** = exact email or domain match → safe to merge
in a script.

**Manual-review candidates** = fuzzy match → need a human to confirm.

### 2. Stale records

A contact / lead is "stale" if no activity (call, email, meeting, note)
in the last 180 days AND not in an active deal. These should be either
archived or routed into a reactivation cadence.

### 3. Field completeness

For deals: is `amount`, `close_date`, `stage`, `owner`, and
`primary_contact` set on every open record? Missing fields make the
forecast lie.

For contacts: is `lifecycle_stage`, `qualification_score`, and
`source` set on everyone? Missing fields make attribution lie.

### 4. Ownership integrity

Every active record needs an owner who is still on the team. Orphan
records (owner deleted, owner deactivated) silently rot.

## The sweep deliverable

You'll receive a CSV per axis with: record IDs, the specific defect, and
a suggested action (merge / archive / fill / reassign). Plus a one-page
"executive view" that gives you the % rot per object so you can show your
team / your boss what we cleaned up.

## The 4-hour cleanup you can run yourself first

1. Export your CRM contacts. In a spreadsheet, sort by email. Highlight
   any email that appears more than once. Merge those manually.
2. Filter open deals where `amount` is blank. Fill them in or close them.
3. Filter contacts where `last_activity_date < today - 180 days` AND
   they're not in an active deal. Move them to a "Reactivation"
   list segment.
4. Audit your owner field. If a deleted user shows up as owner, reassign.

That gets you 60% of the way; the sweep brief gets you the other 40% +
the per-record action list.

## What's NOT in scope

- Building net-new automation in your CRM.
- Importing data from external sources / form integrations.
- Custom field schema design (separate engagement).
"""

_DASHBOARD_SETUP_BODY = """\
# Operator Dashboard Setup — Brief

## The principle: one page or it doesn't get checked

The single biggest dashboard mistake is having 14 of them. You will not
check 14 dashboards. You will check 1 dashboard, once a day, if it loads
in under 3 seconds and tells you whether anything is on fire.

This brief gives you the layout, the metrics, and the wiring to ship
that one page.

## The tile layout (5x3 grid, 15 tiles)

**Row 1 — Money this week**
- Cash in (Stripe / bank deposits)
- Cash out (variable costs, AP)
- Net runway in days

**Row 2 — Pipeline pulse**
- New leads (7-day count vs. prior 7-day)
- Conversion rate to first call
- Deals in-flight (open opportunities)

**Row 3 — Delivery + ops**
- Active customers
- Open support tickets
- Yesterday's failed jobs / alerts

**Row 4 — Marketing + voice**
- Site sessions (7-day)
- Top-3 traffic sources
- Cold-call dials made yesterday

**Row 5 — Strategic**
- 30-day MRR trend (sparkline)
- 7-day NPS / satisfaction
- "What I said I'd ship this week" — a free-text reminder tile

## The source-to-spreadsheet wiring

Pick **one** intermediate store (Google Sheet or Airtable). Every tile
pulls from one tab on that sheet. Every tab is populated by a single
Zap / Make scenario / n8n flow.

This separation is the trick — the dashboard never talks to a source
system directly. Source systems push to the sheet on schedule; the
dashboard reads only from the sheet. **That's how you ship in a week
instead of a quarter.**

Sheet structure:

```
[tab: money_weekly]      stripe_payouts.zap   -> sheet (daily 7am)
[tab: pipeline_pulse]    hubspot_export.zap   -> sheet (daily 6am)
[tab: delivery_ops]      crm_status_export.zap -> sheet (hourly)
[tab: marketing_voice]   ga4_export.zap       -> sheet (daily 8am)
[tab: strategic]         manual + automated (mixed cadence)
```

## The weekly review ritual

Every Friday, 30 minutes:

1. Open the dashboard.
2. For every tile in the **red zone** (you'll define thresholds per
   tile), write down one specific action to take next week.
3. Move those actions into next week's plan.
4. Take a screenshot. Save to `Operator Reviews/YYYY-WW.png`.
5. Done.

The screenshot archive is your historical record — six months in, you
can flip through them and see what trended.

## What you'll receive in the deliverable

- The Looker / Google Sheets / Notion template (whichever you specified).
- The Zap / Make blueprints for the 5 feeder scenarios.
- A worked example with placeholder data so you can see the layout.
- The Friday review checklist as a printable one-pager.

## What's NOT in scope

- Custom BI tool builds (Tableau / Looker Studio deep work).
- Predictive analytics or forecasting models.
- Multi-user permission systems — this is built for the solo operator.
"""


_404_AUDIT_BODY = """\
# Broken Link & 404 Audit — Operator Brief

## The hidden tax of broken links

Every 404 on your site is three losses stacked: a visitor who bounced, a
crawl-budget unit Google spent on nothing, and a backlink whose equity
just evaporated. Redirect chains add a fourth — every extra hop sheds
PageRank and adds latency that compounds on mobile. Most sites have
between 2% and 8% of their internal URLs broken at any given time and
the operator has no idea because nothing alerts on it.

This brief gives you the full-site crawl, the prioritized fix list, and
the redirect map ready to drop into your server config or CMS.

## The 5-axis crawl

### 1. Internal 404s

Every internal link pointing to a URL that returns a 4xx. These are the
highest-priority fixes — you control both the source and the target, so
there's no excuse for shipping them.

### 2. Soft 404s

Pages that return 200 OK but contain "page not found" content, an empty
template, or a zero-result search page. Google's Coverage report catches
most of these; a crawler catches the rest by flagging suspiciously thin
or template-only pages.

### 3. Redirect chains and loops

Any redirect path longer than 1 hop (`/a -> /b -> /c`) is a chain — flatten
to `/a -> /c` directly. Any path that cycles (`/a -> /b -> /a`) is a loop
and will time out browsers. Both leak link equity and slow first paint.

### 4. External broken links

Outbound links to dead third-party URLs. Lower priority than internal
fixes but they hurt user trust and can drag your perceived authority.

### 5. Orphan pages

URLs that exist (return 200, are in your sitemap or Search Console index)
but have zero internal links pointing to them. Either link them properly
or remove them from the sitemap — Google treats orphans as low-priority
and they dilute crawl budget.

## The fix list (prioritized)

### P0 — fix this week

- Every internal 404 on a page that has organic traffic in the last 90
  days. Pull the list from GSC Coverage > "Not found (404)" cross-referenced
  with pages that had impressions. These are bleeding right now.
- Any redirect loop. Browsers will give up after 20 hops and serve a
  blank error.
- Any broken link in your top navigation, footer, or checkout flow.
  These compound across every pageview.

### P1 — fix this month

- Flatten every redirect chain of 2+ hops to a single hop. Update the
  source links where you control them; update the redirect rules where
  you don't.
- Fix internal 404s on pages with backlinks (use Ahrefs or GSC Links
  report to find which dead URLs have inbound equity). 301 those to the
  best-matching live page.
- Resolve soft 404s — either return a real 404 status, or beef up the
  page so it's a legitimate 200.

### P2 — fix this quarter

- Audit external outbound links quarterly with a re-crawl. Replace or
  remove dead ones.
- Decide on every orphan page: link it from a category page, or remove
  it from `sitemap.xml` and let it drop out of the index naturally.

## The deliverable

You'll receive:

- A Screaming Frog or Sitebulb crawl export (CSV) with every defect
  flagged, the source page(s) linking to it, the response code, and
  the redirect chain if any.
- A prioritized fix list scoped to P0 / P1 / P2 as above.
- A drop-in `redirects.conf` (or `_redirects` for Netlify, or
  `vercel.json` rewrites, or your CMS's redirect import format) with
  every recommended 301 ready to deploy.
- A one-page executive summary: total URLs crawled, defects by category,
  estimated traffic recovery from P0 fixes.

## The 30-minute audit you can run yourself

1. Open Google Search Console > Coverage report. Filter for "Not found
   (404)". Export the CSV. Sort by impressions descending — the top 20
   are where to start.
2. Install the free version of Screaming Frog (crawls up to 500 URLs
   for free). Point it at your domain. Wait for the crawl to finish.
   Filter Response Codes > Client Error (4xx) — that's your internal
   404 list. Filter Response Codes > Redirection (3xx) and sort by
   "Redirects" column descending — anything > 1 is a chain.
3. For each URL in the GSC top-20 list, decide: does it map to a live
   page? If yes, write a 301. If no, return a real 404 and remove the
   internal links pointing to it.

That gets you 70% of the recoverable traffic. The full audit gets you
the rest plus the orphan + soft-404 + external-link sweep.

## What's NOT in scope

- Content rewrites on the destination pages of your redirects.
- New site architecture or URL-structure redesign.
- htaccess / nginx config debugging beyond the redirect rules we ship.
- Recovering rankings on pages that have been 404'd for over a year —
  that equity is mostly gone and is a separate SEO engagement.
"""

_DNS_HEALTH_BODY = """\
# DNS & Email Auth Setup — Implementation Brief

## Where this fits vs. the Deliverability Audit

The Email Deliverability Audit is **diagnostic** — it tells you what's
wrong with your sending posture and gives you a prioritized fix list.

This brief is **implementation** — we publish the actual DNS records,
verify them end-to-end, and hand you a sending domain that passes SPF,
DKIM, and DMARC on the first try. If you don't yet know whether you
have a problem, start with the audit. If you know you need the records
shipped, you're in the right place.

## What we publish

### 1. SPF (Sender Policy Framework)

One TXT record at the apex of your sending domain that lists every
service authorized to send mail as you. Capped at 10 DNS lookups —
exceed that and the record goes PermError and every recipient treats
you as unauthenticated.

Example for a Google Workspace + SendGrid + Postmark sender:

```
TYPE: TXT
HOST: @
VALUE: v=spf1 include:_spf.google.com include:sendgrid.net include:spf.mtasv.net ~all
```

The `~all` (soft fail) is the standard production posture. `-all`
(hard fail) is correct once you're certain every legitimate sender
is listed — flip after 30 days of DMARC reports show zero unexpected
sources.

### 2. DKIM (DomainKeys Identified Mail)

Per-sender public-key TXT records, each at the selector subdomain that
sender requires. Each ESP gives you the selector and the key value at
setup; you publish, they sign.

Example shape (SendGrid uses two CNAMEs pointing at their managed keys):

```
TYPE: CNAME
HOST: s1._domainkey
VALUE: s1.domainkey.u12345.wl.sendgrid.net

TYPE: CNAME
HOST: s2._domainkey
VALUE: s2.domainkey.u12345.wl.sendgrid.net
```

Google Workspace publishes a single TXT at `google._domainkey` instead.
We'll publish whatever shape each of your senders requires.

### 3. DMARC (Domain-based Message Authentication)

One TXT record at `_dmarc.yourdomain.com` that tells receivers what
to do when SPF + DKIM don't align, and where to send aggregate reports.

Phase-in posture (this is what we ship on day 1):

```
TYPE: TXT
HOST: _dmarc
VALUE: v=DMARC1; p=none; rua=mailto:dmarc-reports@yourdomain.com; ruf=mailto:dmarc-forensic@yourdomain.com; fo=1; adkim=r; aspf=r; pct=100
```

`p=none` means "monitor only, don't quarantine." After 30 days of clean
reports we move to `p=quarantine; pct=25` and ramp `pct` to 100 over
60 days, then to `p=reject`. We'll set up the reporting mailbox + a
free parser (Postmark's DMARC monitoring or dmarcian's free tier) as
part of the build.

### 4. MX records

We verify your MX records point at the correct inbound mail host and
that there's no stale fallback pointing at a deprovisioned server.
Misconfigured MX is the #1 cause of "we sent you a reply and it
bounced" — receivers can't deliver inbound mail to verify your
sending identity.

```
TYPE: MX
HOST: @
PRIORITY: 1   VALUE: aspmx.l.google.com
PRIORITY: 5   VALUE: alt1.aspmx.l.google.com
PRIORITY: 5   VALUE: alt2.aspmx.l.google.com
PRIORITY: 10  VALUE: alt3.aspmx.l.google.com
PRIORITY: 10  VALUE: alt4.aspmx.l.google.com
```

### 5. BIMI prep (optional, ship-ready)

Once DMARC is at `p=quarantine` or stricter, we publish a BIMI record
so your logo appears next to your name in supporting inboxes (Gmail,
Yahoo, Apple Mail). Requires a square SVG logo (SVG Tiny PS profile)
and, for Gmail, a Verified Mark Certificate ($1,500/yr from DigiCert
or Entrust — not bundled, we'll flag the cost and let you decide).

```
TYPE: TXT
HOST: default._bimi
VALUE: v=BIMI1; l=https://yourdomain.com/bimi/logo.svg; a=https://yourdomain.com/bimi/vmc.pem
```

If you skip the VMC, Apple Mail will still display the logo;
Gmail will not. We publish the record either way so you're ready when
you have the certificate.

## The deliverable

- Every record above, published in your DNS provider (Cloudflare, Route
  53, Namecheap, GoDaddy — whichever you use).
- An end-to-end verification: we send test mail from each authorized
  sender through `check-auth@verifier.port25.com` and `mail-tester.com`
  and confirm SPF=PASS, DKIM=PASS, DMARC=PASS for each.
- The DMARC reporting mailbox set up with a free parser dashboard so
  you can see who's sending as you.
- A 90-day ramp plan: when to flip `~all` to `-all`, when to move
  DMARC from `p=none` to `quarantine` to `reject`, and what to watch
  for at each step.
- A one-page DNS zone snapshot you can hand to anyone who asks "what's
  on our domain?"

## The 30-minute check you can run yourself

1. Go to https://mxtoolbox.com/SuperTool.aspx. Run these checks against
   your sending domain: SPF Lookup, DKIM Lookup (you'll need to know
   your selector — try `google`, `s1`, `s2`, `selector1`, `selector2`),
   DMARC Lookup, MX Lookup, Blacklist Check. Screenshot anything red.
2. From a terminal, run:
   ```
   dig +short TXT yourdomain.com
   dig +short TXT _dmarc.yourdomain.com
   dig +short MX yourdomain.com
   ```
   You should see exactly one SPF record (starts with `v=spf1`) and
   exactly one DMARC record (starts with `v=DMARC1`). Multiple SPF
   records = automatic fail at every receiver; multiple DMARC records
   = same.
3. Send a test message from each sending service to a Gmail address.
   Open the message > three-dot menu > "Show original". Confirm SPF,
   DKIM, and DMARC each say PASS.

If any of those three steps produces a red flag and you're not sure
how to fix it, that's the engagement.

## What's NOT in scope

- Migrating your inbound mail to a different provider (Google
  Workspace -> Microsoft 365, etc.).
- Buying or installing the Verified Mark Certificate for BIMI on Gmail.
- Reputation rehab if your domain is already on a major blocklist
  (Spamhaus, Barracuda, SORBS) — that's a 30-60-day separate engagement.
- Custom mail-flow rules / journaling / DLP inside your mailbox
  provider's admin console.
"""


# ---------------------------------------------------------------------------
# Add-on micro-products — short scoped deliverables
# Pricing band $29.99–$99.99 per parent brief.
# ---------------------------------------------------------------------------

ADDON_STRIPE_HARDENING = AddOnConfig(
    sku_id="addon_stripe_hardening",
    display_name="Stripe Webhook Hardening Brief",
    price_usd_cents=4999,
    description=(
        "Signed-payload verification, replay-window guard, idempotency keys, "
        "and dead-letter routing for your existing Stripe webhook handler."
    ),
    deliverable_title="Stripe Webhook Hardening — Implementation Brief",
    deliverable_body=_STRIPE_HARDENING_BODY,
    email_subject="Your Stripe Webhook Hardening Brief",
)

ADDON_EMAIL_DELIVERABILITY = AddOnConfig(
    sku_id="addon_email_deliverability",
    display_name="Email Deliverability Audit Brief",
    price_usd_cents=7999,
    description=(
        "SPF/DKIM/DMARC posture review, sending-domain reputation snapshot, "
        "and a prioritized fix list to lift inbox placement above 95%."
    ),
    deliverable_title="Email Deliverability Audit — Operator Brief",
    deliverable_body=_EMAIL_DELIVERABILITY_BODY,
    email_subject="Your Email Deliverability Audit Brief",
)

ADDON_AUTOMATION_HEALTH_CHECK = AddOnConfig(
    sku_id="addon_automation_health_check",
    display_name="Automation Health-Check Brief",
    price_usd_cents=4999,
    description=(
        "Inventory of your existing Zapier / Make / n8n scenarios, with "
        "a redundancy + failure-mode + cost-per-run analysis."
    ),
    deliverable_title="Automation Health-Check — Operator Brief",
    deliverable_body=_AUTOMATION_HEALTH_BODY,
    email_subject="Your Automation Health-Check Brief",
)

ADDON_CRM_HYGIENE_SWEEP = AddOnConfig(
    sku_id="addon_crm_hygiene_sweep",
    display_name="CRM Hygiene Sweep Brief",
    price_usd_cents=4999,
    description=(
        "Dedupe + stale-record + field-completeness scan of your CRM, "
        "with a prioritized cleanup script you can run yourself."
    ),
    deliverable_title="CRM Hygiene Sweep — Operator Brief",
    deliverable_body=_CRM_HYGIENE_BODY,
    email_subject="Your CRM Hygiene Sweep Brief",
)

ADDON_DASHBOARD_SETUP = AddOnConfig(
    sku_id="addon_dashboard_setup",
    display_name="Operator Dashboard Setup Brief",
    price_usd_cents=9999,
    description=(
        "Single-page operator dashboard skeleton — KPI tiles, source-to-"
        "spreadsheet wiring, and the weekly review ritual."
    ),
    deliverable_title="Operator Dashboard Setup — Brief",
    deliverable_body=_DASHBOARD_SETUP_BODY,
    email_subject="Your Operator Dashboard Setup Brief",
)

ADDON_404_AUDIT = AddOnConfig(
    sku_id="addon_404_audit",
    display_name="Broken Link & 404 Audit Brief",
    price_usd_cents=2999,
    description=(
        "Full-site crawl that identifies broken internal links, 404 pages, "
        "soft 404s, redirect chains/loops, and orphan pages — delivered with "
        "a prioritized fix list + drop-in redirects config."
    ),
    deliverable_title="Broken Link & 404 Audit — Operator Brief",
    deliverable_body=_404_AUDIT_BODY,
    email_subject="Your Broken Link & 404 Audit Brief",
)

ADDON_DNS_HEALTH = AddOnConfig(
    sku_id="addon_dns_health",
    display_name="DNS & Email Auth (SPF/DKIM/DMARC) Setup Brief",
    price_usd_cents=9999,
    description=(
        "End-to-end DNS hardening for your sending domain: SPF, DKIM, DMARC, "
        "MX, and BIMI prep — published, verified, and ramped over 90 days."
    ),
    deliverable_title="DNS & Email Auth Setup — Implementation Brief",
    deliverable_body=_DNS_HEALTH_BODY,
    email_subject="Your DNS & Email Auth Setup Brief",
)


# ---------------------------------------------------------------------------
# Registry indices
# ---------------------------------------------------------------------------

PRODUCTS: List[ProductConfig] = [
    PLAYBOOK_LEAD_QUAL,
    PLAYBOOK_CLIENT_ONBOARDING,
    PLAYBOOK_SALES_FOLLOWUP,
    PACK_CREATOR_QUICKSTART,
    PACK_CONTENT_FUNNEL,
    PACK_AUTHORITY_ACCELERATOR,
]

ADDONS: List[AddOnConfig] = [
    ADDON_STRIPE_HARDENING,
    ADDON_EMAIL_DELIVERABILITY,
    ADDON_AUTOMATION_HEALTH_CHECK,
    ADDON_CRM_HYGIENE_SWEEP,
    ADDON_DASHBOARD_SETUP,
    ADDON_404_AUDIT,
    ADDON_DNS_HEALTH,
]

_BY_ID: Dict[str, ProductConfig] = {p.sku_id: p for p in PRODUCTS}
_ADDONS_BY_ID: Dict[str, AddOnConfig] = {a.sku_id: a for a in ADDONS}


class UnknownSKUError(LookupError):
    """Raised by ``get_product`` / ``get_addon`` when sku_id is not registered."""


def get_product(sku_id: str) -> ProductConfig:
    """Look up a playbook/pack SKU; raise UnknownSKUError if not registered."""
    cfg = _BY_ID.get(sku_id)
    if cfg is None:
        raise UnknownSKUError(
            f"unknown product sku_id: {sku_id!r} "
            f"(registered: {sorted(_BY_ID)})"
        )
    return cfg


def get_addon(sku_id: str) -> AddOnConfig:
    """Look up an add-on SKU; raise UnknownSKUError if not registered."""
    cfg = _ADDONS_BY_ID.get(sku_id)
    if cfg is None:
        raise UnknownSKUError(
            f"unknown addon sku_id: {sku_id!r} "
            f"(registered: {sorted(_ADDONS_BY_ID)})"
        )
    return cfg


def get_any(sku_id: str) -> ProductConfig | AddOnConfig:
    """Look up either a product or an add-on; raise if neither matches."""
    if sku_id in _BY_ID:
        return _BY_ID[sku_id]
    if sku_id in _ADDONS_BY_ID:
        return _ADDONS_BY_ID[sku_id]
    raise UnknownSKUError(
        f"unknown sku_id: {sku_id!r} "
        f"(products: {sorted(_BY_ID)}, addons: {sorted(_ADDONS_BY_ID)})"
    )


def list_all() -> List[ProductConfig | AddOnConfig]:
    """Return all registered SKUs (products first, then add-ons)."""
    return [*PRODUCTS, *ADDONS]


def products_root() -> Path:
    """Directory holding the source content for playbooks + packs."""
    return Path(__file__).resolve().parent


__all__ = [
    "ProductConfig",
    "AddOnConfig",
    "UnknownSKUError",
    "PRODUCTS",
    "ADDONS",
    "get_product",
    "get_addon",
    "get_any",
    "list_all",
    "products_root",
]
