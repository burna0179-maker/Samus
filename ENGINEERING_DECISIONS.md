# Engineering Decisions

*Samus is a sales automation platform — an information-asymmetry arbitrage engine that identifies small businesses with weak digital presence and generates personalized outreach. Every decision below either serves that goal or governs it safely.*

---

## 1. Workcell Decomposition Over a Monolith

I decomposed Samus into 21 FastAPI workcells plus 10 SQS worker sidecars, each its own container. I rejected the monolith not out of reflexive microservices enthusiasm but because the failure modes here are genuinely heterogeneous. A prospecting scanner that hammers third-party APIs has completely different retry, rate-limit, and cost characteristics than an LLM synthesis call or a DynamoDB write. Colocating them means one misbehaving component — a blocked DNS lookup in the signal filter, say — can stall outreach delivery. The tradeoff I accepted is operational surface area: 22 Dockerfiles, 29 GCP Cloud Run services, meaningful orchestration overhead. What I learned is that the decomposition boundary should track *failure domain*, not feature domain. Every split I regret maps to a feature seam; every split that paid off maps to an independent failure mode.

## 2. HTTP + SQS Dual Dispatch Path

The gateway routes `POST /dispatch/{target}` to either SQS or signed HTTP depending on environment config. I rejected a single path in either direction. SQS-only would have made local development and synchronous integration tests painful. HTTP-only would have made the production outreach pipeline fragile under transient downstream unavailability. The dual path lets the same dispatch surface serve local Docker Compose with direct HTTP and the VM/Cloud Run targets with durable queues. The cost is that every workcell must be agnostic to which transport delivered the job. That constraint turned out to be a forcing function for good design: jobs are self-describing, idempotency is required, and correlation IDs propagate through both paths identically.

## 3. Deterministic-Before-LLM

All governance, scoring, state transitions, and billing run on deterministic code. LLM calls are reserved for synthesis — places where the output is genuinely open-ended and where a wrong answer is recoverable. I explicitly rejected LLM-assisted scoring or routing because those paths touch money and compliance. The 7-axis ProspectSignal with a weighted admission threshold of 0.62 is pure arithmetic. UCB1 bandit strategy selection is pure arithmetic. If I cannot read the decision logic in a code review, I do not trust it in production. The tradeoff is that deterministic logic requires explicit modeling of every case. That cost has been lower than I expected; the cases I thought would require LLM judgment mostly did not.

## 4. Three Persistence Models: DynamoDB + JSONL + Neo4j

I use three persistence models and I chose each for a distinct access pattern. DynamoDB (7 tables in CRM alone) handles keyed operational reads — prospect state, bandit arm counts, idempotency tokens — where I need single-digit millisecond latency at any scale. JSONL append-only ledgers handle audit trails, DLQ records, and reward computations where I need immutable history, zero-schema-migration risk, and the ability to replay or grep. Neo4j (Hivemind) handles the prospect knowledge graph — relationships between prospects, opportunities, conversations, and KG tiering — where I need traversal queries that would be absurd in a key-value store. I rejected a single unified database because no one engine serves all three patterns well. The operational cost is three connection pools, three failure modes, and three backup strategies. I would make the same choice again.

## 5. Per-Service HMAC Identity Over OAuth/JWT/mTLS

Each service has a shared secret; caller-grants define which services can call which. I evaluated OAuth, JWT bearer tokens, and mutual TLS. OAuth requires an authorization server that becomes a single point of failure and operational dependency. JWT with asymmetric keys requires key distribution and rotation ceremony. mTLS requires certificate infrastructure that is expensive to operate for an internal service mesh at this scale. HMAC with a capability registry is simpler, auditable at the application layer, and fails loudly when a service calls something it is not granted. The tradeoff is that shared secrets require secure distribution, which I handle via DPAPI-sealed injection at boot. If the architecture grew to dozens of teams and hundreds of services I would revisit this. At current scale it is the right call.

## 6. Layered LLM Budget Chain

LLM spend flows through four layers: Layer A is a global $1/day cap; Layer B is a model floor regex (prevents accidental use of expensive models); Layer C is a circuit breaker; Layer D is a per-workcell token quota. Additionally, a hard wrapper enforces at most one LLM call per job. I rejected a single top-level budget check because a single check fails catastrophically when one runaway job consumes the entire daily budget before other workcells have run. The layered design means a single misbehaving workcell is rate-limited before it affects the rest of the system. The Stake Sentence budget (daily cap on outreach LLM calls) is a separate control because the risk model is different: unbounded outreach is a compliance and reputation risk, not just a cost risk.

## 7. Fail-CLOSED as Default, One Explicit Fail-Open Exception

All 11 guardrails (G1–G11) fail closed by default — when the guard cannot make a determination, the action is blocked. The one explicit exception is G9, the LLM budget guardrail, which fails open with bounded overspend. I chose this deliberately: a false-positive in the budget check that blocks all outreach is a worse outcome than a few cents of overspend. Uncontrolled outreach to real humans is not recoverable; a small budget overrun is. Every other guardrail governs an action that is either externally visible (sending email, making a call) or computationally irreversible (writing state). Those fail closed without exception. I document this asymmetry explicitly in the Codex because the next engineer to touch G9 needs to understand why it looks different from the other ten.

## 8. Immutable Baseline at Boot

Identity and governance code is hash-verified against a protected baseline at boot. If verification fails, the service refuses to start. I rejected runtime patching of governance code as an operational convenience because it creates an attack surface: a compromised dependency or config injection could silently alter the rules governing outreach. The immutable baseline means the governance posture I shipped is the governance posture running in production. The tradeoff is that any legitimate change to governance code requires a full redeploy. That is a feature, not a bug.

## 9. Recovery Directory as Explicit Superseded Designs

The `recovery/` directory contains superseded designs. They are not active code and not dead code to be deleted — they are documented decisions that I evaluated and set aside. I rejected deleting them because the reasoning embedded in those designs is not obvious from the current implementation. An engineer reading the codebase three months from now should be able to see what I tried, why I moved on, and what assumptions changed. This is not sentiment; it is navigational. The cost is that `recovery/` must be maintained with clear header comments indicating status, or it becomes a source of confusion rather than clarity.

## 10. Constrained Graph Schema for Hivemind

I designed the Neo4j schema around four first-class node types: prospects, opportunities, conversations, and KG tiering relationships. I rejected a property-graph free-for-all where any concept could become a node. The risk in an unconstrained graph schema is that the graph becomes a dumping ground and traversal queries become expensive and unreliable. By constraining the schema and centralizing the Neo4j client in `backend/common/`, every workcell speaks the same graph vocabulary. The tradeoff is that adding a new concept requires a deliberate schema decision rather than a convenience write. Three times I have been grateful for that friction.
