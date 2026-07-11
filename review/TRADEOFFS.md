# Architecture Tradeoffs

These are the tradeoffs I accepted when building Samus. For each: what I chose, what I gave up, and my current assessment of whether it was the right call. The deeper reasoning lives in [`ARCHITECTURAL_TRADEOFFS.md`](../ARCHITECTURAL_TRADEOFFS.md) and [`docs/DESIGN.md`](../docs/DESIGN.md).

---

## Complexity versus isolation

**I chose:** 21 independently containerized workcells with a shared runtime library.  
**I gave up:** operational simplicity. 22 Dockerfiles, per-workcell deployment configuration, distributed debugging overhead.  
**Assessment:** Still the right call. The isolation has paid off in failure containment, and `backend/common/` as the single implementation of cross-cutting concerns turns out to be worth more than the container overhead costs.

---

## Expressiveness versus auditability

**I chose:** Constrained graph schema, explicit Cypher boundaries, deterministic policy in governance paths.  
**I gave up:** Flexibility to add arbitrary relationship types or LLM-driven routing decisions.  
**Assessment:** Correct. Every time I've wanted to add a new graph label, the forcing function of the schema constraint has made me define it explicitly — which is exactly the right behavior for a system that sends customer communications.

---

## Availability versus enforcement

**I chose:** Fail-CLOSED as the default for all 11 guardrails; fail-open only for the LLM budget (G9).  
**I gave up:** Uninterrupted operation in cases where a guardrail check fails to evaluate.  
**Assessment:** The asymmetry is correct. Failing open on bounded LLM overspend is acceptable. Failing open on outreach without a Stake Sentence is not. I would not change this.

---

## Local development versus distributed guarantees

**I chose:** File fallbacks and HTTP dispatch that work without queue provisioning, DLQ, or cloud identity.  
**I gave up:** Behavioral equivalence between development and production. In-process limiters, local JSONL ledgers, and HTTP fallbacks don't reproduce queue durability or cross-instance rate limits.  
**Assessment:** Correct for development velocity; the cost is that I have to be explicit about what the local environment doesn't test. I document this in `KNOWN_TECHNICAL_DEBT.md` rather than pretending equivalence.

---

## Provider abstraction versus feature access

**I chose:** Shared LLM client in `backend/common/` that all workcells must use.  
**I gave up:** Workcell-specific use of provider features (streaming, function calling with specific signatures, model-specific parameters).  
**Assessment:** Correct at the current scale. The budget chain and audit benefit outweigh the feature access cost. If a workcell needed a provider-specific capability badly enough, the right fix is to extend the shared client with an opt-in capability flag — not to bypass it.

---

## Cloud portability versus operational fit

**I chose:** Cross-cloud deployment — Cloud Run (GCP) compute over cross-cloud AWS persistence (DynamoDB, SQS, SES).  
**I gave up:** Managed identity federation. Currently using long-lived AWS credentials via GCP Secret Manager. Also: some local disk behaviors in workers that aren't Cloud Run compatible.  
**Assessment:** The compute cost advantage of Cloud Run is real. The identity management gap (no Workload Identity Federation) is technical debt I accept as known-and-deferred. I document the fix in `KNOWN_TECHNICAL_DEBT.md`.

---

## Test breadth versus proof of operation

**I chose:** 416 test files covering service logic, governance validation, integration paths, and architecture snapshots.  
**I gave up:** CI pipeline artifacts. The test suite exists but there is no automated run to produce reproducible pass/fail evidence.  
**Assessment:** The test breadth is strong evidence of implementation discipline. The absence of CI is the clearest portfolio gap. A reviewer should treat the counts as accurate but require a fresh run to verify.
