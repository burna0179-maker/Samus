# Engineering Summary

I built Samus to solve a specific problem: a growing automation system where every new capability duplicated infrastructure. The answer was a platform that makes security, observability, retry logic, and cost accounting into shared services — not optional libraries each workcell reimplements.

This document summarizes what the repository demonstrates and where I would invest next.

---

## What I Built

Samus is a distributed Python platform. Twenty-two FastAPI workcells plus fifteen SQS worker sidecars handle prospecting, CRM, outreach, voice, SEO analysis, finance, intake, memory, strategy, and governance. Every workcell shares a common runtime from `backend/common/` that provides HMAC authentication, idempotency, DLQ and replay, signed HTTP client, SSRF-safe fetch, Prometheus metrics, audit events, and a four-layer LLM budget enforcement chain.

The platform runs on three deployment targets — Docker Compose for local development, an Ubuntu VM for the development stack, and GCP Cloud Run for production (29 services in `${GCP_PROJECT}`, us-west1) — with cross-cloud AWS persistence (DynamoDB, SQS, SES, SNS).

---

## Strongest Evidence in This Repository

### Platform abstraction

`backend/common/` centralizes cross-cutting behavior that would otherwise drift across 22 services. The LLM budget chain (global cap → model floor → circuit breaker → per-workcell quota) is a single implementation that every workcell inherits. One change in `llm_client.py` propagates everywhere. Without this abstraction, governing AI spend across a distributed system is nearly impossible.

### Distributed execution design

The gateway routes via synchronous HTTP or durable SQS based on environment configuration. The service layer handles both paths identically — no duplicated business logic. SQS workers implement polling, visibility-timeout management, idempotency, DLQ routing, and graceful shutdown. I validated both paths with parity tests.

### Risk-calibrated side effects

Outreach, voice, finance, and intake paths each have defense in depth: HMAC signatures on inter-service calls, Stake Sentence enforcement before any outreach LLM call, CAN-SPAM compliance checks, suppression lists, idempotency keys before external API calls, and append-only audit ledgers. The Stake Sentence requirement (one human-authored sentence per Opportunity, enforced with anti-template and dedup guards, daily cap, fail-CLOSED) reflects a deliberate choice to keep a human in the authorization chain for customer-facing actions.

### Operational completeness

The repository includes Docker Compose configuration, 22 per-workcell Dockerfiles, Cloud Run deployment scripts, PowerShell operator scripts with DPAPI secret management, health probes, scheduled task registration, DLQ inspection tooling, and data initialization helpers. This is not a demo that needs a real deployment wrapped around it. It is the deployment.

### Test surface

505 test files and ~5,617 test functions cover workcell service logic, governance and Codex validation, identity and boot integrity, stake sentence enforcement, integration paths, and architecture snapshots. The evidence is strongest when accompanied by a CI run — the absence of a CI pipeline is the clearest gap in this repository's portfolio presentation.

---

## Signals a Senior Reviewer Should Notice

- Cross-cutting concerns implemented once and inherited, not copied.
- Explicit architecture decision records (8 ADRs) with rejected alternatives.
- DLQ, replay, and idempotency designed as first-class concerns, not retrofit.
- Security beyond endpoint authentication: SSRF protection, HMAC caller grants, immutable baseline boot verification, graph schema constraints.
- A layered cost governance model that makes AI inference an operationally managed dependency.
- `recovery/` clearly separating prototype and superseded code from active architecture.
- Version evolution (v1.0 through v2.2.1) showing that later additions landed as additive seams without breaking existing contracts.

---

## What I Would Invest in Next

**CI pipeline.** The test suite is the strongest implementation evidence in this repository. Without an automated run producing artifacts, a reviewer must take the counts on trust. This is the highest-priority gap.

**Distributed tracing backend.** I have correlation IDs and Prometheus metrics wired. What's missing is a trace aggregation backend (Jaeger, Honeycomb) and the distributed trace context propagation across SQS. This would make incident root-cause analysis significantly faster.

**Shared state for in-process limiters.** Rate limiters, nonce stores, and idempotency caches are currently per-instance. Under Cloud Run scaling, these provide no cross-replica guarantee. The fix is a shared Redis or DynamoDB-backed store for these — a one-afternoon change per workcell.

**Workload Identity Federation.** The cross-cloud deployment uses long-lived AWS credentials bound via GCP Secret Manager. The correct solution is GCP Workload Identity Federation mapped to AWS IAM roles — no stored credential, machine-bound identity. I deferred this because it requires AWS IAM policy design beyond the current deployment scope.

**Formal SLOs.** I have health probes and Prometheus metrics but no formal SLO definitions, no alert thresholds, and no SLO-linked dashboards. For a production system handling customer communication, this gap matters.
