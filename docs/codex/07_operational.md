# 07 — Operational Knobs

Every env var, DDB table, file ledger, CLI, and HTTP endpoint Samus
exposes. Reference when you need to seed, tune, or audit.

---

## Environment variables

### Stake Sentence

| Var | Default | Purpose |
|---|---|---|
| `SAMUS_STAKE_SENTENCE_DAILY_CAP` | `10` | Hard ceiling on Stake Sentences recorded per UTC day |
| `SAMUS_STAKE_SENTENCE_DEDUP_PATH` | `/opt/samus/data/stake_sentence_dedup.json` | Override location of dedup hash ledger |
| `SAMUS_STAKE_SENTENCE_BUDGET_JSON` | `/opt/samus/data/stake_sentence_budget.json` | JSON fallback when DDB unavailable |
| `SAMUS_OPERATOR` | `alex` | Author attribution string written to `stake_sentence_authored_by` |

### Outreach (CAN-SPAM)

| Var | Default | Purpose |
|---|---|---|
| `SAMUS_OUTREACH_POSTAL_ADDRESS` | (unset, refuses to build) | Physical postal address in compliance footer |
| `SAMUS_OUTREACH_UNSUBSCRIBE_URL` | (unset, refuses to build) | Opt-out URL in compliance footer |

Both required. Campaign builder fails closed if either is empty.

### LLM budget

| Var | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | (DPAPI) | Anthropic credentials; LLM degrades to template if missing |
| `SAMUS_LLM_DAILY_BUDGET_USD` | `1.00` | Global $-cap per UTC day |
| `SAMUS_LLM_GLOBAL_BUDGET_JSON` | `/opt/samus/data/llm_global_budget.json` | JSON fallback ledger |

### Apollo

| Var | Default | Purpose |
|---|---|---|
| `APOLLO_API_KEY` | (DPAPI) | Apollo People Search; campaign refuses if missing |
| (intent: `SAMUS_APOLLO_DAILY_BUDGET_USD`) | (not yet built) | Daily Apollo enrichment ceiling — see [chapter 04, G11](04_guardrails.md) |

### Operator console

| Var | Default | Purpose |
|---|---|---|
| `SAMUS_CONSOLE_BEARER_TOKEN` | (unset, console disabled) | Bearer auth for `/console` + `/api/console/*` |

### Boot

| Var | Default | Purpose |
|---|---|---|
| `PYTHONIOENCODING` | (unset) | Set to `utf-8` to avoid sparkle-emoji banner failures |

---

## DPAPI-stored secrets

Use `Hustleforge.Secrets` module (`_shared/scripts/Hustleforge.Secrets.psm1`)
with scope `Samus`:

| Secret | Required for |
|---|---|
| `ApolloApiKey` | Outreach campaign builder |
| `SesSmtpUser` / `SesSmtpPass` | SES email send |
| `MailFrom` | Verified SES sender address |
| `PostalAddress` | CAN-SPAM footer |
| `StripeWebhookSecret` | Finance webhook signature verification |
| `AnthropicApiKey` | LLM personalisation path |

**Live pause unblock:** without ApolloApiKey + SES creds + PostalAddress
seeded, outreach is dormant by design. This is the "live-pause" referenced
throughout the Codex.

---

## DynamoDB tables (per `recovery/prospect_schema.py`)

| Table | Partition key | Purpose |
|---|---|---|
| `samus_opportunities` | `opportunity_id` | Opportunity rows + Stake fields (added 2026-05-30) |
| `samus_artifacts` | `artifact_id` | Linked artifacts (stake_sentence, gap_report, proposal, callsheet, voicemail) |
| `samus_llm_budgets` | `"GLOBAL"` | LLM $-cap ledger |
| `samus_stake_sentence_budgets` | `"STAKE_SENTENCE_CAP"` | Stake Sentence count ledger |
| `samus_prospects` | `prospect_id` | Qualified prospects (pre-Opportunity) |
| `samus_contacts` | `contact_id` | Owner-enrichment outputs |

JSON fallbacks for both budget tables live under `/opt/samus/data/`.

---

## File ledgers / artifacts

| Path | Writer | Purpose |
|---|---|---|
| `/opt/samus/data/stake_sentence_budget.json` | budget module | Fallback Stake cap ledger |
| `/opt/samus/data/stake_sentence_dedup.json` | guard module | SHA256 hashes of recent Stakes (dedup) |
| `/opt/samus/data/llm_global_budget.json` | LLM budget module | Fallback LLM $-cap ledger |
| `host_artifacts/outreach_skipped_no_stake.jsonl` | run_campaign | Append-only log of contacts skipped for missing Stake |
| `host_artifacts/prospecting_geo_state.json` | prospecting scheduler | Ring catalog state (warm/hot rings) |
| `host_artifacts/apollo_spend.json` | (intent: Apollo budget) | Not yet built |
| `Samus/.data/pattern_winners.yaml` | (intent: A/B winner log) | Not yet built |

---

## CLI commands

### Author a Stake Sentence

```powershell
# CLI entry — single-shot author for a known opportunity_id
python -m backend.crm.stake_opportunity OPP-2026-05-30-001 "I'm reaching out because I drove past your Marysville location Thursday and the second-site expansion is exactly the moment this matters."
```

Exit codes:
- `0` — Stake recorded, artifact registered
- `1` — Failure (cap exhausted | guard rejection | duplicate | budget unavailable | write failed). stderr names the reason.

### Run the outreach campaign

```powershell
python -m backend.outreach.run_campaign `
  --stake-sentences-json stakes.json `
  --opportunity-map-json opp_map.json `
  --max-send 25
```

`stakes.json` maps `{email_lowercase: stake_sentence}`. Missing entries =
contact skipped to `outreach_skipped_no_stake.jsonl`.

`opp_map.json` maps `{email_lowercase: opportunity_id}` so the campaign can
resolve Stakes from the CRM rather than requiring stakes-json.

### Run prospecting daily

```powershell
powershell -ExecutionPolicy Bypass -File `
  D:\Hustleforge\Samus\backend\prospecting\Run-ProspectingDaily.ps1
```

Scheduled task fires this at 07:30 daily (registered via
`Register-ProspectingDailySchedule.ps1`).

### Morning brief

```powershell
python -m backend.morning_runner
```

Requires the gitignored `backend/finance/*.yaml` seed files
(`codb_registry.yaml`, `liabilities.yaml`, `declines.yaml`) — copy from a
sibling worktree on fresh checkouts.

---

## HTTP endpoints (operator_console pack)

All require `Authorization: Bearer $SAMUS_CONSOLE_BEARER_TOKEN`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/console` | HTML console shell |
| `GET` | `/api/console/opportunities/pending_stake` | List up to 50 Opportunities with empty/null stake, oldest first |
| `POST` | `/api/console/opportunities/{opportunity_id}/stake` | Author a Stake Sentence for the given Opportunity — same flow as the CLI |

`POST stake` response codes:
- `200` — Stake accepted; returns the persisted Opportunity
- `404` — Opportunity not found
- `409` — Duplicate Stake (already used in last 100)
- `422` — Guard rejected (banned phrase | length | casing | ASCII ratio)
- `429` — Daily Stake cap exhausted
- `503` — Budget ledger unavailable (fail-closed)
- `500` — Persist failure after guard pass (operator must investigate)

---

## Scheduled tasks (Windows Task Scheduler)

| Task | Cadence | Script |
|---|---|---|
| `Run-ProspectingDaily` | 07:30 daily | `Run-ProspectingDaily.ps1` |
| `Pull-SamusCloudState` | 4h | `Pull-SamusCloudState.ps1` (S4U LocalMachine-DPAPI fallback) |
| `Run-OutreachDaily` | 09:00 daily (live-pause) | `Run-OutreachDaily.ps1` |
| `Run-MorningBrief` | 08:00 daily | morning_runner |
| (intent) `Register-DriftWatcherQuarterly` | every 90d | Re-audit dormant customers, open Opp on score-delta |

---

## Verification checks before unblocking the live pause

Per the Executor advisor's #1 ranked move:

1. `Get-ScheduledTaskInfo Run-OutreachDaily` → `LastTaskResult=0` next fire
2. `Samus/.data/outreach_ledger.jsonl` → ≥1 SES `MessageId` line within 24h
3. SES console: ≥1 `Delivery` event, 0 bounces, 0 complaints
4. Apollo dashboard: API call count > 0, spend < daily budget

Miss any of the four → halt, do not proceed.
