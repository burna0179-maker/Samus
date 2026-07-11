# Lessons Learned

engineering retrospective on building Samus. These are the things I got wrong, what changed, and where the evidence lives.

---

## 1. Cross-cutting concerns should be extracted once the third workcell drifts

I didn't start with `backend/common/`. I started with 21 workcells that each handled auth, retries, and audit logging however they needed to. The drift became visible when I had to fix a retry bug in three places in the same week, and when an HMAC signing change required touching every workcell boundary. That's when I extracted the common layer.

The lesson isn't "build the abstraction first." It's that the right time to extract is when the cost of drift exceeds the cost of indirection — and that happened around workcell five or six. I should have been watching for it earlier. `backend/common/` now centralizes HMAC auth, LLM budgets, retry logic, idempotency tokens, and audit event emission. Every workcell that touches an external service routes through it.

## 2. A single LLM budget is not a budget — it's a suggestion

The first version had one token quota guard at the top of the outreach pipeline. That was naive. When multiple workcells share a budget signal, whoever runs first consumes freely, and the last workcell in the chain gets throttled. The actual behavior was non-deterministic depending on queue timing.

I replaced the flat budget with a layered chain: per-workcell quotas tracked through `portfolio_controller`, with priority rebalancing at dispatch. The evidence is the `portfolio_controller` workcell added in v1.3.0 — it exists specifically because a single quota signal was being ignored in practice by whichever workcell happened to schedule first.

## 3. The recovery/ directory exists because git history isn't a design artifact

When I redesigned the governance layer in v2.0.0, I had prototype code that was wrong but instructive. If I'd deleted it and committed, the failure mode it demonstrated would have been invisible to anyone reviewing the system later — including me six months from now.

`recovery/` is an explicit separation of prototype and superseded code from the active runtime. It's not a graveyard; it's documentation of dead ends with the context intact. Git history tells you what changed. `recovery/` tells you why the old approach was abandoned and what failure it exhibited. These are different things.

## 4. The Stake Sentence requirement revealed that my defaults were wrong

Early in the design, LLM dispatch calls could be made without operator-authored context. The assumption was that the workcell had enough internal signal to govern its own calls. That assumption was wrong. In practice, calls were made with generic system prompts that gave the model no grounding in what this particular prospect needed or what constraint was active.

The Stake Sentence requirement — enforced at dispatch — forces an operator-authored sentence into every LLM call that describes the business context and the constraint being honored. Implementing it revealed how many of my early call sites had no such context at all. The enforcement is in the dispatch layer because that's the only place where I could guarantee it ran before the model saw anything.

## 5. Fail-open was the wrong default at almost every governance gate

I calibrated several early governance checks to fail-open because I was worried about blocking legitimate outreach. The result was that the system would proceed on ambiguous signals and generate outreach that later failed the 11-workcell guardrail check — wasted LLM spend, wasted SES sends, and sometimes contact with prospects that should have been filtered.

The correct calibration is fail-closed on admission and fail-open only on transient infrastructure errors. The `check_action` gate validates runtime calls against Codex-declared rules and is fail-closed. SQS worker errors that can't be attributed to a bad record go to DLQ and replay rather than being silently dropped. Getting this right required observing specific failure modes, not reasoning about them in advance.

## 6. The DLQ + replay pattern was retrofitted after observing silent failures

The original SQS worker code returned success on errors it couldn't handle cleanly. The rationale was "let it move on." The consequence was that failed prospect records disappeared with no trace. I only discovered this when a batch of 40 records produced zero outreach and I had no way to diagnose why.

DLQ routing and replay were added as a direct response to that failure mode. The design is in `backend/common/` because DLQ handling is not workcell-specific — every SQS worker sidecars the same pattern. The 10 worker sidecars all inherit this through common infrastructure rather than implementing it independently.

## 7. The 0.62 signal filter threshold is empirical, not principled

`signal_filter` uses a 7-axis `ProspectSignal` composite and admits prospects above a 0.62 threshold. That number was derived from scoring a batch of known-good and known-bad prospects after v1.3.0 shipped. It is not a theoretically derived value. I mention this because engineers reviewing the codebase will find a hard constant and wonder where it came from. It came from observation.

The lesson is that admission thresholds for LLM pipelines should be treated as hyperparameters calibrated against real outcomes, not as constants reasoned from first principles. The 0.62 value should be revisited whenever the prospect corpus changes meaningfully.

## 8. Local dev and cloud deployment are not behaviorally equivalent, and pretending otherwise wastes time

The Docker Compose dev environment and the Cloud Run deployment share a codebase but not an identity model, a secret resolution path, or a network topology. Early in development I treated behavioral parity as a goal and spent time trying to close the gaps. I stopped doing that.

The right frame is: local dev is for fast iteration on workcell logic; cloud deployment is where integration and governance behavior are observed. Tests that target environment-specific behavior (SES send rates, SQS visibility timeouts, Neo4j cluster auth) run against cloud-adjacent state, not the local compose stack. The 416 test files reflect this — unit coverage runs locally, integration surface runs against real infrastructure.

## 9. What I would do differently on day one

Extract `backend/common/` at workcell three, not workcell fifteen. Enforce the Stake Sentence requirement in the dispatch layer from the first LLM call. Instrument DLQ routing before observing the first silent failure. Treat the governance Codex as a constraint on code, not as a companion document — the `check_action` gate linkage should have been day-one architecture, not a v2.0.0 addition. And: calibrate fail-closed by default everywhere, accept the friction of occasional over-blocking, and adjust from evidence.
