# Interview Notes

I built Samus as a solo-operator sales automation platform — 21 FastAPI workcells, 10 SQS worker sidecars, Python 3.11, AWS + GCP cross-cloud — and the most important design work was on how the system governs itself, not on how it generates output.

These notes are not a script. They are the actual reasoning behind the decisions, written out so I can recover it under pressure in an interview. I wrote this for myself.

---

## "Walk me through the most important architectural decision you made."

The LLM budget chain.

The short version is that I have a global $1/day cap, a per-workcell quota, a circuit breaker, and a hard limit of one LLM call per job. The reason it's layered is that each layer catches a different failure class that the others miss.

**The global cap** (`llm_global_budget.py`) catches runaway spend when one path goes wrong. If a bug causes a workcell to dispatch jobs in a tight loop, the global cap terminates that before it becomes expensive. Without a global cap, a single misconfigured queue can exhaust a day's API budget in minutes.

**The per-workcell quota** (`llm_budget.py`, `portfolio_controller`) catches the fairness problem that a flat global cap misses. When multiple workcells share a single budget signal, whoever executes first consumes freely and the last workcell in the chain gets throttled. I observed this in v1.3.0 — queue timing determined which workcells got LLM access, which was non-deterministic and not what I wanted. The per-workcell layer assigns explicit quotas and lets `portfolio_controller` rebalance based on efficiency EMA.

**The circuit breaker** catches transient API failure spiraling into repeated error loops. If a workcell accumulates ten consecutive errors, the breaker opens for five minutes. Without this, a model outage causes every job to fail, re-queue, and fail again — burning quota on noise.

**The one-call-per-job ceiling** (`llm_client.py` wrapper, not per-caller discipline) catches the class of bug where a workcell calls the model once, gets a borderline result, and calls again to "improve" it. This is the kind of thing that happens naturally when you give engineers a capable API without a hard ceiling. The ceiling is enforced by the wrapper, not by code review.

Each layer is independently enforced. The circuit breaker can trip without the global cap being near exhaustion. A workcell can exhaust its per-workcell quota while the global cap has headroom. The one-call ceiling fires even if all other budget signals are green. They're not redundant — they address orthogonal failure modes.

---

## "How do you prevent a distributed system from doing something twice?"

Three mechanisms, each targeting a different replay surface.

**In-process idempotency** (`common/idempotency.py`): an LRU-backed OrderedDict, thread-safe, with a `first_seen(key)` method that returns True exactly once per key. Every SQS worker service entry point calls this before doing anything with side effects. The pattern is claim-before-execute: if `first_seen` returns False, the job has already been claimed and the worker returns without acting. The key is derived from the SQS message ID plus a business-level identifier.

**SQS visibility timeout + DLQ**: SQS delivers a message and starts a visibility timer. If the worker crashes or times out without deleting the message, SQS re-delivers it. The `first_seen` check handles this — the re-delivered message loses the race and exits cleanly. Messages that fail repeatedly route to DLQ (`common/dlq.py`) rather than disappearing silently. I retrofitted DLQ routing after observing a batch of 40 prospect records produce zero outreach with no trace of why — silent failure was worse than stuck messages.

**Stripe webhook idempotency**: Stripe delivers the same webhook event multiple times by design. Every Stripe event has an `event_id` I check against the idempotency store before processing. The pattern is the same — claim the event ID first, act second, and any replay of the same event ID exits without duplicate effect. The DLQ handles unprocessable events rather than returning 200 to Stripe (which would stop retries for genuinely bad events).

The common thread: side effects are always preceded by an idempotency claim, never after. Getting this wrong — checking after the effect — creates a window where a crash between the effect and the claim causes a duplicate on replay.

---

## "You have 21 services. How do you keep them from drifting?"

`backend/common/` as a mandatory shared runtime, not a suggestion.

I didn't start with a common layer. I extracted it when three workcells had drifted on HMAC signing in the same two-week period — that was the evidence that told me the extraction cost had been paid by drift cost. By v1.3.0, `backend/common/` centralized HMAC auth, LLM budgets, retry logic, idempotency, DLQ handling, audit event emission, metrics, and safe fetch. Every workcell that touches an external service routes through it.

Parity tests (`tests/` surface) verify that workcells using common infrastructure produce consistent behavior at the boundary — specifically that auth, retry, and audit patterns aren't re-implemented per-workcell. The immutable baseline (`docs/adr/0008-immutable-baseline.md`) hash-verifies governance files at boot, so a workcell can't silently run with a modified guardrail file.

The additive seam policy (explicit since v2.1.1) is the structural answer to future drift: new capability ships as a new workcell or a new optional seam, not as a modification to existing contracts. If a new feature requires touching an existing contract to work, that's a design signal that the feature needs a different entry point.

What I would acknowledge honestly: the capability registry isn't as rigorous as it could be. I track what workcells expose, but the enforcement is convention-based rather than schema-validated at integration time. That's technical debt I'd address if the team grew.

---

## "Why did you choose HMAC over OAuth or mTLS for service identity?"

Operational simplicity in a controlled deployment, at the cost of cryptographic elegance.

The environment here is a closed fleet: 21 workcells and 10 sidecars I control, deploying to Docker Compose for dev and Cloud Run for prod. There's no third-party caller, no user-facing auth flow, and no multi-tenant boundary.

**OAuth** adds an authorization server as a runtime dependency. In a single-operator fleet, that's an infrastructure component I'd have to run, secure, and make highly available for the system to function. The added blast radius isn't worth it when I'm already managing the secret distribution.

**mTLS** is the right answer for high-assurance multi-party systems. The certificate lifecycle — generation, distribution, rotation, revocation — is non-trivial overhead for a solo operator, and the tooling complexity in Python is higher than HMAC. mTLS would be the right default if I were building a system with external parties or compliance requirements.

**HMAC** (`common/security.py`, per-service keys per ADR-0002) signs every request with a per-service key and includes a caller-identity header. The gateway validates the signature and the declared caller. It's not as strong as certificate-based mutual authentication, but for a controlled fleet it's sufficient — an attacker would need the service key, not just network access. Key rotation is a script run; revocation is key replacement. I can reason about the security properties without a PKI background.

The tradeoff I accepted explicitly: if external parties ever need to call into the system, HMAC is the wrong foundation for that boundary. That's the point where I'd revisit. The ADR (0002) documents this.

---

## "What's the biggest thing you got wrong?"

The governance Codex existed as a design document before the runtime enforced it.

In v1.x, the 12-chapter Codex described the 11 guardrails and the `check_action` gate, but the gate didn't actually validate runtime calls against the declared rules. Workcells had their own independent check logic, which could — and did — drift from what the Codex specified. Governance was documented but not enforced; it was a companion document to the code, not a constraint on it.

The v2.0.0 rewrite linked the `check_action` gate to Codex rules at runtime. That made governance drift structurally impossible: a workcell that calls `check_action` with a payload that violates a declared rule gets rejected, regardless of what the workcell's internal logic says.

The cost of getting this wrong: between v1.0 and v2.0.0, governance was convention-based. I was the enforcement mechanism. That doesn't scale even to one other engineer. The lesson I drew: treat governance as a constraint on code from day one, not as documentation that describes intent. The gap between "we documented this" and "this is enforced" is where safety properties go to die.

Evidence in the codebase: `docs/codex/04_guardrails.md` documents the v2.x enforcement points; `LESSONS_LEARNED.md` §5 describes the original fail-open calibration and what it cost.

---

## "How would you redesign this for a team of 10 engineers?"

The architecture would stay mostly intact. The operational model would change substantially.

**What to collapse**: the 21 workcells would be grouped into bounded domains — prospecting/signal, outreach/content, delivery/compliance, governance/audit — with domain ownership assigned to engineers. Right now I navigate freely across workcell boundaries because I built all of them. A team needs ownership boundaries that match the codebase structure.

**What to extract**: `backend/common/` would become a proper internal library with versioned releases and an owned API surface. Currently it's treated as shared code in a monorepo; for a team, the governance of "what goes in common" needs to be explicit, because common is where drift-by-subtraction happens when engineers add workcell-specific things incrementally.

**What governance needs to become**: the 11 guardrails need integration tests that run in CI against the actual gate implementations, not just unit tests of the guard logic. Right now I verify guardrail behavior primarily through code review and the immutable baseline check. A team of 10 needs automated regression for every guardrail — any change that weakens a guardrail's enforcement should fail CI, not just code review.

**What to leave alone**: the additive seam policy, the SQS + signed HTTP dual path, and the Stake Sentence requirement. These are load-bearing and the cost of changing them is high relative to any gain.

**The operational gap to close first**: the capability registry needs schema validation at deploy time. Today it's convention-enforced. With 10 engineers shipping independently, convention enforcement is a guaranteed failure mode.

---

## "How do you govern AI in a production system?"

Not with a policy document. With a layered enforcement chain that makes violations structurally difficult.

The architecture here has four enforcement layers:

**Budget chain**: $1/day global cap, per-workcell quota with EMA-based rebalancing, circuit breaker, max 1 LLM call per job. Each layer catches a different failure class. Critically, the per-job ceiling is enforced by the LLM client wrapper — callers can't go around it.

**Stake Sentence**: every LLM call that results in outreach must have an operator-authored sentence as context. The sentence passes through a guard (`stake_sentence_guard.py`) that rejects templates, banned phrases, deduplication against the last 100 used, and basic authenticity signals. The gate fires before any LLM call, which means a missing or bad Stake Sentence costs no tokens. The daily cap on Stake Sentences (default 10/day) is the real outbound ceiling, and it's fail-CLOSED: if the persistence layer is unavailable, outreach stops, not continues.

**Codex + check_action**: 12 chapters of design rules, enforced at runtime. Every action that modifies state or generates output calls `check_action` with a payload declaring what it intends to do. The validator rejects calls that violate declared Codex rules. Governance drift is structurally blocked — the check runs before the action, and the Codex is hash-verified at boot.

**11 guardrails, all fail-closed except G9**: G9 (LLM budget) fails open on persistence error because losing the budget ledger costs bounded overspend, not safety. Everything else — Stake Sentence cap, CAN-SPAM compliance, TCPA auto-dialer prohibition, evidence-source constraint, legitimacy gate — fails closed. The inversion is deliberate and documented in `docs/codex/04_guardrails.md`.

What this is not: vague "AI safety" principles. Each guardrail exists because the Codex Council named a specific failure mode it would step on. The auto-dialer prohibition (G5) exists because TCPA statutory damages would end a solo-builder company. The evidence-source constraint (G6) exists because LLM-inferred vulnerability claims about real businesses, presented as facts, is defamation.

The question I'd want an interviewer to push on: "what happens if the Codex itself gets modified?" Answer: the immutable baseline hashes governance files at boot and refuses to start if they've changed. That's the last line.
