# Design Rationale

This document explains why I built Samus the way I did. It covers the reasoning behind architectural choices, what I rejected and why, and the tradeoffs I accepted. Individual durable decisions live in `docs/adr/`.

The code and tests are the source of truth for runtime behavior. This document explains intent.

---

## What I Was Optimizing For

When I started designing Samus, I had a specific problem: a growing set of automation workflows that shared infrastructure needs but had no shared infrastructure. Every new capability duplicated authentication, retry logic, persistence access, and cost management. I built Samus to eliminate that drift permanently.

The design is optimized for:

- reusable platform infrastructure across all capabilities;
- clear ownership boundaries per business domain;
- deterministic execution wherever possible;
- durable async work where determinism isn't sufficient;
- human-auditable side effects for irreversible or externally-visible actions;
- bounded and accountable use of LLM inference;
- incremental deployment — I can add a workcell without touching existing ones;
- explicit failure and recovery paths, not just happy-path coverage.

It is not optimized for minimum service count, globally distributed availability, or eliminating all operator involvement. Those are legitimate goals for different systems.

---

## Why Workcells

I chose to decompose Samus into bounded workcell modules rather than a single application or a coarser three-tier design. Each workcell owns its domain models, FastAPI routes, SQS worker, and deployment definition.

Benefits I was targeting:

- Ownership is clear. The CRM workcell owns CRM. The SEO workcell owns site auditing. Engineers can understand and modify one without reading the others.
- Failures stay local. A stuck outreach queue doesn't block prospecting enrichment.
- Platform behavior is reusable. Every workcell gets HMAC authentication, idempotency, metrics, and LLM budgeting from `backend/common/` without implementing it themselves.
- Domain changes don't require editing one central application.

The costs I accepted:

- 22 Dockerfiles to maintain.
- Contract versioning between workcells.
- Distributed debugging and tracing complexity.
- Eventual consistency between workcell state.

I accepted these costs deliberately. For a single-developer project, the complexity is real. But the value is demonstrating that I can design and operate platform boundaries — which is harder to show with a monolith.

---

## Why HTTP and SQS

I support both synchronous HTTP dispatch and durable SQS dispatch through the same gateway. The gateway selects the path based on whether a queue URL is configured. The service layer handles both — no duplicated business logic.

The dual path solves a real tension:

- HTTP keeps local development practical. I can start a workcell with `uvicorn` and call it directly without provisioning queues.
- SQS provides durability, independent consumers, and retry semantics for long-running or expensive work.

The risk I accepted is behavioral drift — the HTTP path and the SQS path must produce identical business outcomes. I address this with parity tests and by placing business logic exclusively in the service layer, not in route handlers or worker dispatch.

---

## Why Deterministic Logic Before LLM Logic

Every place I use an LLM is a place where cost, latency, and nondeterminism increase. I designed Samus to use LLMs only where flexible synthesis or interpretation materially adds value over a deterministic alternative.

Deterministic logic handles:

- governance and guardrail enforcement;
- scoring thresholds and signal filtering;
- FSM state transitions;
- billing math;
- suppression checks;
- retry decisions;
- report structure;
- capability and authorization checks;
- idempotency and routing.

LLMs handle:

- personalized outreach drafting when a human-authored Stake Sentence is present;
- callsheet opener synthesis from prospect research;
- Gap Report narrative when evidence-source constraints are satisfied;
- voicemail draft generation.

This design reduces cost, latency, audit complexity, and nondeterminism in the paths that need to be reliable. It also makes the LLM budget chain meaningful — there's a bounded set of places where LLM calls happen, and I can reason about each of them.

---

## Why Multiple Persistence Mechanisms

I use DynamoDB, SQS, JSONL ledgers, and Neo4j rather than a single database. This looks like accidental complexity from the outside. It isn't.

Each store fits a distinct workload requirement:

- **DynamoDB** — durable keyed state and atomic coordination. The CRM domain has seven tables. DynamoDB gives me single-digit millisecond latency on keyed access, conditional writes for idempotency, and managed scaling.
- **SQS** — work queues with retry semantics, visibility timeouts, and DLQ routing. The queue semantics matter: I can replay, inspect, and poison-pill individual messages.
- **JSONL** — append-only audit trails. Fast writes, human-readable, no schema migration. Appropriate for ledgers where I need immutable history and operator recovery, not queries.
- **Neo4j** — controlled relationship traversal. I use this for prospect graphs, conversation graphs, and KG tiering — workloads where graph queries are the natural fit and I need to prevent arbitrary schema mutation.

The cost is reconciliation complexity and operational burden of running four different storage systems. I believe the fit improvement justifies it. For a team that needed to minimize operational surface, I would evaluate consolidating the JSONL ledgers into DynamoDB and dropping Neo4j in favor of DynamoDB-backed relationship modeling.

---

## Why Constrain Graph Schema

I could have given the graph layer arbitrary label support and open-ended Cypher. I rejected that because arbitrary schema mutation makes graph behavior unauditable and introduces authorization complexity — if any workcell can create any label, I lose the ability to reason about what the graph represents.

Instead, I constrained the schema to explicit types and bounded the query surface. The graph is expressive enough for its actual use cases — prospect-opportunity-conversation relationship traversal — and auditable in a security review.

---

## Why Human-on-the-Loop Controls

Revenue, outreach, and financially-sensitive actions have asymmetric failure costs. Getting the LLM budget wrong costs money I can recover. Sending unsolicited email to the wrong recipient at scale, or making unauthorized charges, creates legal and reputational harm that can't be undone.

I distinguished four action risk levels:

1. Advisory outputs — no approval needed; deterministic analysis only.
2. Reversible automated actions — automated with audit trail.
3. Externally visible actions — require Stake Sentence; Codex guardrails enforced at dispatch.
4. Financially or legally sensitive actions — require explicit operator approval or configuration gate.

This isn't a refusal to automate. It's a calibrated automation boundary that preserves authority where the failure cost is highest.

---

## Why LLM Cost is a Platform Concern

Left to individual workcells, LLM cost management doesn't work. Each caller makes locally reasonable decisions that collectively exceed a daily budget, or worse, don't track spending at all.

I built a four-layer enforcement chain in `backend/common/llm_client.py`:

- **Layer A** — global daily spend cap ($1/day); rejects calls when the budget is exhausted.
- **Layer B** — model floor; prevents calls that would use a model below the minimum acceptable capability tier.
- **Layer C** — circuit breaker; trips on repeated API failures to prevent cascade.
- **Layer D** — per-workcell token quota; each workcell gets a defined share of the budget.

Every LLM call in the system passes through this chain. No workcell bypasses it. The maximum is one LLM call per job — enforced in the wrapper, not by caller discipline.

I chose fail-open on the LLM budget (G9 in the Codex) because a bounded overspend on inference is recoverable. I chose fail-closed on the Stake Sentence (G1) because uncontrolled outreach is not recoverable. The asymmetry is intentional.

---

## Why Local Fallbacks Exist

Some paths fall back to local files when cloud services are unavailable. This allows development and partial operation without full AWS provisioning. It improves first-run usability, test isolation, and operator recovery during provider outages.

These fallbacks do not provide distributed guarantees. They should not be used in production paths where durability, consistency, or multi-instance correctness matters. In production, I remove or disable fallbacks for critical writes.

---

## Why recovery/ Is Separate

`recovery/` contains prior designs, prototypes, and superseded implementations. This material preserves reasoning without representing it as shipped functionality. Git history alone loses the contextual structure of why something was replaced.

The directory must remain clearly labeled and excluded from any claim of active architecture. If code in `recovery/` is referenced by the live stack, that is a bug.

---

## Designs I Rejected

**Runtime schema mutation** — Rejected. Arbitrary ontology mutation would break graph auditability and complicate authorization. The constrained schema is a deliberate constraint, not a limitation.

**LLM-first routing** — Rejected. Deterministic policy, safety controls, and cost enforcement must remain authoritative. LLM routing would introduce nondeterminism in paths where reliability is required.

**Single large application** — Evaluated and rejected. It would reduce operational complexity but make platform boundaries invisible and eliminate independent deployment evidence.

**Single persistence system** — Evaluated and rejected. Queue semantics, keyed state, append-only evidence, and graph relationships have different operational requirements. A single system would force compromises on all four.

---

## Tradeoff Summary

| Choice | Benefit | Cost |
|---|---|---|
| Workcell decomposition | Ownership and isolation | Operational complexity |
| HTTP + SQS | Flexible execution | Parity and observability burden |
| Multiple stores | Workload fit | Reconciliation and operations |
| Deterministic-first | Predictability and auditability | Less flexibility |
| Human-on-the-loop | Risk control | Slower high-risk workflows |
| Local fallbacks | Development resilience | Weaker distributed guarantees |
| Central LLM budgets | Cost governance | Shared dependency and policy maintenance |
| Constrained graph | Auditability and security | Reduced expressiveness |
