# The Samus Protocol Codex

> *Not for the machine. For us.*
>
> When this thing gets too big, or too complex, or runs its course — this
> document is how we operate it without losing ourselves in the complexity of
> our own genius. It is the shared memory of every decision we made and every
> reason we made it. Read it like you would read a captain's log: in order,
> the first time, and out of order forever after.

**Codex version:** 1.0 — first cut, 2026-05-30
**Maintainer:** Alex
**Scope:** Samus the outbound business-development agent — its purpose, its
guardrails, its operating envelope, and the human role inside it.

---

## How to use this Codex

- Read in order on first pass. The chapters build on each other.
- After that, jump by the index below. Every chapter is self-contained enough
  to re-read in isolation.
- When you change Samus, **update the relevant chapter first**, then write
  the code. If you can't say why in the Codex, you don't get to ship it.
- When the Codex disagrees with the code, the Codex is the question and the
  code is the wrong answer — until you can update one to match the other.

---

## Master index

| # | Chapter | What it answers |
|---|---|---|
| [01](01_premise.md) | **Premise** | What Samus is for. What it isn't. The information-asymmetry arbitrage thesis. |
| [02](02_council_verdict.md) | **The Council Verdict** | The five-advisor deliberation that rejected the original "more proactive" framing and rebuilt the design. Preserved verbatim. |
| [03](03_stake_sentence.md) | **The Stake Sentence** | The keystone. One operator-authored line per Opportunity. Why it converts vendor→partner. |
| [04](04_guardrails.md) | **The Guardrails** | Every fail-closed gate. What it stops, why it must never be bypassed. |
| [05](05_pipeline_flow.md) | **Pipeline Flow** | Funnel → Gap Report → Proposal → Outreach → Voice. The operating diagram. |
| [06](06_modules_map.md) | **Modules Map** | Every workcell with a one-line WHY. Where to look when something breaks. |
| [07](07_operational.md) | **Operational Knobs** | Env vars, DDB tables, file ledgers, CLI commands, console endpoints. |
| [08](08_decisions_log.md) | **Decisions Log** | ADR-style record of choices made and roads not taken. Append-only. |
| [09](09_failure_modes.md) | **Failure Modes** | Catalog of what can go wrong + which guardrail catches it. The Contrarian's gift. |
| [10](10_glossary.md) | **Glossary** | Stake Sentence, Gap Report, Opportunity, FSM stages, banned phrases — exact definitions. |
| [11](11_when_to_shut_it_down.md) | **When To Shut It Down** | Exit conditions. What "done" looks like. How to wind Samus down without losing the artifacts. |
| [12](12_the_validation_layer.md) | **The Validation Layer** | How the Codex becomes runtime — `check_action` gate + auto-drafted ADRs + boot contract. |

---

## The three sentences that summarize the whole Codex

If you read nothing else, read these:

1. **Samus is an information-asymmetry arbitrage engine, not a sales pipeline.**
   Its edge is noticing something specific about a prospect's business before
   they notice it. Volume is the enemy of credibility; depth and chosenness
   are the products.

2. **The Stake Sentence is the load-bearing 5% the audited 95% exists to
   amplify.** Alex writes one sentence per prospect declaring why he picked
   them. Everything else — Gap Report, proposal, callsheet, outreach,
   voicemail — is the machined frame around that one human act of judgment.

3. **Every gate fails closed.** Budget exhausted → refuse. Guard rejects →
   refuse. Stake missing → refuse. There is no bypass env var anywhere on the
   gate path, and there never will be. If you find one, that is the bug.

---

## How to update this Codex

- Open chapter, edit, commit. No PR ceremony for solo work — just commit with
  a `docs(codex): ...` prefix so the log reads cleanly.
- The Decisions Log ([chapter 08](08_decisions_log.md)) is **append-only**.
  Never delete a decision; supersede it with a new entry that explains why.
- The Glossary ([chapter 10](10_glossary.md)) is the single source of truth
  for term meanings. If you find a term used in code that isn't in the
  glossary, add it before you keep reading the code.
- When a chapter grows past ~400 lines, split it. Do not let any one chapter
  become unreadable. The Codex is a reading product, not a database.

---

## Provenance

This Codex was written on **2026-05-30** during the session that shipped the
Stake Sentence system on branch `feat/samus-stake-sentence` (commits
`dccfb694` + `025558c`). Sources captured:

- The original 3-phase plan Alex drafted (funnel → proposal → closure).
- The 5-advisor Council deliberation (Contrarian, First-Principles,
  Expansionist, Outsider, Executor) + 5-way blind cross-review.
- The Chairman synthesis that reframed "proactive" as truth-and-warmth not
  throughput-and-escalation.
- The post-Council insight that crystallized the Stake Sentence as the
  irreducible human input.
- The implementation that followed: 3 new modules, 8 modified files, 6 new
  test files, 40 new tests, full suite 2902/2902 green.

Every claim in this Codex traces to one of those sources. If you find a
claim that doesn't, treat it as suspect until you can.
