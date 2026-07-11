# Interview Guide

This is a preparation reference, not a script. It records the architectural reasoning I would use to answer technical interview questions grounded in this system. Detailed question-by-question reasoning lives in [`INTERVIEW_NOTES.md`](../INTERVIEW_NOTES.md).

---

## One-sentence description

I built Samus as a distributed Python platform that decomposes sales automation into independently deployable workcells sharing security, governance, messaging, persistence, observability, and bounded AI infrastructure.

---

## Core architecture questions

### Why not a monolith?

A monolith would have reduced operational complexity — fewer containers, no inter-service authentication, simpler debugging. I chose decomposition because the design goal was to demonstrate reusable platform boundaries: HMAC identity, idempotency, DLQ routing, and LLM budget enforcement that any workcell inherits without reimplementing. For a smaller deployment or a team that prioritized simplicity, I would consolidate low-traffic workcells while preserving service-layer interfaces so the consolidation is reversible.

### Why support both HTTP and SQS?

HTTP keeps local development practical — I can start a single workcell and call it without SQS provisioning. SQS gives durability, independent consumers, and retry semantics for work that can't be dropped. The critical discipline is keeping the service layer path-agnostic so both dispatch modes produce identical business outcomes. I enforce this with parity tests.

### How are duplicate side effects prevented?

The pattern is claim-before-execute: task and provider event IDs are written to DynamoDB with a conditional expression before the side effect fires. If the write fails (ID already claimed), the handler returns early. DLQ routing captures failures. Append-only JSONL ledgers support reconciliation and replay. The pattern is explicit in the SQS worker base class in `backend/common/`.

### Why multiple databases?

DynamoDB fits durable keyed state and atomic coordination. JSONL fits append-only audit and operator recovery. SQS fits work queues with retry semantics. Neo4j fits controlled relationship traversal. Each store was chosen for a distinct access pattern. The tradeoff is reconciliation complexity and operational burden — four systems to monitor and operate.

### How is AI governed?

Every LLM call passes through a four-layer enforcement chain in `backend/common/llm_client.py`: global daily spend cap → model floor → circuit breaker → per-workcell token quota. Maximum one call per job, enforced in the wrapper. Outreach paths additionally require a human-authored Stake Sentence before any LLM call fires. The Codex (`docs/codex/`) defines 11 guardrails, and `check_action` validates calls against them at dispatch time.

### What would you change for production scale?

- Automate CI and produce reproducible test artifacts.
- Replace in-process limiters with a shared backend (Redis or DynamoDB).
- Implement Workload Identity Federation instead of long-lived cross-cloud credentials.
- Define SLOs and instrument alerting.
- Add a distributed tracing backend and propagate trace context across SQS.
- Separate the request-serving and long-running worker surfaces more explicitly in Cloud Run configuration.
- Define Cloud Run autoscaling parameters from production traffic data rather than defaults.
- Exercise restore and failover procedures against real DynamoDB backup.

---

## STAR narrative

**Situation:** A growing automation system was accumulating independent workflows that each duplicated authentication, retry logic, cost management, and observability.

**Task:** Design a platform that could add capabilities without duplicating cross-cutting infrastructure, and govern customer-facing actions without constant manual review.

**Action:** I decomposed the runtime into 21 FastAPI workcells, built a shared library (`backend/common/`) that all workcells inherit, designed HTTP/SQS dual dispatch with a path-agnostic service layer, introduced per-service HMAC identity and caller grants, implemented a four-layer LLM budget chain, required human-authored Stake Sentences for outreach, containerized the full stack with 22 Dockerfiles, and built 416 test files covering service logic, governance validation, and integration paths.

**Result:** A coherent platform where every workcell gets security, observability, idempotency, and cost governance without implementing any of it. The strongest result is architectural consistency across 21 services over six versions (v1.0 → v2.1.1), with v2.1.1 additions landing as additive seams that didn't break existing contracts.

---

## Phrasings to use

Instead of "enterprise-grade," say: validated by an automated test suite across 21 independently deployed services.  
Instead of "fully autonomous," say: policy-governed, with human authorization required for customer-facing actions.  
Instead of "production proven," say: deployed to GCP Cloud Run with AWS-backed persistence.  
Instead of "infinitely scalable," say: designed for independent workcell scaling; current in-process limiters require shared state for multi-instance guarantees.  
Instead of "zero-trust," say: includes per-service HMAC authentication, SSRF protection, caller grants, and immutable baseline verification.

---

## Claims to avoid

Do not say: fully autonomous, enterprise-grade, infinitely scalable, zero-trust, production proven, highly available.
