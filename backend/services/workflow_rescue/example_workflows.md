# Workflow Rescue — Worked Examples

Seven 48-hour rescue patterns spanning the verticals that most rescue requests come from. Each one is a real-shaped build that fit inside the scope gates (≤ 5 steps, ≤ 3 external tools, ≤ 3 templates) and shipped a *production-ready workflow that saves hours every week from day one.*

Use these two ways:

1. **When scoping a new Rescue** — find the closest match here and cite the specifics to the prospect. ("We did this for a roofing crew last month. Pulled their booking calendar straight into the CRM, killed about 6 hours of admin a week.") Specificity sells better than capability claims.
2. **When building** — branch from the closest match. Don't start from a blank canvas. Most rescues are one of these patterns with config tweaks.

Each example follows the same shape: the vertical, the manual task that was killing them, what got built in 48 hours, and the result.

---

### Home services (HVAC / plumbing / roofing): every booking lived in a different place

**The manual task that was killing them:** The owner was the dispatcher. Customers booked through a Calendly link, paid a deposit through a Stripe invoice he sent by hand, and then he re-typed every job into a shared Google Sheet so his two field techs knew where to go. Three systems, zero connections, every booking was a 10-minute admin tax — and he was missing about one job per week from typos or forgetting to invoice.

**What we built (48 hours):**
- Calendly "booking created" webhook as the trigger
- Auto-generated Stripe invoice with the deposit amount, hosted URL emailed to the customer
- New row appended to the dispatch sheet with job address, time, customer phone, and tech assignment
- SMS to the assigned tech via Twilio: "New job Thu 10am — 142 Oak St — [customer phone]"
- Failure alerts routed to the owner's Discord (so he knew the second anything broke)

**Result:** 6+ hours/week back on admin. Zero missed invoices in the 30-day support window (vs. ~1/week before). Owner stopped being the dispatcher and went back to actually running the company.

---

### E-commerce (Shopify storefront): the bookkeeper was a manual Shopify export every morning

**The manual task that was killing them:** Every morning at 7am the bookkeeper would ping the founder for yesterday's order count, gross sales, and refunds. The founder was copying numbers out of the Shopify admin by hand into an email. 15 minutes a day, 7 days a week, every week, forever. He hated it more than any other 15-minute task in the business.

**What we built (48 hours):**
- Scheduled trigger fires at 6am every day
- Shopify Orders API pulls the previous 24 hours of orders (with proper timezone-aware `created_at_min` / `created_at_max`)
- Refunds + gross sales totaled, appended as a new row to a Google Sheet (running history, not overwrite)
- Summary email sent to the bookkeeper with the day's numbers + a link to the sheet for line-item detail
- Slack ping to the founder only if the workflow fails

**Result:** ~2 hours/week back. Bookkeeper stopped chasing the founder. Founder stopped opening Shopify before coffee. The sheet itself became a side-benefit dataset — month-over-month trend visible at a glance.

---

### Agency (marketing / creative): leads sat in the founder's inbox until they went cold

**The manual task that was killing them:** Squarespace contact form went to the founder's inbox. She'd try to triage between client calls and miss half the new leads for hours — sometimes a full day. Her best estimate was that lead-to-first-contact time was averaging 8+ hours, and she was watching her close rate drop because faster competitors were calling within 30 minutes.

**What we built (48 hours):**
- Squarespace form submission as the trigger
- Contact + Deal record created in HubSpot in one API hop (Associations endpoint — single failure surface, not two)
- Slack ping to the #sales-leads channel with the lead's name, email, and form body, formatted so a human can see everything without clicking through
- Auto-reply email from her domain: "Got your message — I'll be in touch within 2 hours."
- Discord failure alerts to her ops contact

**Result:** Lead-to-first-contact time dropped from 8+ hours to under 30 minutes. Close rate on inbound leads recovered ~22% over the next 60 days. She stopped checking her inbox between calls.

---

### B2B SaaS: support emails were getting buried under product alerts

**The manual task that was killing them:** Their support@ inbox got everything — real support tickets, automated platform alerts, marketing noise, the works. The two-person team was missing actual customer issues because the volume was overwhelming. Anything with "refund" or "broken" needed to surface immediately and they were missing them.

**What we built (48 hours):**
- Gmail label-based trigger fires only when an inbound email is labeled with their existing routing label
- Keyword filter step matches "refund," "broken," "not working," "cancel" in the subject or first 200 chars
- Matching emails create a Linear issue tagged `urgent-support` with the email body + sender as the issue description
- Auto-reply to the customer: "We saw your message and a real human will respond within 4 hours. Reference #[ticket-id]."
- Discord ping to the on-call engineer

**Result:** Zero missed urgent tickets in the 30-day window (vs. ~3-4/month before). Support response time on urgent issues dropped from "sometimes the next day" to under 2 hours. The team stopped feeling like they were drowning in the inbox.

---

### Real estate (boutique brokerage): new leads sat in a sheet while reps drove around

**The manual task that was killing them:** Their cold-call list lived in a shared Google Sheet. Marketing would add new leads from listing-page scrapes and Zillow alerts. The reps wouldn't see new leads until they were back at a desk — which for a real-estate team is sometimes the end of the day. Hot leads were going cold because nobody was getting touched within the first hour.

**What we built (48 hours):**
- Apps Script trigger on the lead sheet — fires the moment a new row is added (more reliable than Zapier's 5-15 min polling)
- Territory → rep lookup pulled from a separate mapping sheet (so it stays maintainable when reps change)
- Twilio SMS to the assigned rep with lead name, city, source, and a "reply Y to claim" handshake
- Sheet auto-updated with `claimed_by` + `claimed_at` once the rep replies
- Failure alerts to the broker's email

**Result:** Lead-to-first-contact time dropped from "end of day" to median 12 minutes. The broker said it was the first time her reps stopped fighting over leads — the assignment was automatic, the handshake was clean.

---

### Healthcare admin (small clinic): patient intake was a paper-to-spreadsheet manual relay

**The manual task that was killing them:** New patients filled out a JotForm intake. The office manager would print it, walk it to the nurse, then re-type the highlights into the practice's scheduling spreadsheet. Every intake was a 12-minute hand-relay and the form details would sometimes be transcribed wrong (allergies, medications) — which is the one place you really don't want transcription errors.

**What we built (48 hours):**
- JotForm submission as the trigger
- New row appended to the scheduling sheet with name, DOB, reason for visit, allergies, and current medications — pulled directly from the form fields, no manual retype
- PDF copy of the full intake stored in the clinic's Google Drive in a patient-named folder
- Email to the office manager + nurse: "New intake — [name] — [reason for visit]" with the Drive link
- No PHI in any external notifications (Slack/Discord) — all sensitive data stays inside their Google Workspace

**Result:** 4-5 hours/week back for the office manager. Zero transcription errors on allergies or medications in the 30-day window. Charts were ready before the patient arrived, not during the appointment.

---

### Professional services (accounting firm): client onboarding was 7 manual steps the partner kept forgetting

**The manual task that was killing them:** Every new client engagement required: send the engagement letter, create a folder in Drive, create a row in the client tracker, send the welcome email with portal access, schedule the kickoff call, send the doc-request checklist, and ping the assigned associate on Slack. The partner was doing this himself, would forget 1-2 steps every time, and had a running joke that "onboarding is where good intentions go to die."

**What we built (48 hours):**
- Stripe `checkout.session.completed` webhook (engagement letter was sold as a Stripe Checkout) as the trigger
- Client folder created in Drive from a template
- Row appended to the client tracker sheet with engagement type pulled from Stripe metadata
- Welcome email sent with portal access link + doc-request checklist (templated from engagement type)
- Slack DM to the assigned associate with the client name + Drive folder link

**Result:** 7 manual steps collapsed to 1 (the partner takes payment, the rest happens). ~3 hours/week back. Zero missed onboarding steps in the 30-day window. The partner started taking on more clients because the friction of starting was gone.

---

## Out-of-scope examples (these are Buildouts, not Rescues)

These look like Rescues to a prospect but blow the gates the second you map them. Don't try to cram them into 48 hours. Quote the Workflow System Buildout SKU instead:

- **"Build a full lead-routing engine** — scores leads, distributes to 4 territories, syncs Salesforce + HubSpot, escalates stale leads, sends weekly reports." → 8+ steps, 4+ tools. **Buildout.**
- **"Set up our entire e-commerce ops** — order intake, inventory sync, refund handling, customer notifications, support ticket routing." → 5 distinct workflows. **Buildout.**
- **"Migrate everything from our Zapier mess into n8n** and consolidate the 12 workflows we have today." → Migration is its own engagement. **Buildout.**
- **"Replace our manual sales process** — sequenced outreach, reply detection, qualification scoring, calendar handoff to AEs, post-meeting follow-ups." → Multiple workflows + AI-in-the-loop. **Buildout.**

When you reject a request as Buildout-scope, do it the same way you reject a non-viable Phase 1 audit: be direct, name the specific gate that pushed it over, and offer the correct SKU. The customer who needs a Buildout is better-served knowing that today than discovering it on Hour 36 of a Rescue that was never going to fit.
