# 10 — Glossary

The single source of truth for term meanings. When you encounter a term in
the code that isn't here, add it before continuing.

---

## Core concepts

**Stake Sentence** — A 40-280-character operator-authored sentence
declaring why Alex personally chose this specific prospect. Required for
all outbound from Samus. Renders verbatim in email body, Gap Report
header, and callsheet/voicemail opener. See [chapter 03](03_stake_sentence.md).

**Gap Report** — The SEO + security audit output rendered as Markdown (and
optionally PDF), shown to the prospect as evidence of specific issues
with their public posture. Intended (G6) to contain only
crawler-verifiable claims. The Stake Sentence is the first block above
the cover.

**Opportunity** — A CRM-tracked prospect in active pursuit. Has an FSM
stage and accumulates artifacts. Lives in `samus_opportunities` table.
See `backend/crm/models.py::Opportunity`.

**Proposal** — A deterministic, hard-priced remediation plan generated
from a Gap Report ("close these 5 gaps, costs $X"). Stored as an artifact
linked to the Opportunity.

**Callsheet** — The script Alex uses when making a human call leg.
Includes the Stake Sentence verbatim as the opener.

**Voicemail draft** — The script Alex listens to and decides whether to
record as an actual voicemail. **Samus never autonomously dials.** See
ADR-002.

**Artifact** — Any structured output linked to a Prospect, Opportunity, or
Contact. Has a `kind` (one of `seo_audit`, `seo_report`, `proposal`,
`call_sheet`, `voicemail`, `content_draft`, `stake_sentence`). Lives in
`samus_artifacts` table.

---

## Gates and guardrails

**Gate / Guardrail** — A code-enforced check that refuses to proceed when
some invariant is violated. Numbered G1–G11 in [chapter 04](04_guardrails.md).

**Fail-closed** — On failure, refuse the action. Used when losing the
gate loses safety. See: Stake Sentence budget (G3), Stake gate (G1),
CAN-SPAM (G4).

**Fail-open** — On failure, allow the action with logging. Used when
losing the gate costs money but not safety. See: LLM budget (G9).

**Daily cap** — A per-UTC-day ceiling on some action. Reset at UTC
midnight by `bucket_day` comparison on read. Two flavors in Samus:
LLM-budget (fail-open) and Stake-Sentence-budget (fail-closed).

**Banned phrase** — A string in `STAKE_SENTENCE_BANNED_PHRASES` that, if
present (case-insensitive substring) in a Stake Sentence, causes the
guard to reject. The list catches common SDR template tells.

**Dedup hash** — SHA256 over the normalized (lowercased,
single-spaced) Stake Sentence. Compared against the last 100 hashes; if
present, the Stake is rejected as a duplicate.

---

## Pipeline stages

**Prospecting** — Surfacing candidate businesses from geo rings +
industry verticals. Lives in `backend/prospecting/`. Daily 07:30 task.

**Enrichment** — Adding owner contact info via the cascade: homepage →
`/contact` → `/about` → Facebook mbasic.

**Qualification** — Composite Need + Visibility score crossing a
threshold. Triggers `audit_and_report` automatically.

**Audit** — `audit_and_report` pipeline: passive SEO crawl + security
probe → raw findings.

**Outreach 3-step sequence** — (a) email with proposal link, (b) 24h
follow-up, (c) 7-day no-reply → voicemail draft. All steps require Stake
Sentence (G1).

**FSM stages (Opportunity)** — `new` → `qualified` → `proposal` →
`negotiation` → `closed_won` | `closed_won_retainer` | `closed_lost`.

**FSM stages (Outreach)** — `open` → `pitch` → `engage` →
`{handle_objection | close_attempt}` → `{fallback | exit}`. Separate from
the CRM FSM.

---

## Operator-facing terms

**Operator** — Alex. The human in the loop. The `SAMUS_OPERATOR` env var
stamps `stake_sentence_authored_by` with this identity.

**Live pause** — The deliberate state where Samus is fully built but
not actually sending outbound, because secrets (Apollo, SES, postal
address) are unset. Unblocked by seeding DPAPI secrets. See [chapter 07](07_operational.md).

**Console** — `/console` HTML shell + `/api/console/*` endpoints behind
Bearer auth. The web surface for authoring Stake Sentences and reviewing
pending Opportunities.

**Morning brief** — Daily 08:00 task that produces a finance + sales
summary. Requires gitignored YAMLs (`codb_registry.yaml`,
`liabilities.yaml`, `declines.yaml`) seeded on fresh worktrees.

---

## Architecture terms

**Workcell** — A bounded business capability (prospecting, outreach,
CRM, finance, strategy, voice, etc.). Each lives in its own
`backend/<name>/` directory.

**Pack** — A bolt-on capability that extends an agent without modifying
its core (e.g., `operator_console`). Lifespan-wired via settings flags.

**Standard plane / Core plane** — The capability tiers in the
HustleAgent canonical architecture. Core is foundational; Standard layers
on top.

**Quorum** — The cross-agent consensus mechanism (Major + Anita +
Darwin). `CLEAN` quorum = unanimous safe-to-proceed signal.

**Hub** — The cross-agent fan-out bus. Samus subscribes (PDC observer);
peers publish.

---

## External systems

**Apollo** — Apollo.io B2B contact database. Used for prospect
enrichment + email unlock. Carries no prior express written consent —
key reason auto-dialing is forbidden (ADR-002).

**SES** — AWS Simple Email Service. The send rail for outreach. Bounce
and complaint rates govern account health (F10).

**Stripe** — Payment processor. `payment_intent.succeeded` is the
terminal multiplier on `reward_density` (ADR-004 intent).

**DynamoDB (DDB)** — Primary persistence. Per-table JSON fallbacks
exist under `/opt/samus/data/` for the budget tables.

**DPAPI** — Windows Data Protection API. Used by `Hustleforge.Secrets`
PowerShell module to store secrets at rest. S4U scheduled tasks can't
decrypt CurrentUser scope — use LocalMachine fallback.

**Defender / Sysmon / WDAC** — Host security stack. Defender's
`PowhidSubExec` signature blocks PS payloads matching certain patterns
(see memory note for the workaround).

---

## Voice / tone

**Vendor framing** — "We serve businesses like yours." Templated;
discoverable by anyone. The wrong framing.

**Partner framing** — "I picked you, for this reason." Requires
declared chosenness. The Stake Sentence is the operationalization. The
right framing.

**Chosenness** — The asymmetry produced when a prospect realizes the
person on the other end refused most of the queue to stop on them. The
mechanism by which Samus converts vendor→partner.

**Earlier warmth** — The Outsider's reframe: proactive should mean
generating context for being known to a prospect *before* the pitch, not
escalating after. Operationalized as G8 (pre-flight legitimacy filter,
intent).

---

## Failure-mode shorthand

Cross-reference [chapter 09](09_failure_modes.md) for full entries.

| Code | Short |
|---|---|
| F1 | TCPA class action |
| F2 | Defamation from hallucinated Gap Report |
| F3 | Subscription-product class action |
| F4 | Stake cap bypassed |
| F5 | Goodhart collapse |
| F6 | CAN-SPAM violation |
| F7 | Live-pause seed gap |
| F8 | Stake budget ledger corruption |
| F9 | Dedup false positive |
| F10 | SES suspension |
| F11 | Apollo budget burn |
| F12 | Bulk Sunday-night Stakes (operator failure) |
| F13 | Codex stops getting updated |
