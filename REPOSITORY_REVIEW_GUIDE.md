# Repository Review Guide

If you're reviewing this repository for an engineering portfolio assessment, this file tells you where to spend your time. Read it first.

---

## 30-Second Summary

Samus is a sales automation platform that finds small businesses with weak digital presence, generates personalized outreach, and governs every action through an 11-guardrail enforcement chain. It is a solo-built production system: 21 FastAPI workcells, 10 SQS worker sidecars, Python 3.11, AWS (DynamoDB, SQS, SES, SNS) + GCP Cloud Run, with 416 test files and 4,582 test functions. The engineering interest is primarily in the governance and cost-control architecture, not in the scale of the service mesh.

---

## 2-Minute Read Path

Start here, in this order:

1. **`docs/codex/04_guardrails.md`** — The 11 runtime guardrails, each with its location in code, what it stops, and why it can't be bypassed. This is the constraint architecture. Reading this first tells you what the system cannot do, which is usually more informative than reading what it can.

2. **`docs/codex/03_stake_sentence.md`** — The keystone human-in-the-loop requirement. Explains why a machine-generated sales system requires an operator-authored sentence per prospect, and how that requirement is enforced at the dispatch layer rather than by convention.

3. **`LESSONS_LEARNED.md`** — Nine specific things that were designed wrong and then fixed, with the evidence that surfaced each problem. Reading this alongside the current codebase tells you which design decisions were hard-won and which were deliberate from the start.

4. **`SYSTEM_EVOLUTION.md`** — The version history as an engineering narrative: what assumption broke in each version, what evidence surfaced it, and what the next design stage had to solve. Shows that the architecture arrived at its current form through specific failure modes, not through upfront design.

---

## 10-Minute Inspection Path

**`backend/common/`** — The mandatory shared runtime. Everything a workcell touches that crosses a service boundary runs through here: HMAC auth (`security.py`), LLM budget chain (`llm_budget.py`, `llm_global_budget.py`), idempotency (`idempotency.py`), DLQ and replay (`dlq.py`, `replay_worker.py`), SSRF-safe fetch (`safe_fetch.py`), audit events (`audit.py`), deliberation router (`deliberation.py`). The density of `backend/common/` relative to any individual workcell is a direct measure of how much drift the system experienced before the extraction.

**`backend/common/llm_budget.py`** — Read the module docstring. It explains the four-layer budget chain (global cap, per-workcell quota with EMA scaling, circuit breaker, per-job ceiling) and the failure semantics of each layer. The docstring is unusually complete because the budget chain is the most consequential piece of shared infrastructure.

**`backend/common/stake_sentence_guard.py`** and **`backend/common/stake_sentence_budget.py`** — These two files implement the inversion: the LLM budget fails open on persistence error; the Stake Sentence cap fails closed. Compare the two modules to see the failure mode reasoning applied concretely.

**`backend/gateway/`** — The dispatch gateway. `POST /dispatch/{target}` routes to SQS or signed HTTP based on configuration. This is the integration seam between all workcells and external callers.

**`docs/adr/`** — Eight Architecture Decision Records. ADR-0001 (HTTP vs SQS), ADR-0002 (HMAC identity), ADR-0003 (deterministic before LLM), ADR-0008 (immutable baseline) are the highest-signal reads. Each is short; all eight together take about five minutes.

**`tests/`** — The test surface is 416 files and 4,582 functions. Rather than counting coverage, look at the pattern: unit tests for guard and budget logic, integration tests for SQS worker behavior and DLQ routing. The DLQ tests in particular tell you what failure modes the system was designed against.

---

## 30-Minute Deep Dive

**All eight ADRs** (`docs/adr/`): Read in order. They document not just decisions but the options that were rejected and why. ADR-0002 on HMAC vs OAuth vs mTLS is the most interesting tradeoff discussion.

**The full Codex** (`docs/codex/00_INDEX.md` through `12_the_validation_layer.md`): The Codex is the design specification that the `check_action` gate enforces at runtime. Reading it establishes the intent; then look at `backend/common/governance.py` and a workcell's `check_action` call sites to verify the linkage.

**`backend/signal_filter/`**: The 7-axis `ProspectSignal` composite and the 0.62 admission threshold. `LESSONS_LEARNED.md` §7 explains where that number came from.

**`backend/strategy/`**: UCB1 bandit implementation and `reward_density.py`. The reward formula subtracts harm signals (retractions, unsubscribes, complaints) — this is G7 in the guardrail list. The formula and its coefficients are env-tunable; the harm subtraction is not.

**`recovery/`**: Superseded designs with context. This is not dead code to skip; it's documentation of design dead ends with reasoning intact. `RECOVERY_INDEX.md` is the entry point.

**`ENGINEERING_DECISIONS.md`** and **`ARCHITECTURAL_TRADEOFFS.md`** at the root: supplementary decision rationale that didn't fit the ADR format.

---

## What This Repository Proves

- Governance of a production AI pipeline can be layered and structurally enforced rather than convention-based. The `check_action` gate, immutable baseline, and per-job LLM ceiling together make it difficult to accidentally bypass guardrails.
- A solo operator can build and maintain 21-service complexity if the shared runtime is extracted early enough and the workcell boundaries are clear.
- `LESSONS_LEARNED.md` and `SYSTEM_EVOLUTION.md` together demonstrate that the current architecture was arrived at through iteration on specific failure evidence, not through upfront design. That's visible in the version history.
- The test surface (4,582 functions) reflects accumulated knowledge of what breaks, not a coverage target set at the start.

## What This Repository Does Not Prove

- Production traffic load or measured throughput. This is a solo operator system; the workload is bounded by the human operator's input rate (10 Stake Sentences per day maximum by design). There are no benchmark results to reproduce.
- Multi-engineer workflow or code review patterns. The HMAC identity model, the workcell ownership conventions, and the capability registry are all convention-enforced rather than tooling-enforced in ways a team would need.
- Multi-tenant or external-party security boundary. HMAC is the right choice for a closed fleet under single-operator control. It is not the right choice for a boundary with external parties.

---

## Starting the Stack Locally

See `scripts/README.md` for the local compose startup sequence. The `docker/README.md` covers the 22 Dockerfile layout and which compose target maps to which workcell grouping.
