# Performance Characteristics

This is an honest analysis of the performance properties of Samus as designed. It is not a benchmark report — I don't have reproducible CI performance data to publish, and publishing numbers without that would be misleading. What I can describe accurately is what the architecture optimizes for, what it deliberately accepts latency on, and what I would measure in a production environment.

---

## Where I Optimized for Latency

**Synchronous HTTP dispatch path**: The gateway (`backend/gateway/`) uses direct signed HTTP when a workcell doesn't have a queue URL configured. This path is used for development and for any workcell whose contract is request/response rather than fire-and-forget. The HTTP path has no queue polling interval and no SQS delivery lag — it's bounded by workcell processing time plus one network hop.

**Deterministic logic before LLM**: The pipeline runs deterministic checks — signal filter scoring, legitimacy gate, Stake Sentence validation, CAN-SPAM compliance — before any LLM call. This is not primarily a latency optimization; it's a cost optimization. But the effect is that a prospect who fails admission never pays LLM latency or cost. The most expensive operation in the pipeline is the last one to run, not the first.

**Signal filter gate before expensive enrichment**: The `signal_filter` workcell applies the 7-axis `ProspectSignal` composite and the 0.62 admission threshold before Apollo enrichment. Apollo enrichment involves external API calls with variable latency. Running the filter first means rejected prospects are dropped at near-zero cost and latency.

**DynamoDB access patterns**: All DDB access is by primary key — LLM budget records keyed by workcell, idempotency records keyed by message ID, Stake Sentence budget keyed by bucket day. No scan operations. Single-digit millisecond reads are the expected case, not the optimistic one.

---

## Where I Accepted Latency

**SQS dispatch path**: The async path routes messages through SQS, which introduces polling interval latency plus queue processing time. This is a deliberate tradeoff for durability: SQS gives retry semantics, visibility timeout, and DLQ routing that direct HTTP doesn't. For durable production work — outreach pipelines, multi-step enrichment chains — the latency cost is worth it.

**Cross-cloud calls (Cloud Run → AWS)**: Several workcells run on Cloud Run and call AWS services (SQS, SES, DynamoDB). Cross-cloud calls have higher latency than same-region calls — network egress, TLS handshake, and routing are all slower across providers. I accepted this because Cloud Run gives strong autoscaling and cost characteristics for the GCP-resident workcells, and AWS gives DynamoDB, SES, and SNS where those services are clearly better fits. The cross-cloud topology is a known latency budget line item, not an oversight.

**Neo4j traversals**: Relationship-heavy queries in Neo4j — prospect graph traversal, KG tiering, organizational connection scoring — are slower than DynamoDB key lookups by design. Graph traversals have latency proportional to the traversal depth and edge fanout, not a flat cost. I use Neo4j only for queries that actually need graph semantics; the organizational connection score in `backend/cognitive/` is the primary example. DynamoDB handles all single-entity state.

---

## Where I Optimized for Throughput

**SQS workers as independent consumers**: Each of the 10 worker sidecars polls its queue independently. They share no state and don't coordinate on job allocation. Throughput scales linearly with the number of workers up to queue saturation. Adding a worker sidecar increases throughput without changing any other part of the system.

**Parallel workcell deploys on Cloud Run**: Cloud Run autoscales workcell instances independently. A burst on the outreach workcell doesn't starve the SEO audit workcell — they scale separately. The cross-workcell isolation also means one workcell's latency regression doesn't cascade.

---

## LLM Cost Profile

The system is designed to stay under a $1/day global budget, not to maximize LLM throughput. This shapes the performance characteristics in ways that are easy to miss:

- The one-call-per-job ceiling means LLM cost per prospect is bounded and predictable. There's no long-tail cost distribution from jobs that spiral into multi-call chains.
- The Stake Sentence cap (default 10/day) is the real throughput ceiling. The system can't outrun the human's ability to write 10 deliberate sentences, and that's intentional.
- The circuit breaker absorbs model outage periods without saturating the retry queue. During an outage, no new LLM quota is consumed on doomed calls.

---

## JSONL Ledgers

The audit ledger, belief ledger, reward computations, and control tick log are JSONL append-only files. Append is fast — no index maintenance, no transaction overhead. Queries are slow — a full ledger read scales with file size. This is deliberate: these ledgers are for audit and retrospective analysis, not for real-time query. Any operational signal that needs fast lookup (budget state, idempotency records, queue depth) lives in DynamoDB.

---

## What I Would Measure in Production

If I were defining SLOs for this system, the candidates are:

- **Gateway dispatch p95 latency**, split by HTTP path vs SQS path. These have different latency profiles and should be tracked separately; an aggregate hides degradation on either.
- **LLM call latency and daily budget burn rate**, per workcell. Budget burn rate is both a cost signal and an early indicator of pipeline misconfiguration (unexpected job volume hitting the LLM layer).
- **SQS queue depth per worker**, as a backpressure signal. Queue depth growing without corresponding worker activity indicates a worker health problem. Queue depth growing with workers busy indicates a throughput ceiling being approached.
- **DynamoDB ConsumedCapacityUnits per workcell per operation type**. Because all access is by primary key, spikes in ConsumedCapacity indicate unexpected access patterns — scans introduced by a bug, or volume changes the provisioned capacity isn't sized for.
- **DLQ depth**. Non-zero DLQ depth is the signal that jobs are failing in ways the retry logic can't recover from. It should trigger investigation, not just alerting.
