# 06 — Modules Map

Where to look when something breaks. Each entry: path → one-line WHY → key
public symbols.

---

## `backend/common/`

| Module | WHY | Key symbols |
|---|---|---|
| `stake_sentence_budget.py` | Daily cap on Stake Sentence authorings — fail-CLOSED ledger | `count_today`, `remaining_today`, `record_use`, `reset_today`, `StakeSentenceBudgetUnavailable` |
| `stake_sentence_guard.py` | Anti-template + dedup validator on every Stake Sentence | `validate_stake_sentence`, `is_duplicate`, `record_hash`, `reset_dedup_ledger`, `StakeSentenceRejected`, `STAKE_SENTENCE_BANNED_PHRASES` |
| `llm_client.py` | Anthropic wrapper enforcing daily $-cap + per-workcell quotas + per-job ≤1 call ceiling | `anthropic_messages` |
| `llm_global_budget.py` | DDB+JSON ledger for the LLM $-cap (fail-OPEN on persistence) | (parallel structure to stake_sentence_budget) |

---

## `backend/crm/`

| Module | WHY | Key symbols |
|---|---|---|
| `models.py` | Pydantic models for Prospect/Opportunity/Artifact + FSM stages + validators (incl. Stake guard hook) | `Opportunity`, `OpportunityStage`, `CreateOpportunityRequest`, `Artifact`, `ArtifactKind`, `CreateArtifactRequest` |
| `service.py` | Repository functions over the CRM tables + Stake setters and pending-list query | `create_opportunity`, `set_opportunity_stake`, `list_opportunities_pending_stake`, `get_opportunity` |
| `stake_opportunity.py` | CLI + reusable function to author a Stake Sentence end-to-end (cap → guard → dedup → write → artifact) | `attach_stake_sentence`, `main` (CLI entry) |
| `create_opportunity.py` | Programmatic Opportunity creation entrypoint (used by lead conversion, finance, strategy, etc.) | `create_opportunity` |

---

## `backend/outreach/`

| Module | WHY | Key symbols |
|---|---|---|
| `campaign.py` | Turns Apollo contacts into compliant, Stake-gated email message requests | `CampaignConfig`, `CampaignResult`, `compose_body`, `build_messages`, `load_suppression`, `OutreachStakeMissing`, `_load_stake_sentence_from_opportunity` |
| `run_campaign.py` | CLI dispatcher for the 3-step sequence — Apollo → unlock → build → send + skip-and-log if no Stake | `--stake-sentences-json`, `--opportunity-map-json` |
| `apollo_source.py` | Apollo People-Search adapter — returns `ApolloContact` objects | `ApolloContact` |
| `fsm.py` | Outreach state machine (open → pitch → engage → close_attempt → exit) — separate from CRM Opportunity FSM | `next_state` |
| `models.py` | Pydantic types for outreach messages | `OutreachMessageRequest` |
| `service.py` | Send-message dispatcher (SES integration) | `send_message` |

---

## `backend/seo/`

| Module | WHY | Key symbols |
|---|---|---|
| `report.py` | Renders the Gap Report markdown — stake sentence as italicized first block when supplied | `render_seo_report_markdown`, `write_seo_report`, `customer_slug_from_url` |
| `audit.py` | Crawler + passive security probes | (various) |
| `optimize.py` | Recommendations from audit output | (various) |
| `content.py` | Content-gap analysis | (various) |

---

## `backend/prospecting/`

| Module | WHY | Key symbols |
|---|---|---|
| `callsheet.py` | Builds opener + voicemail drafts for Alex's human-read call leg; stake sentence threads through all 6 builders | `build_call_sheet`, `build_call_sheet_with_llm`, `_opener`, `_voicemail` |
| `scoring.py` | Composite Need+Visibility scoring on raw prospect data | (various) |
| `enrichment.py` | Owner-enrichment cascade (homepage → contact → about → FB mbasic) | (various) |
| `Run-ProspectingDaily.ps1` | Host scheduled task entrypoint (07:30 daily) | n/a |

---

## `backend/packs/operator_console/`

| Module | WHY | Key symbols |
|---|---|---|
| `routes.py` | HTTP surface for the operator console — incl. Stake authoring endpoints | `GET /api/console/opportunities/pending_stake`, `POST /api/console/opportunities/{id}/stake` |
| `pod.py` | Console state holder (in-memory between requests) | (pod state) |
| `history.py` | Console interaction log | (various) |

---

## `backend/gateway/`

| Module | WHY | Key symbols |
|---|---|---|
| `app.py` | FastAPI entrypoint, wires routers, sets lifespan | `app` |

---

## `backend/common/llm_*`

See `backend/common/` above. Re-emphasis here because LLM-budget mistakes
are the most common source of "Samus burned the budget" tickets:
`anthropic_messages` is the **only** path to the LLM. Lint should fail any
direct `import anthropic` outside `llm_client.py`.

---

## Recovery directory

`recovery/` holds historical or future-port modules NOT in the live stack:

| File | Status |
|---|---|
| `prospect_schema.py` | Live — DDB schema reference for opportunities (incl. 3 stake columns added 2026-05-30) |
| `intelligence/`, `realtime_adaptive/`, `fulfillment_worker_v2.py` etc. | Historical — designs not in current build |

If you find code referencing `recovery/*` from the live stack, that's a
bug.

---

## Workcell map (one-line role each)

| Workcell | Role |
|---|---|
| `samus-prospecting` | Surfaces prospects from geo rings + verticals, enriches owner contact |
| `samus-fulfillment` | Once a deal closes, drives the work product (planner + worker) |
| `samus-crm` | Customer pipeline FSM, opportunity tracking, artifact ledger |
| `samus-outreach` | Composes + sends Stake-gated cold emails, schedules follow-ups |
| `samus-finance` | Stripe + lender data, morning briefing, COGS registry |
| `samus-strategy` | Bandit-driven prioritization (intent: outcome attribution feeding G7) |
| `samus-voice` | Voicemail/dialer endpoint (Samus drafts; Alex invokes) |
| `samus-knowledge` | (Reserved — Chroma was dropped in current iteration) |
| `samus-recovery` | Backup/restore tooling |

---

## When the modules map disagrees with the code

The map is the question, the code is the answer. If code added a module or
moved a symbol, **update this chapter in the same commit**. A drifting
modules map is a faster path to confusion than no map at all.
