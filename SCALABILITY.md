# Scalability

This document is an honest first-person analysis of where this platform scales and where it does not. I built it, I know its seams, and I am not going to oversell a design that has real constraints. The goal is to give a reviewer a clear picture of what the current architecture supports, what it would take to go further, and where I made deliberate choices that trade scalability for simplicity at the current operating scale.

---

## What Scales with This Design

**Independent workcell deployment.** The platform is structured as 21 FastAPI workcells, each deployable independently as a Cloud Run service. If the signal-scoring workcell is the bottleneck, I scale that one — I do not redeploy a monolith. This was a deliberate architectural choice and it pays off in targeted scaling, independent release cycles, and fault isolation. A failure in the SEO enrichment workcell does not take down the outreach workcell.

**SQS-backed async workers.** The 10 SQS worker sidecars are natural fan-out targets. Adding capacity means adding consumer instances; SQS handles the distribution. The queue absorbs load spikes without applying backpressure to upstream callers. If inbound job volume triples overnight, the queue depth rises and workers drain it — the workcells that enqueue jobs are not blocked. This is the right pattern for a workload that is bursty by nature (prospecting runs, campaign triggers).

**DynamoDB access patterns.** I designed the DynamoDB access patterns around keyed reads — prospect lookups by ID, idempotency checks by composite key, suppression checks by prospect-campaign pair. DynamoDB scales horizontally without schema changes at these access patterns. I am not running table scans in hot paths. The read/write capacity can be provisioned or set to on-demand; the application code does not need to change.

**Signal filter as a gate before expensive enrichment.** The platform runs a signal scoring pass (threshold 0.62) before committing to LLM enrichment, external API calls, or outreach queue insertion. This is not just a quality filter — it is a cost and throughput gate. At higher lead volumes, the filter absorbs the load increase without proportionally increasing downstream cost. A lead that scores 0.40 consumes one lightweight scoring call and nothing else.

---

## What Does Not Scale with Current Design

**In-process rate limiters and nonce stores.** This is the most significant scalability constraint in the current implementation. Rate windows, nonce stores, and idempotency caches are held in each worker process's memory. At one instance per workcell — the current Cloud Run configuration — this is correct. At two instances, the state diverges immediately. A rate limit configured for 10 calls per minute becomes 20 effective calls per minute across two instances. A nonce consumed on instance A is unknown to instance B. Any horizontal scale-out of the SQS workers triggers this problem.

**UCB1 bandit assumes single-writer update cadence.** The strategy workcell uses a UCB1 multi-armed bandit backed by DynamoDB to select between outreach strategies. The UCB1 algorithm assumes that the agent reading and updating the reward estimates is operating on a consistent view of the counts. At a single writer, the DynamoDB update is effectively serialized. At multiple concurrent writers — multiple worker instances processing strategy jobs simultaneously — the updates race. The bandit will still converge, but the convergence behavior is undefined by the algorithm's design and I have not characterized it empirically at concurrency greater than one.

**JSONL ledgers on local disk.** Several components write JSONL ledgers to local disk for audit and replay purposes. On Cloud Run, local disk is ephemeral and per-instance. A ledger written by instance A is not visible to instance B and is lost on container restart. This is not a correctness problem at one instance, but it becomes one the moment the deployment scales out or the container cycles.

**Cross-cloud AWS latency compounds under load.** The GCP Cloud Run services call AWS us-west-1 over the public internet. At low call volumes, the latency is acceptable and consistent. Under load — many concurrent enrichment or outreach jobs — the cross-cloud calls are the limiting factor in end-to-end throughput. There is no circuit breaker at this boundary and no adaptive backoff beyond standard HTTP retry logic. A sustained AWS latency event will exhaust the Cloud Run request timeout budget and land jobs in the DLQ rather than failing fast and alerting.

**No horizontal scale configuration defined.** Cloud Run supports minimum and maximum instance counts and CPU/memory autoscaling. I have not defined these parameters for production. The current deployment runs on default Cloud Run settings, which means scale-out behavior under load is not configured intentionally. This is acceptable during early operation; it is not acceptable before a traffic event.

---

## What the Design Would Need for Multi-Region or High-Availability

**Shared state backend for limiters.** The in-process rate limiters and nonce stores need to move to a shared backend before any meaningful horizontal scale-out. Redis is the natural fit for rate window state (atomic increment, TTL-based expiry). DynamoDB conditional writes are the right fit for nonces (idempotent by construction). This is a targeted change to the common layer — the workcell business logic does not change, only the backing store.

**DLQ visibility and replay UI.** The DLQ exists and catches failed jobs, but there is no operational interface for inspecting queue depth, reviewing failed message payloads, or triggering targeted replay. At low volume I can use the AWS console directly. At any meaningful scale, I need a purpose-built interface or at minimum a runbook-level script that surfaces the right information without requiring console access.

**Formal SLOs and alerting.** I cannot operate a multi-region system without defined latency and error-rate targets. The current monitoring posture is observational — I look at logs when something seems wrong. That does not work at scale. The SLOs need to be defined, written down, and connected to alerts that fire without a human actively watching.

**Cross-cloud identity via Workload Identity Federation.** Long-lived AWS credentials stored in Secret Manager are manageable at one deployment target and one operator. They become a rotation and audit burden at scale. The correct solution is OIDC federation: Cloud Run workloads present a GCP-issued token, AWS IAM validates it, no long-lived credential exists. This is a configuration change, not an architectural one, but it is a prerequisite for operating the cross-cloud boundary at any serious scale.

**CDN or regional routing for intake.** The intake endpoint is a single Cloud Run service in us-west1. If intake callers are geographically distributed or if the platform expands to serve multiple regions, a single-region intake becomes a latency and availability liability. A CDN layer or regional anycast routing in front of intake is the standard solution. This is not a near-term concern at current volume, but it is the right place to start a multi-region design.
