# Staffing / Hiring Campaign — design memo

**Status:** design only, no code shipped. When the operator says "start
<sample-client> hiring", this memo is the drop-in blueprint.

**Sibling template pattern:** mirrors
`school_enrollment_campaign.yaml` (accelerated) and
`school_phased_maintenance.yaml` (phased) — a declarative graph of
campaign nodes routed to existing workcell capabilities. NO new workcell
code needed for the first version; every node either reuses a capability
that already exists or lands as an approval-gated content-generation
node.

## Why put this on the calendar

Hiring is fundamentally a scheduled activity — every step has a
deadline, an interview slot, or an offer window. The
[`calendar_projection`](../../intake/calendar_projection.py) module was
built specifically so this campaign can push its schedule
onto samushustleforge@'s calendar as first-class planner entries.

Every deadline / slot below becomes a Google Calendar event with a tagged
`extendedProperties.private.projection_kind = "hiring_milestone"` and a
`[HIRING]` title prefix, so the operator's calendar reads at a glance:

```
[HIRING] <sample-client>: Posting closes — Aug 15
[HIRING] <sample-client>: Interview slot — Sample Vendor — Aug 18 14:00
[HIRING] <sample-client>: Shortlist due to client — Aug 20
[HIRING] <sample-client>: Offer window ends — Aug 30
```

## Template shape

```yaml
campaign_template:
  template_id: staffing_hiring_campaign
  vertical: staffing
  name: Staffing / Hiring Campaign
  version: 1.0.0

  required_inputs:
    - client_id
    - company_name
    - role_title              # e.g. "Caregiver — Sacramento"
    - role_description        # long-form job description
    - target_geography        # for posting distribution
    - approval_contact
    - posting_close_at        # absolute ISO date/time -> [HIRING] event
    - shortlist_due_at        # absolute ISO date/time -> [HIRING] event
    - offer_window_end_at     # absolute ISO date/time -> [HIRING] event
    - screening_criteria      # list; drives applicant scoring
    - salary_range            # for the posting
```

## Node types (all use existing capabilities)

| Node id | Type | Workcell / capability | Approval | Calendar projection |
|---|---|---|---|---|
| `intake_role_brief` | `artifact_ingest` | scaffold / generate_assets | none | — |
| `write_job_posting` | `content_generation` | scaffold / generate_assets | operator | — |
| `publish_job_posting` | `public_action` | outreach / send_message | client | — |
| `posting_closes` | (deadline marker) | — | — | **projection: posting_close_at** |
| `receive_applications` | `metrics_collection` | campaigns / update_kpis | none | — |
| `score_applicants` | `content_generation` | scaffold / generate_assets | operator | — |
| `interview_slot` | (calendar-only) | — | operator | **projection: per candidate** |
| `shortlist_due` | (deadline marker) | — | — | **projection: shortlist_due_at** |
| `present_shortlist_to_client` | `content_generation` | scaffold / generate_assets | operator | — |
| `offer_window_ends` | (deadline marker) | — | — | **projection: offer_window_end_at** |
| `notify_finalists` | `external_outreach` | outreach / send_message | operator | — |
| `onboard_hire` | `funnel_plan` | proposal / generate_proposal | operator | — |
| `monthly_hiring_report` | `reporting` | campaigns / generate_report | operator | — |

## Instance file example

`clients/sample_cleaning/campaign.yaml`

```yaml
campaign_instance:
  campaign_id: sample_cleaning_hiring_2026
  client_id: sample_cleaning
  template_id: staffing_hiring_campaign
  vertical: staffing

  inputs:
    company_name: <sample-client>
    role_title: "Caregiver — Sacramento"
    role_description: |
      Full/part-time in-home caregivers ...
    target_geography: "Sacramento, CA"
    approval_contact: "operations@example.com"
    posting_close_at:    "2026-08-15T23:59:00-07:00"
    shortlist_due_at:    "2026-08-20T17:00:00-07:00"
    offer_window_end_at: "2026-08-30T17:00:00-07:00"
    screening_criteria:
      - "valid driver's license"
      - "background check clear"
      - "6+ months caregiving experience"
    salary_range: "$18-24/hr DOE"
```

## Calendar projection wiring

When `orchestrator.create_campaign(instance)` fires for a
`staffing_hiring_campaign` instance, it iterates over `inputs.*_at`
fields and calls
[`calendar_projection.project_event`](../../intake/calendar_projection.py)
with `projection_kind="hiring_milestone"`. Each projection is idempotent
via `source_id = f"{campaign_id}/{input_key}"` — a second create_campaign
call finds and reuses the existing event.

Interview slots are projected as they're scheduled (either operator-
placed via the two-way sync, or produced by a future automation node
that turns applicant availability into concrete slots).

## Business events emitted

- `calendar.event_scheduled` — once per projected deadline (already in
  taxonomy)
- `calendar.event_completed` — when a deadline passes (already in
  taxonomy)
- No new taxonomy required for v1. Later, if we want a per-hiring-stage
  timeline view, add `hiring.applicant_scored`, `hiring.shortlist_sent`,
  `hiring.offer_accepted`.

## What's needed when the operator greenlights this

1. Create `clients/sample_cleaning/campaign.yaml` (bind to the
   template)
2. Create `backend/campaigns/templates/staffing_hiring_campaign.yaml`
   (this shape)
3. Add `sample_cleaning` to `client_directory` recognition (auto
   via campaign.yaml presence)
4. Add a small hook in `orchestrator.create_campaign` (or a new
   `_project_hiring_milestones` helper called from there) that walks
   `inputs.*_at` and projects each
5. Optionally: add `staffing` to the known verticals list in
   `campaign_registry` if that gets tightened

Everything else (client_correspondence, intent routing, forwarder,
customer_service track for issues with a hire) already works verbatim
for hiring the same way it works for enrollment — the client
directory + intent router + planner calendar are vertical-agnostic.

## Timeline sketch (operator drives greenlight)

```
op says "start <sample-client> hiring"
    → create template YAML (30 min)
    → create instance YAML (5 min)
    → add project_hiring_milestones hook (~40 lines)
    → tests (~2h)
    → rebuild + verify projection lands on calendar (~15 min)
```

Total: sub-half-day when we get the go-ahead.
