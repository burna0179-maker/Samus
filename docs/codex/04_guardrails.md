# 04 — The Guardrails

Every guardrail in Samus exists because the Council named a specific failure
mode it would otherwise step on. This chapter catalogs each one, what it
stops, and why it must not be bypassed.

The unifying rule: **every gate fails closed**. No bypass env var exists on
any gate path, and none will be added.

---

## G1 — Stake Sentence required at outbound dispatch

**Where:** `backend/outreach/campaign.py::compose_body`,
`backend/outreach/campaign.py::build_messages`,
`backend/outreach/run_campaign.py` (the 3-step sequence).

**What it stops:** Templated cold-outreach with no human-authored intent.
The Council's most consistent finding was that volume kills credibility;
the Stake Sentence forces credibility per contact.

**Why it can't be bypassed:** The whole architecture exists to make the
human's one-sentence judgment the load-bearing element. If the gate has a
bypass, the architecture has no purpose. See [chapter 03](03_stake_sentence.md).

**How it's enforced:**
- `compose_body` raises `OutreachStakeMissing` if `stake_sentence` is None
  or empty, **before any LLM call** so a missing stake also can't burn
  tokens.
- `build_messages` skips contacts with no entry in `stake_sentences` and
  counts them as `not_sendable`.
- The body template literally contains `{stake_sentence}` and
  `_safe_format` raises `OutreachStakeMissing` if the token is not filled,
  rather than silently dropping it.
- An `assert stake in body` after rendering verifies the invariant
  defensively.

---

## G2 — Stake Sentence guard (anti-template, anti-dup)

**Where:** `backend/common/stake_sentence_guard.py::validate_stake_sentence`,
`is_duplicate`.

**What it stops:** Stake Sentences that are functionally templates. The
banned-phrase list catches the most common SDR tells. Length, casing, and
ASCII checks catch lazy or AI-generated submissions. Dedup catches "I'll
just reuse the good one."

**Why it can't be bypassed:** A bypass would re-introduce templating
through the back door — defeating the architecture's purpose (G1).

**The banned-phrase list (case-insensitive substring):**

```
i hope this finds you well
i came across your
noticed you
saw your business
we help businesses
we work with
our company specializes
synergy
leverage
ecosystem
circle back
touch base
```

This list is in `backend/common/stake_sentence_guard.py` as
`STAKE_SENTENCE_BANNED_PHRASES`. Adding a phrase requires a Decisions Log
entry ([chapter 08](08_decisions_log.md)). Removing one requires a stronger
one.

---

## G3 — Daily Stake Sentence cap (fail-CLOSED)

**Where:** `backend/common/stake_sentence_budget.py`.

**What it stops:** Throughput drift. The cap is Samus's real outbound
ceiling. Default 10/day.

**Why it fails CLOSED (inverted from LLM budget):** The LLM budget fails
open on persistence error because losing it costs money but not safety.
The Stake Sentence cap is the **outbound ceiling**; losing it loses
*safety*. If neither DDB nor JSON ledger is readable, the cap raises
`StakeSentenceBudgetUnavailable` and refuses to record.

**Knob:** `SAMUS_STAKE_SENTENCE_DAILY_CAP` (env). Default `10`. Never
config'd to >50 without a Decisions Log entry stating why.

---

## G4 — CAN-SPAM compliance (refuses to send)

**Where:** `backend/outreach/campaign.py::build_messages` raises
`ValueError` if `sender_postal_address` or `unsubscribe_url` is empty.

**What it stops:** A cold campaign going out without the federally required
physical address and opt-out link. This guard predates the Stake Sentence
work; it survived the Council review unchanged because nobody argued for
loosening it.

**Knobs:**
- `SAMUS_OUTREACH_POSTAL_ADDRESS` (env)
- `SAMUS_OUTREACH_UNSUBSCRIBE_URL` (env)

Both must be set before any campaign builds. The compliance footer is
appended last to every body regardless of LLM path.

---

## G5 — No auto-dialer (TCPA poison)

**Where:** Conceptual — the auto-dialer leg of the original 3-phase plan
was removed by Council verdict and **never wired**. Voice work product is
the **voicemail draft artifact**, not an outbound call.

**What it stops:** TCPA liability. Auto-dialing prospects from
Apollo-scraped numbers with no prior express written consent is a
$500–$1,500 per-call statutory exposure. A single litigious recipient
ends a solo-builder company.

**Why this is structural, not configurable:** There is no
`SAMUS_AUTODIAL_ENABLED=true`. If one ever appears in the codebase, that
is the bug — delete it. The callsheet generator produces a
`voicemail_draft` artifact that Alex listens to and records (or doesn't).
The decision to dial is human and per-call.

See [chapter 02](02_council_verdict.md), Contrarian's ranked failure #1.

---

## G6 — Gap Report evidence-source constraint *(enforced 2026-05-30 via ADR-012)*

**Where:** `backend/seo/evidence_source.py` (the enum),
`backend/seo/models.py` (`SeoIssue.evidence_source`, `AuditResult.evidence_sources`),
`backend/seo/audit.py` (tagging at extraction time),
`backend/seo/report.py::_filter_verified_issues` (the fail-closed serialization
filter), and Codex validator `VR-G6`.

**What it stops:** LLM-inferred vulnerability claims about a real
business showing up in a document signed-as-fact and sent to that business.
Auto-defamation in a fancy hat.

**Enforcement:** Every `SeoIssue` carries `evidence_source: EvidenceSource | None`.
Valid sources are deterministic only: `crawled_header`, `crawled_html`,
`crawled_meta`, `cert`, `dns`, `redirect`, `public_registry`, `robots_txt`,
`sitemap`, `http_status`. Untagged findings are dropped at render time by
`_filter_verified_issues`. The Codex layer's `VR-G6` rejects any
`gap_report_render` call that doesn't declare its post-filter
`evidence_sources` list (empty list is permitted; missing key blocks).

**Why it can't be bypassed:** The filter runs at serialization, before any
markdown leaves the workcell. The Codex validator is meta-checked at the
call site so a future caller skipping the filter is caught.

---

## G7 — Reward function subtracts harm *(enforced 2026-05-30 via ADR-012)*

**Where:** `backend/strategy/reward_density.py::compute_reward`,
`backend/strategy/harm_signals.py`, Codex validator `VR-G7`.

**What it stops:** Goodhart collapse. A reward of *"Opportunity created"*
trains Samus toward maximizing Opportunities-created; a reward of
*"closed_deal"* alone trains Darwin's mutation engine toward whatever
rhetoric closes deals fastest — a manipulation optimizer.

**Formula (in code):**

```
reward = stage_advanced * SAMUS_REWARD_STAGE_WEIGHT
       − llm_cost_cents * SAMUS_REWARD_LLM_COST_WEIGHT
       − SAMUS_REWARD_HARM_K * (retracted_claims + unsubscribes + complaints)
       + SAMUS_REWARD_TERMINAL_MULTIPLIER * stripe_payment_intent_succeeded_count
```

Coefficients are env-tunable; defaults `(1.0, 0.01, 5.0, 100.0)`. Negative
reward clips to 0. Every computation persists to
`host_artifacts/reward_computations.jsonl` for audit.

**Enforcement:** `compute_reward` calls `check_action` with
`subtracts_harm=True`. The Codex validator's `VR-G7` rejects any
`reward_function_update` action that does NOT set `subtracts_harm=True` —
no future caller can bypass the harm term by going around `compute_reward`.

**Harm signal collectors:** fail-OPEN individually (return 0 on lookup
failure), fail-CLOSED in aggregate (all three throwing aborts the
computation).

---

## G8 — Pre-flight legitimacy signal *(enforced 2026-05-30 via ADR-012)*

**Where:** `backend/prospecting/legitimacy.py`,
`backend/prospecting/legitimacy_check.py`,
`backend/prospecting/sources/{ca_sos,prior_inbound,chamber_roster}.py`,
`backend/crm/needs_warm_path.py`,
`backend/outreach/run_campaign.py::_apply_warmth_gate`, Codex validator
`VR-G8`.

**What it stops:** Cold-cold outreach to a business with zero public
signal that they want to be contacted. The Outsider's reframe
operationalized.

**Rule (enforced):** Prospect must have ≥1 of:
- `public_registry` — deterministic registry hit (CA SOS today; others extensible)
- `chamber_roster` — operator-curated Chamber JSON match
- `prior_inbound` — onboarding lead or outreach ledger hit
- `rfp` — public RFP/procurement match *(collector deferred)*
- `open_job_listing` — public hiring signal *(collector deferred — TOS-sensitive)*

Cold-cold prospects (no signal) divert to a `needs_warm_path` queue
(DDB `samus_needs_warm_path` + JSON fallback). Diverted contacts never
burn Apollo credit or LLM budget — the pre-flight runs BEFORE
stake-resolution + compose.

**Enforcement:** `run_campaign._apply_warmth_gate` populates
`ApolloContact.legitimacy_signal` with the highest-confidence signal kind,
which flows through `compose_body` into the Codex payload. `VR-G8`
rejects any `outreach_send` action whose payload lacks
`legitimacy_signal`.

**Operator surface:** `GET /api/console/needs_warm_path` lists diverted
prospects; `POST /api/console/needs_warm_path/{id}/promote` lets the
operator attach a manual warmth signal and return the prospect to the
active pool.

---

## G9 — LLM budget cap (fail-OPEN on persistence, fail-CLOSED on cap hit)

**Where:** `backend/common/llm_client.py`,
`backend/common/llm_global_budget.py`.

**What it stops:** Runaway LLM spend. Default $1/day global cap.

**Why it fails OPEN on persistence error:** A budget ledger I/O failure is
not a safety risk — it risks overspending by some bounded amount. The
correct response is "log and continue" rather than "refuse all LLM use." 
This is **inverted** from G3 (Stake Sentence cap) because the failure
modes are inverted.

**Knob:** `SAMUS_LLM_DAILY_BUDGET_USD` (env). Per-workcell quotas inside
that.

---

## G10 — Per-job LLM ceiling (max 1 LLM call per job)

**Where:** `backend/common/llm_client.py` — `anthropic_messages` wrapper.

**What it stops:** A single workcell job recursing or looping into
multiple LLM calls and burning the daily budget on one prospect.

**Hard limit:** 0 or 1 LLM call per job. Enforced by the wrapper, not by
caller discipline.

---

## G11 — Apollo budget governor *(enforced 2026-05-30 via ADR-012)*

**Where:** `backend/common/apollo_budget.py`, `backend/common/apollo_pricing.py`,
wrappers in `backend/outreach/apollo_source.py`.

**What it stops:** Apollo enrichment burning the day's API budget on a
single bad campaign config.

**Pattern:** Mirror of `llm_global_budget.py`. DDB
`samus_apollo_budgets` + JSON fallback at `/opt/samus/data/apollo_budget.json`.
Daily bucket keyed by UTC `bucket_day`. Default cap `$5.00/day` via env
`SAMUS_APOLLO_DAILY_BUDGET_USD`.

**Failure semantics:**
- **fail-OPEN on persistence:** ledger I/O failure costs bounded
  overspend, not safety — log loudly, continue.
- **fail-CLOSED on cap hit:** pre-flight `assert_allows(usd)` raises
  `ApolloBudgetExceeded` BEFORE the HTTP call is made.

**Cost knowledge:** `apollo_pricing.estimate_call_cost(endpoint, units)`
maps endpoints → credits → USD using
`SAMUS_APOLLO_USD_PER_CREDIT` (default `$0.04`). Known endpoints:
`people_search` 1c, `email_unlock` 1c, `phone_unlock` 8c,
`organization_search` 0c. Unknown endpoints assume 1 credit, log warning.

**Codex hook:** Apollo wrappers fire a `check_action(action_kind="other",
payload={"target":"apollo_call",...})` so calls are visible to the
validator for audit. There is no `VR-G11` blocking rule — the budget
module's own fail-CLOSED is the enforcer; the Codex layer just observes.

**Operator surface:** `GET /api/console/apollo/budget` returns today's
spend, cap, remaining, and recent-call list.

---

## Inversions to remember

The two cap modules look identical and are not:

| | Fail-OPEN on persistence | Fail-CLOSED on persistence |
|---|---|---|
| **Module** | `llm_global_budget.py` | `stake_sentence_budget.py` |
| **What it caps** | Money | Outbound ceiling |
| **Cost of losing the cap** | Bounded overspend | Unbounded sends |
| **Right response to ledger loss** | Allow, log warning | Refuse, raise |

If you ever find yourself porting the LLM budget pattern to a new cap,
**check which failure mode the new cap is protecting against** before you
copy the fail-open semantics.

---

## The bypass that does not exist

There is no `SAMUS_SKIP_STAKE`, no `SAMUS_OUTREACH_FORCE`, no
`SAMUS_BYPASS_GUARDS`, no admin escape hatch on any gate. Every gate is
designed to be unbypassable by configuration alone — only by code change.

The reason is structural: if you can disable the architecture's load-bearing
constraint via env var, you will, on a Friday afternoon at 5:47pm when the
demo is in fifteen minutes, and then never re-enable it. Don't give yourself
the option.
