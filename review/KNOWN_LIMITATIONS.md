# Known Limitations

These are limitations I am aware of and have chosen to document rather than obscure. For each: what the limitation is and why it exists. Remediation paths live in [`KNOWN_TECHNICAL_DEBT.md`](../KNOWN_TECHNICAL_DEBT.md).

---

## CI pipeline absent

No automated CI workflow exists in this repository. The test suite (416 files, 4,582 test functions) is the implementation, but a reviewer must run it locally to verify. This is the highest-priority gap for portfolio presentation. A fresh `pytest` run on the main branch produces the evidence; the absence of CI means it is not reproducible on demand.

## Branch state

The active branch is significantly ahead of its remote and includes uncommitted changes. For a reviewer inspecting the repository, the counts and documentation reflect the working tree, not the last pushed commit. This is a process discipline gap, not an architectural one.

## In-process limiters are per-instance

Rate limiters, nonce stores, and idempotency caches in `backend/common/` are in-process. Under Cloud Run scaling with multiple instances, these provide no cross-replica guarantee. The fix is a shared backend (Redis or DynamoDB-backed) for these stores — deferred because the current deployment scale doesn't yet require it, but a known gap for multi-instance operation.

## Cross-cloud identity uses long-lived credentials

Cloud Run services use AWS credentials stored in GCP Secret Manager to access DynamoDB, SQS, SES, and SNS. The correct architecture is GCP Workload Identity Federation mapped to AWS IAM roles. This is known and deferred due to implementation scope. The current approach relies on secret rotation discipline rather than machine-bound identity.

## Compose and Cloud Run are not operationally equivalent

Background polling workers, local disk writes, and cross-cloud identity behave differently in Compose versus Cloud Run. Local development exercises most code paths but does not reproduce queue durability, distributed rate limits, or cloud IAM behavior.

## Some authorization paths are configuration-gated

Certain advanced authorization and control paths require explicit configuration to activate. In a production deployment, these should be unconditional. The current state is a known gap, not a design choice.

## CLOUD_OPS_PLAYBOOK.md contains live infrastructure identifiers

`docs/CLOUD_OPS_PLAYBOOK.md` references the GCP project ID, service account names, and service URLs. This file requires sanitization before the repository is made public. It is retained in its current form because it documents the real deployment topology.

## No claim of high availability, formal verification, or production scale

This repository demonstrates implementation depth and architectural reasoning. It does not prove high availability, enterprise adoption, production traffic scale, formal security verification, or complete cloud portability. Any claim beyond what is visible in the repository is unsupported.

## Voice artifact retention

Customer voice recordings and transcriptions from the Vapi integration lack a formal retention policy. In a production deployment handling real customer data, a defined retention, access control, and deletion policy is required.

## Test reproducibility requires a fresh run

Test counts are from the working-tree snapshot. The evidence is most credible when accompanied by a CI run with duration, pass/fail summary, and coverage report. Run `pytest tests/ -v` to reproduce.
