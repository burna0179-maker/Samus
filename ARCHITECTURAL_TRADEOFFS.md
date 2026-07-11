# Architectural Tradeoffs

*This document goes deeper than the tradeoff pairs in `review/TRADEOFFS.md`. For each tension I name what I chose, what I gave up, and whether I still think it was right — including the decisions I have revisited or would revisit.*

---

## 1. Container Per Workcell vs. Shared Process

**The tension:** 21 FastAPI workcells + 10 SQS worker sidecars as independent containers means 22 Dockerfiles and 29 Cloud Run services. A shared process model would reduce that surface dramatically.

**What I chose:** Independent containers per workcell.

**What I sacrificed:** Operational simplicity. Deploying a cross-cutting change — updating the `backend/common/` LLM budget chain, for instance — means rebuilding and redeploying every container that imports it. Cold start latency on Cloud Run is a real problem for low-traffic workcells.

**Do I still believe it?** Yes, but with one regret: I underestimated how often I would need to push common library updates. I have mitigated this with a shared base image layer, but the deploy overhead remains. If I were starting over I would be more deliberate about which workcells genuinely need isolation and which could share a process with clear goroutine/thread separation.

---

## 2. Stake Sentence as Hard Gate vs. Soft Signal

**The tension:** Requiring a human-authored Stake Sentence before any outreach LLM call is maximally conservative. I could have made it a quality signal that influences scoring rather than a binary gate.

**What I chose:** Hard gate. The guard runs an anti-template check and deduplication; if it fails, the job stops.

**What I sacrificed:** Throughput. Opportunities that would be perfectly fine to pursue sometimes stall because the Stake Sentence was templated or duplicated. This creates operational friction — someone has to rewrite it before the pipeline resumes.

**Do I still believe it?** Yes, unconditionally. The Stake Sentence is the one place in the pipeline where a human asserts judgment about a specific opportunity. Making it a soft signal would let the system route around it. The operational friction is the point. If it is annoying to bypass, it is working.

**Hurt or paid off?** Hurt short-term throughput. Paid off by preventing three classes of outreach errors I can point to concretely.

---

## 3. Centralized `backend/common/` vs. Per-Workcell Implementations

**The tension:** Centralizing HMAC middleware, the LLM budget chain, the Anthropic wrapper, Neo4j client, DynamoDB helpers, JSONL ledger, SQS worker base, rate limiting, idempotency, correlation IDs, and audit events in one library creates a tight coupling between every workcell and a single shared module.

**What I chose:** Central library. All workcells import from `backend/common/`.

**What I sacrificed:** Independent deployability in the strict sense. A breaking change to `backend/common/` is a breaking change to every consumer. I have managed this with careful versioning discipline, but it requires it.

**Do I still believe it?** Yes. The alternative — per-workcell implementations of HMAC, rate limiting, and audit logging — would have produced 21 divergent implementations within six months. I have seen that failure mode in other systems. The coupling cost is real but bounded; the divergence cost is unbounded. The correctness benefit of a single audit event implementation is not negotiable.

**Paid off:** The LLM budget chain as a shared module means I made one change to enforce the 1-LLM-call-per-job limit across all workcells simultaneously. That would have required 21 coordinated changes in a distributed implementation.

---

## 4. UCB1 Bandit Strategy vs. Fixed Scoring

**The tension:** UCB1 + hierarchical bandit for strategy selection is more complex than a fixed scoring rubric. It requires DynamoDB-backed arm counts, reward computation, and careful initialization.

**What I chose:** UCB1 bandit.

**What I sacrificed:** Interpretability. When a strategy gets selected, the decision is a function of historical reward history, not an explicit rule I can read in a PR. For a compliance review, "the bandit chose it" is not a satisfying answer.

**Do I still believe it?** Mostly. I would revisit the initialization strategy — early-run behavior when arm counts are low is essentially random, which creates a warm-up period where the system is not actually optimizing. I documented this in ADR notes but did not solve it before shipping. A Thompson Sampling approach with a stronger prior might have handled cold start better.

**Hurt operationally:** Yes, specifically during the first week of a new strategy variant being introduced.

---

## 5. JSONL Ledgers for Audit Trails vs. a Structured Database

**The tension:** JSONL append-only files for DLQ records, audit trails, and reward computations are simple and immutable, but they are not queryable without tooling. A relational table or DynamoDB stream would support richer ad-hoc queries.

**What I chose:** JSONL.

**What I sacrificed:** Query convenience. Answering "how many outreach jobs were blocked by G7 in the last 30 days" requires grepping or writing a parser, not running a SQL query.

**Do I still believe it?** Yes for the audit trail specifically. Append-only immutability with no update or delete path is the right property for a compliance record. The inconvenience of querying it is a forcing function to write proper analysis scripts rather than ad-hoc queries that might miss edge cases. For operational dashboards I export aggregates to DynamoDB; the JSONL is the authoritative record, not the query target.

**Paid off:** Zero schema migrations on the audit trail across the entire version history from v1.0 to v2.1.1.

---

## 6. 7-Axis ProspectSignal with Fixed Threshold vs. Learned Admission

**The tension:** A fixed weighted score threshold of 0.62 for prospect admission is deterministic and auditable but does not adapt to changes in the prospect population or signal distribution over time.

**What I chose:** Fixed threshold with explicit weights.

**What I sacrificed:** Adaptability. If the signal distribution shifts — if, say, the review count axis becomes systematically less predictive — the threshold does not self-correct. Someone has to notice and change it.

**Would I revisit?** Yes, and I have been thinking about it. The v2.1.1 causal uplift capability is the beginning of an answer: measuring which signal axes actually predict conversion, not just admission. I have not yet closed the loop between the uplift measurement and the signal weights. That is the next thing to build.

---

## 7. 11 Discrete Guardrails (G1-G11) vs. a Unified Policy Engine

**The tension:** 11 named guardrails with individual implementations, documented in `docs/codex/04_guardrails.md`, means 11 places to look when something fails. A unified policy engine would centralize evaluation.

**What I chose:** 11 discrete guardrails.

**What I sacrificed:** Composability. Adding a new guardrail requires a new implementation, not a new policy expression in a shared evaluator. There is no query language for "which guardrails would block this job" without running them.

**Do I still believe it?** Yes, specifically because the guardrails have heterogeneous failure semantics. G9 fails open; the rest fail closed. A unified policy engine would need to represent that asymmetry explicitly, and doing so in a policy DSL is not obviously simpler than doing so in code. The discrete implementation makes each guardrail's failure mode visible in its own file. I can point a new engineer at G9 and explain the asymmetry in two minutes. Explaining it in a policy engine would take longer.

**Paid off:** Every guardrail failure is independently observable and independently testable. The 4,582 test functions include dedicated coverage for each guardrail's fail-closed (and in G9's case, fail-open) behavior.

---

## One Decision I Revisited: The Recovery Directory

Early in the project I deleted superseded designs rather than archiving them. I reversed this decision after losing context on why the original capability registry design was abandoned. The current `recovery/` directory convention — keeping superseded designs with explicit status headers — came from that mistake. It is not glamorous but it has prevented the same loss of reasoning context twice since. The cost is that anyone reading the codebase must understand that `recovery/` is not active code. I address this with directory-level documentation, but it requires maintenance discipline to keep current.
