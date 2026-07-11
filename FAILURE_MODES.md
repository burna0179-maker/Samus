# Failure Modes

This document catalogs the failure modes I identified during design, encountered during development, or discovered in production operation. It is written for engineers reviewing this system — not as marketing, but as an honest accounting of what I built defenses for and where the gaps remain.

---

## Handled Failure Modes

### SQS message lost, duplicated, or redelivered

SQS provides at-least-once delivery. Every worker sidecar treats re-delivery as the default case, not the edge case. Each job carries an idempotency key derived from stable message attributes; the worker checks a DynamoDB conditional write before performing any side-effectful operation. If the write conflicts, the job is a duplicate and is discarded without reprocessing. Messages that exceed the retry threshold move to a Dead Letter Queue. I built a replay path from the DLQ for diagnosed failures — manual trigger, not automated, because automated DLQ replay without root-cause investigation tends to reproduce the original failure.

### LLM provider over-budget

The LLM budget chain has four layers: a global $1/day cap, a model-floor selection that downgrades to cheaper models before refusing, a circuit breaker that trips on sustained overrun, and a per-workcell quota that isolates one workcell's spend from others. At the bottom of this chain, the failure behavior is split by consequence: persistence calls (writing enrichment data, scoring leads) fail-open because an unwritten enrichment is a bounded, recoverable loss. Outreach calls fail-closed because sending an uncontrolled message to a prospect is not bounded — I cannot unsend it. This distinction is explicit in the circuit breaker configuration and is not an accident.

### Stake Sentence missing or malformed

Every outreach job requires a Stake Sentence — a human-readable constraint that scopes what the LLM is permitted to say. If the Stake Sentence is absent or fails validation, the dispatch is rejected before any LLM call is made. This is a hard stop, not a logged warning. The design intent is that a missing Stake Sentence indicates a caller that bypassed the normal job construction path, which is itself a signal of a configuration or integration error that should surface loudly rather than proceed silently.

### Duplicate outreach

Before any SES send, the outreach workcell checks a suppression list and an idempotency store keyed on (prospect_id, campaign_id, message_hash). A DynamoDB conditional write gates the actual send. If the prospect has already been contacted in the same campaign window, the job exits cleanly without sending. This exists because SQS can redeliver and because upstream callers occasionally resubmit jobs on retry.

### SSRF in the SEO crawler

The SEO enrichment workcell fetches external URLs as part of prospect research. I wrapped all outbound fetch calls in a SSRF-safe utility in `common/` that validates the resolved IP against RFC-1918 ranges and blocks redirects to internal targets. This was a deliberate defense built before the crawler was wired to production — SSRF in an enrichment worker that can reach internal AWS services would be a serious exposure.

### Stripe webhook replay

Stripe can deliver webhooks more than once and does so on retry. The webhook handler performs an atomic DynamoDB conditional write on the event ID before executing any billing state change. Replayed events are detected and discarded. This is table-stakes for any Stripe integration, but I want to be explicit that it is implemented rather than assumed.

### Immutable baseline drift

Several workcells depend on a pinned configuration baseline. At boot, each service computes a hash of its baseline artifact and compares it to a stored expected value. A mismatch halts startup rather than running with a potentially corrupted configuration. This catches accidental mutation during deployment and distinguishes "configuration was intentionally updated" from "something changed the file on disk."

---

## Partially Handled or Deferred Failure Modes

### Multi-instance in-process limiter divergence

Rate limiters and nonce stores are in-process. At one replica, this is correct and fast. At two replicas, each instance has an independent view of the rate window, so a caller could hit both instances and effectively double the allowed rate. In the current deployment (single Cloud Run instance per workcell) this is not a live issue, but it is a latent failure waiting for the first scale-out event. I documented the risk and deferred the fix; the remediation path is a shared Redis or DynamoDB backing store.

### SQS queue not provisioned

If an expected SQS queue does not exist at worker startup, the worker polls against a missing resource and silently drops messages. There is no startup assertion that all expected queues are present. This has caused confusion during environment setup when Terraform-managed infrastructure was partially applied. The fix is a boot-time queue existence check that fails fast.

### Cross-cloud AWS latency spike

The platform runs primarily on GCP Cloud Run but calls AWS services (DynamoDB, SES, SQS) across the public internet. There is no circuit breaker at the GCP→AWS boundary. A latency spike in AWS us-west-1 will cause request timeouts in Cloud Run workers, which will then exhaust their retry budget and land messages in the DLQ. This is recoverable but not graceful. I have not built adaptive backoff at this boundary beyond standard HTTP retry logic.

### `recovery/` code mistaken for active

The `recovery/` directory contains superseded design artifacts — earlier approaches that were replaced but preserved for reference. They are not imported by any active module, but they are present in the repository tree with no machine-enforced marker distinguishing them from live code. A reviewer or future contributor could reasonably mistake them for active paths. I have not added tooling (a linter rule, an import guard, or a directory-level README) to enforce the boundary.

---

## Known Gaps I Would Address Next

**No distributed tracing.** When a job fails across the SQS → worker → DynamoDB → SES chain, reconstructing the sequence from logs requires manual correlation of trace IDs across four separate log streams. I have structured logging and correlation IDs, but no tracing backend (Jaeger, Cloud Trace, X-Ray) is configured. Root-cause analysis on subtle failures is slower than it should be.

**No formal SLOs and therefore no alerting.** I know empirically what "normal" looks like for job latency and queue depth, but I have not codified it. There are no SLO definitions, no error budget, and no alert definitions attached to any monitoring backend. This means I detect problems when I notice them, not when they cross a threshold.

**Voice artifact retention has no policy.** Call recordings and transcriptions are stored, but there is no defined retention window, no automated deletion, and no documented legal basis for the retention period. For a system handling outbound sales calls, this is a compliance gap that needs a written policy and an enforcement mechanism before the system operates at any meaningful scale.
