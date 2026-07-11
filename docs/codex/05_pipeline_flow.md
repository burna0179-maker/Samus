# 05 — Pipeline Flow

The operating diagram. Read this when you need to remember where in the
machine a thing happens.

---

## The end-to-end flow

```
                        ┌────────────────────────────┐
                        │  Prospecting workcell       │
                        │  Daily 07:30 scheduled task │
                        │  Geo-ring + vertical filter │
                        │  Industry-first scoring      │
                        │  Cross-zip dedup            │
                        └─────────────┬──────────────┘
                                      │
                                      ▼
                        ┌────────────────────────────┐
                        │  Owner enrichment cascade   │
                        │  homepage → /contact        │
                        │  → /about → FB mbasic       │
                        │  (intent: + CA SOS, G6+G8)  │
                        └─────────────┬──────────────┘
                                      │
                                      ▼
                        ┌────────────────────────────┐
                        │  SEO + security audit       │
                        │  audit_and_report pipeline  │
                        │  → Gap Report markdown      │
                        │  Top block reserved for     │
                        │  Stake Sentence (if any)    │
                        └─────────────┬──────────────┘
                                      │
                                      ▼
                        ┌────────────────────────────┐
                        │  Composite score            │
                        │  Need (low SEO) +           │
                        │  Visibility (high security) │
                        │  → qualified prospect       │
                        └─────────────┬──────────────┘
                                      │
                                      ▼
                        ┌────────────────────────────┐
                        │  Opportunity created         │
                        │  stake_sentence = "" (slot)  │
                        │  stage = "new"               │
                        └─────────────┬──────────────┘
                                      │
                       ┌──────────────┴──────────────┐
                       │   The HUMAN gate            │
                       │   Alex authors Stake Sentence│
                       │   via CLI or console        │
                       └──────────────┬──────────────┘
                                      │
                                      ▼
                        ┌────────────────────────────┐
                        │  Guard + cap + dedup         │
                        │  validate → record_use →     │
                        │  set_opportunity_stake       │
                        │  → artifact registered       │
                        └─────────────┬──────────────┘
                                      │
                                      ▼
                        ┌────────────────────────────┐
                        │  Proposal workcell          │
                        │  "remediation plan, X gaps,  │
                        │  costs $Y" — deterministic   │
                        │  prompt, ≤1 LLM call         │
                        └─────────────┬──────────────┘
                                      │
                                      ▼
                        ┌────────────────────────────┐
                        │  Callsheet generator         │
                        │  Opener + voicemail draft    │
                        │  Stake sentence prepended    │
                        │  VERBATIM, then ... pause    │
                        │  cue, then standard opener   │
                        └─────────────┬──────────────┘
                                      │
                                      ▼
                        ┌────────────────────────────┐
                        │  Outreach 3-step sequence   │
                        │  (a) email with proposal     │
                        │  (b) 24h follow-up           │
                        │  (c) 7-day → voicemail       │
                        │      DRAFT for Alex          │
                        │  Stake gate UNCONDITIONAL    │
                        │  CAN-SPAM footer required    │
                        └─────────────┬──────────────┘
                                      │
                                      ▼
                        ┌────────────────────────────┐
                        │  CRM Opportunity FSM        │
                        │  new → qualified →          │
                        │  proposal → negotiation →   │
                        │  closed_won / closed_lost   │
                        │  Every outcome (open/view/  │
                        │  click/reply) updates state │
                        └─────────────┬──────────────┘
                                      │
                                      ▼
                        ┌────────────────────────────┐
                        │  reward_density (intent G7) │
                        │  stage_advanced - llm_cost  │
                        │  - k*harm + terminal Stripe │
                        └────────────────────────────┘
```

---

## The 3-step outreach sequence

| Step | Trigger | Action | Notes |
|---|---|---|---|
| **1. Initial email** | Stake authored + Opportunity ready | Send email with Gap Report link + proposal | Stake sentence is the first line of the body |
| **2. Follow-up email** | 24h after step 1, no reply/click | Send follow-up referencing Gap Report findings | Same Stake sentence in header (deliberate consistency) |
| **3. Voicemail draft** | 7 days after step 2, no reply | Generate voicemail draft artifact for Alex to record | **NEVER auto-dial.** Artifact opens with Stake sentence + `...` pause cue + standard opener |

Any contact with no Stake sentence is **skipped at every step**, logged to
`host_artifacts/outreach_skipped_no_stake.jsonl`, and re-queued for the next
batch.

---

## The "needs warm path" detour (intent — G8)

When the pre-flight legitimacy filter ships, qualified prospects with no
warmth signal (no public RFP, no Chamber roster, no prior inbound, no
deterministic registry hit) divert to a `needs_warm_path` queue instead of
entering outreach. Alex's job on that queue: find a warmth signal *or* drop
the prospect. This is the structural fix for the Outsider's 0%-referral
problem.

---

## Artifact lineage per Opportunity

Each Opportunity accumulates these artifacts (kinds defined in
`backend/crm/models.py::ArtifactKind`):

| Kind | Source | When |
|---|---|---|
| `seo_audit` | `audit_and_report` raw output | At qualification |
| `seo_report` | Gap Report markdown (rendered) | At qualification |
| `stake_sentence` | Alex's authored sentence | At human gate |
| `proposal` | Proposal workcell output | After Stake authored |
| `call_sheet` | Callsheet builder | After proposal |
| `voicemail` | Voicemail draft script | At outreach step 3 |
| `content_draft` | Email body before send | At outreach step 1 |

These are linked to the Opportunity via `owner_entity_id` and are the
audit trail Alex's signature ultimately covers. If the Stake Sentence is
missing or any artifact is unverifiable, the chain breaks at that link.

---

## The places the Stake Sentence flows through

(Cross-reference for [chapter 03](03_stake_sentence.md) — used when
debugging a "stake not showing up" issue.)

| File | Function | Behavior |
|---|---|---|
| `backend/crm/stake_opportunity.py` | `attach_stake_sentence` | Author entry point (CLI + console reuse this) |
| `backend/crm/service.py` | `set_opportunity_stake` | Writes stake fields to Opportunity row |
| `backend/crm/service.py` | `list_opportunities_pending_stake` | Source for console "pending" view |
| `backend/outreach/campaign.py` | `compose_body` | Refuses without stake; renders at top of body |
| `backend/outreach/campaign.py` | `_load_stake_sentence_from_opportunity` | Pull from CRM by opp_id |
| `backend/outreach/run_campaign.py` | (batch dispatcher) | Logs skipped to `outreach_skipped_no_stake.jsonl` |
| `backend/seo/report.py` | `render_seo_report_markdown` | Renders stake as `> *...*` + rule |
| `backend/prospecting/callsheet.py` | `_opener`, `_voicemail` | Prepend verbatim with `...` pause |
| `backend/packs/operator_console/routes.py` | `GET /api/console/opportunities/pending_stake` | List view |
| `backend/packs/operator_console/routes.py` | `POST /api/console/opportunities/{id}/stake` | Author endpoint |

If you change the flow, update this table.
