# Known Technical Debt

This is an engineering judgment document, not a feature wishlist. Each item below is something I am aware of, can explain, and have a concrete remediation path for. Some were deliberate tradeoffs made under time or complexity constraints. Some were discovered later. I am recording them here because a system I cannot honestly assess is a system I cannot responsibly operate.

---

## 1. No CI Pipeline

**Severity: High**

There is no automated CI pipeline connected to this repository. The test suite has 416 files and 4,582 test functions, but there are no run artifacts — no green badge, no historical pass rates, no coverage reports. A reviewer has to take my word that the tests pass in a clean environment.

Why it exists: I prioritized building the system over building the build system. The early iteration cycle was tight and the overhead of CI configuration felt like the wrong place to spend time when the architecture was still changing weekly. That calculus stopped being correct some time ago and I have not corrected it.

Fix: Connect GitHub Actions (or Cloud Build, given the GCP deployment target). The test suite already exists and runs locally. The work is configuration, not engineering. Estimated effort: one day. Urgency: this is the highest-priority debt item because it is the one most likely to undermine a reviewer's confidence in everything else.

---

## 2. In-Process Limiters Not Multi-Instance Safe

**Severity: High**

Rate limiters, nonce stores, and idempotency caches are held in process memory. At one replica, this is correct behavior. At two or more replicas behind a load balancer, each instance maintains an independent view of its limits. A caller who hits both instances can double the effective rate. A nonce that was consumed on instance A is unknown to instance B.

Why it exists: shared state backends (Redis, DynamoDB for hot keys) add operational complexity and a latency hit on every guarded operation. For a single-replica initial deployment, in-process state is simpler and faster. The current Cloud Run deployment runs one instance per workcell, so this has not caused a live incident.

Fix: Replace in-process stores with a shared backend. DynamoDB with conditional writes handles nonces well. Redis handles rate window state well. Each workcell would need a configurable backend adapter. Estimated effort: three to five days per workcell depending on how many guarded paths exist.

---

## 3. Cross-Cloud Identity Using Long-Lived Credentials

**Severity: High**

GCP Cloud Run services call AWS (DynamoDB, SES, SQS) using long-lived AWS access key credentials stored in GCP Secret Manager. There is no native IAM federation between GCP and AWS — Workload Identity Federation does not span the two clouds without explicit configuration that I have not implemented.

Why it exists: the cross-cloud design was chosen for service-level reasons (DynamoDB's read/write patterns, SES deliverability infrastructure). The identity solution was the fastest path to a working cross-cloud call. Long-lived credentials in Secret Manager are not a great security posture, but they are an auditable one — rotation is possible and the keys are not in code or environment variables.

Fix: AWS supports OIDC federation from external identity providers. GCP workloads can present a GCP-issued OIDC token that AWS IAM can validate directly. This would eliminate long-lived credentials and allow per-service role assumption. Estimated effort: one to two days to configure the IAM trust policy and update the credential acquisition path in the common layer.

---

## 4. CLOUD_OPS_PLAYBOOK.md Contains Real Infrastructure Identifiers

**Severity: Medium**

`CLOUD_OPS_PLAYBOOK.md` contains the real GCP project ID, service account email addresses, and service URLs for the production deployment. This file is in the repository and would be visible to anyone with repository access.

Why it exists: the playbook was written for operational use and I did not sanitize it before treating the repository as a portfolio artifact.

Fix: replace live values with `<PROJECT_ID>`, `<SERVICE_ACCOUNT>`, etc. before any public or portfolio exposure. This is a one-hour find-and-replace task. It does not require any system changes.

---

## 5. Local Disk Use in Some Workers

**Severity: Medium**

Some worker sidecars write intermediate artifacts to local disk — temporary files during enrichment processing, JSONL ledger appends. Cloud Run containers have an ephemeral local filesystem that is not shared across instances and is lost on container restart. This means that at one instance, local ledger writes survive a request cycle but not a restart. At two instances, they diverge immediately.

Why it exists: local disk is the simplest write target and was appropriate during the initial single-process design. The migration to Cloud Run did not fully resolve this.

Fix: move all writes that need durability to Cloud Storage (for blobs and ledgers) or DynamoDB (for structured state). This requires auditing all `open()` calls in worker code and reclassifying them as either truly ephemeral (acceptable on local disk within a single request) or durable (must move off-disk). Estimated effort: two to three days.

---

## 6. Uncommitted Working Tree — 91 Commits Ahead, Extensive Changes

**Severity: Medium**

The working tree has changes that have not been committed, and the branch is 91 commits ahead of origin. For a portfolio review, this means a reviewer cannot see a clean commit history that reflects the actual state of the system.

Why it exists: the development cadence has been fast and the commit discipline has not kept up. Some of the uncommitted changes represent work-in-progress that I was not ready to checkpoint.

Fix: audit the working tree, stage and commit completed work, and push to origin. Any genuinely in-progress work should be committed to a feature branch rather than left unstaged. This is a process discipline fix, not an engineering fix.

---

## 7. Some Authorization Paths Are Configuration-Gated

**Severity: Medium**

Several authorization checks in the HMAC caller-grant system are enabled by configuration flags rather than being always-on. This means that in an environment where the flag is absent or set incorrectly, an authorization check that should be mandatory becomes optional.

Why it exists: the flags were introduced to allow gradual rollout of the authorization layer without breaking existing integrations during development. They were not removed after the rollout completed.

Fix: remove the configuration gates and make the authorization checks unconditional. Any caller path that cannot pass the check should be updated to provide valid credentials rather than bypassing the check via configuration. Estimated effort: one day to audit all gated paths and remove the conditional logic.
