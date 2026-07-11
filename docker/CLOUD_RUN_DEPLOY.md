# Samus on GCP Cloud Run — Deployment Runbook

> **Status:** PLAN ONLY. Nothing in this document has been executed. It is a
> step-by-step runbook for the operator to deploy the 21-workcell Samus stack
> from the local Windows Docker Compose host onto GCP Cloud Run.
>
> **Author:** generated 2026-05-20 from the canonical `samus` worktree.
> **Revised:** 2026-05-20 — two operator clarifications folded in (see the
> *Revision note* below). **Companion docs:** `docker/DEPLOY.md` (local
> deploy), `docker/README.md`.

---

## Revision note — 2026-05-20 (standalone + experience-feedback)

Two operator clarifications changed the architecture. Both are reflected
throughout; this note is the index of what moved.

1. **The GCP Samus is STANDALONE.** It is explicitly *not* part of the
   Hustleforge ecosystem. It does NOT share the ecosystem's Hivemind, does NOT
   share the operator's local Neo4j Desktop DBMS, and there is NO
   cross-ecosystem multi-database migration. The GCP Samus gets its OWN
   dedicated, fresh AuraDB instance with its own (empty or minimally seeded)
   graph. This **simplifies §5 and W-4 substantially** — the old §5.2/§5.4
   "fold five agents' bare-name databases into one shared AuraDB" problem is
   gone. What remains is purely *internal* to the standalone instance: Samus's
   code carries a private-KG + "promote to a shared `hivemind` tier" two-tier
   idea, and §5.2 now works out how that maps onto one single-database AuraDB
   instance. Decisions **D-3 and D-6** are rewritten; **D-5** is unchanged.

2. **NEW REQUIREMENT — experience feedback to the ecosystem.** Although the
   GCP Samus is operationally standalone, the operator wants the Hustleforge
   ecosystem to *gain experiences* from it: high-confidence knowledge-graph
   promotions, operational outcomes, and CRM/deal results must flow BACK to
   the ecosystem's shared Hivemind. This is a **one-directional learning
   feed**, not a shared live database — the standalone property is preserved.
   The mechanism is designed in the new **§7 (Experience feedback to the
   ecosystem)**; it adds work-item **W-6** and operator decisions **D-8/D-9**.

The 30-service deploy plan, the tiered deploy sequence, the draft
`gcloud run deploy` commands, the cost analysis, and the W-1/W-2/W-3/W-5
work-items are unchanged except where the two clarifications touch them
(noted inline). Section numbers after the old §6 shifted by one to make room
for the new experience-feedback section; old §7 (open risks) is now §8, old §8
(work-items) is now §9, old §9 (decision summary) is now §10.

---

## 0. Why a runbook and not just `gcloud run deploy`

Samus today runs as a 33-container Docker Compose stack on a Windows host
(`Start-SamusStack.ps1`). The architecture has *seven* properties that are
free on a single Docker host and are NOT free on Cloud Run. Every one of them
must be solved before a single `gcloud run deploy` is meaningful. They are
catalogued in **§1**; the rest of the document is the fix for each.

### 1. Local-deployment assumptions that BREAK on Cloud Run

| # | Assumption (works locally) | Why it breaks on Cloud Run | Fixed in |
|---|---|---|---|
| A | **Neo4j at `bolt://host.docker.internal:7687`** — the operator's local Neo4j Desktop DBMS. | Cloud Run containers have no `host.docker.internal`; there is no host. The DBMS is on a private LAN. | §5 (AuraDB) |
| B | **One DBMS, many bare-name databases** — `samus`, `hivemind`, `anita`, `darwin`, … selected via `NEO4J_DATABASE`. | A single AuraDB instance is **single-database** (the DB is always `neo4j`). `USE samus` will fail. | §5.2 |
| C | **Append-only JSONL on a bind mount** — `stripe_events.jsonl` (webhook idempotency), `upsell_queue.jsonl` (nurture queue), every `*_audit.jsonl` ledger — all under `/opt/samus/data/...` on the `samus-data` named volume. | Cloud Run's container filesystem is **ephemeral** and **per-instance**. Two instances do not share it; a scale-to-zero wipes it. Webhook idempotency and the upsell queue would silently corrupt. | §2.3, §3 (per-service notes) |
| D | **Inter-service Docker DNS** — workcells call `http://samus-finance:8080`, `http://samus-gateway:8080`, etc., resolved by the `samus-internal` bridge network. | Cloud Run has no shared network namespace. Each service is a distinct `https://<svc>-<hash>-uw.a.run.app` URL on TLS/443. Compose names do not resolve. | §2.4, §3 |
| E | **Always-on SQS worker sidecars** — 9 `python -m backend.<svc>.worker` containers run `poll_loop()` forever (`restart: unless-stopped`). | Cloud Run is request-driven. By default CPU is throttled to ~0 between requests and instances scale to zero — a background `while` loop is suspended. SQS messages would sit until DLQ. | §2.5 |
| F | **Secrets injected from Windows DPAPI** — `Start-SamusStack.ps1` decrypts DPAPI and exports env vars for the lifetime of `docker compose`. | Cloud Run runs in GCP with no DPAPI, no PowerShell. Secrets must come from GCP Secret Manager. | §2.6 |
| G | **Stripe webhook reaches the local finance container via an ngrok tunnel** (`millard-unruffable-reginia.ngrok-free.dev/stripe_webhook`). | ngrok pins to the local host. A Cloud Run finance service has its own stable `*.run.app` URL; the webhook destination must be repointed and the cutover sequenced so no payment event is lost. | §6 |

Two more, minor but real:

| # | Assumption | Break | Fix |
|---|---|---|---|
| H | `samus-data-init` busybox container `chown`s the named volume as root before workcells start. | Cloud Run has no shared volume to initialise; the init container has no analogue and no purpose. | Drop it. The base image already runs as uid 10001; the only writable path on Cloud Run is `/tmp`. |
| I | `read_only: true` rootfs + `tmpfs: /tmp`. | Cloud Run's rootfs is already read-only by default and `/tmp` is an in-memory tmpfs by default. | No action — Cloud Run matches the Compose posture for free. Any code path that writes outside `/tmp` (the JSONL ledgers) is caught by §2.3. |

---

## 1. Target architecture

> **Standalone boundary.** The GCP Samus is a *self-contained* deployment. The
> only links it has to the Hustleforge ecosystem are (a) it reuses the same
> AWS SQS/DynamoDB account for queues/tables — a pre-existing operational
> dependency, not an ecosystem coupling — and (b) the **one-way experience
> feed** designed in §7, which exports learnings but never reads ecosystem
> state. The GCP Samus's Neo4j is its OWN AuraDB instance; it never connects
> to the operator's local Neo4j or the ecosystem Hivemind.

### 1.1 Topology

```
                          ┌───────────────────────────────────────────────┐
   Stripe  ──webhook──▶    │  samus-finance         (Cloud Run, public-ish) │
   Vapi    ──webhook──▶    │  samus-voice           (Cloud Run, public-ish) │
   Browser ──onboarding─▶  │  samus-intake          (Cloud Run, public)     │
   Operator/SES events ─▶  │  samus-feedback        (Cloud Run, public-ish) │
                          └───────────────────────────────────────────────┘
                                          │  HTTPS + HMAC (id-token)
                                          ▼
   marketing site ──────▶  ┌───────────────────────────────────────────────┐
   api.hustleforge.tech    │  samus-gateway         (Cloud Run, public edge)│
                          └───────────────────────────────────────────────┘
                                          │ HTTPS *.run.app, OIDC id-token
            ┌─────────────────────────────┼─────────────────────────────────┐
            ▼                             ▼                                 ▼
   ┌──────────────────┐        ┌──────────────────┐              ┌──────────────────┐
   │ 17 internal      │        │ samus-crm        │   ...        │ samus-memory     │
   │ workcell services│        │ (ingress=internal)│              │ (ingress=internal)│
   │ (ingress=internal)│       └──────────────────┘              └──────────────────┘
   └──────────────────┘
            │  business workcells with worker.py also run as ...
            ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │  9 SQS worker services — min-instances=1, CPU always-allocated,       │
   │  no ingress, drain AWS SQS queues via poll_loop()                     │
   └──────────────────────────────────────────────────────────────────────┘

   Shared state (this GCP Samus only — the standalone boundary):
     • Neo4j AuraDB         (DEDICATED standalone instance — fresh graph,
                             two internal tiers; replaces host.docker.internal)
     • AWS SQS + DynamoDB   (us-west-1 — unchanged, cross-cloud)
     • Firestore (Native)   (Stripe idempotency log + upsell queue — replaces JSONL)
     • GCS bucket           (generated artifacts — replaces samus-data volume)
     • Secret Manager       (all credentials — replaces DPAPI)

   One-way link OUT of the standalone boundary (NEW — §7):
     • Experience feed      (periodic export of KG promotions + operational
                             outcomes + CRM/deal results → GCS export bucket
                             → ecosystem ingests into its shared Hivemind.
                             Read-only from the ecosystem's side; the GCP
                             Samus never reads back.)
```

### 1.2 Service count

The 21 workcells become **21 Cloud Run "app" services** plus **9 Cloud Run
"worker" services** (one per workcell that ships a `backend/<svc>/worker.py`:
leadgen, prospecting, scaffold, fulfillment, feedback, outreach, optimizer,
proposal, seo — and `crm` if its worker is kept on Cloud Run). Total **30
Cloud Run services**. The 3 existing placeholder services
(`samus-2026`, `samus-feedback-2026`, `samus-intake-2026`) are reused/renamed
(see §1.4).

> The experience feed (§7) is *not* a 31st Cloud Run service in the
> recommended design — it is a small Cloud Run **job** on a Cloud Scheduler
> cadence (D-9), or, if the operator picks the in-process option, a thread in
> `samus-memory`. It does not change the 30-service count of the core stack.

> **Operator decision D-1 — worker model.** Each worker can be (a) a separate
> always-on Cloud Run *service* (`min-instances=1`, CPU always-allocated, no
> ingress), or (b) a Cloud Run *job* triggered by Cloud Scheduler every N
> minutes, or (c) a co-process inside the app service (`run_forever` in a
> thread). This runbook recommends **(a)** — it is the closest 1:1 to the
> Compose sidecars, keeps the long-poll latency low, and the cost of 9
> always-on `min=1` services at low CPU is bounded (see §2.5 / §8). Jobs (b)
> add 15-min worst-case latency and lose SQS long-poll efficiency; co-process
> (c) is fragile because Cloud Run throttles CPU between requests.

### 1.3 Ingress posture

| Group | Services | `--ingress` | Auth |
|---|---|---|---|
| Public edge | `samus-gateway`, `samus-intake` | `all` | gateway: operator-console bearer + HMAC; intake: CORS allow-list + rate-limit + optional CAPTCHA |
| Webhook receivers | `samus-finance`, `samus-voice`, `samus-feedback` | `all` | per-provider signature verify (Stripe-Signature, Vapi HMAC). Note: webhooks are unauthenticated at the Cloud Run layer — they MUST keep `--allow-unauthenticated` and rely on app-layer signature checks. |
| Internal workcells | the other 16 app services | `internal` | HMAC middleware + Cloud Run IAM (`run.invoker`) |
| Workers | 9 worker services | `internal` (or no ingress) | n/a — no inbound HTTP; they only poll SQS |

> `--ingress internal` means the service is only reachable from inside the
> VPC / from other Cloud Run services in the same project via the internal
> path; combined with `--no-allow-unauthenticated` it requires an OIDC
> id-token. See §2.4.

### 1.4 Naming

Existing placeholders use a `-2026` suffix. **Operator decision D-2 — naming
convention.** Recommend dropping `-2026` and standardising on
`samus-<workcell>` for app services and `samus-<workcell>-worker` for workers,
matching the Compose container names exactly. The 3 placeholders are then:
`samus-2026` → recreate as `samus-gateway`; `samus-feedback-2026` →
`samus-feedback`; `samus-intake-2026` → `samus-intake`. If the operator
prefers to keep the placeholder names to avoid re-issuing URLs, every
`*_URL` env var in §3 must use the `-2026` form instead — pick one and be
consistent.

> Note: `Pull-SamusCloudState.ps1` Sink 3 currently filters Cloud Logging on
> `service_name="{0}-2026"`. If D-2 standardises on un-suffixed names, that
> filter string must be updated (it is also touched by W-6 — see §7.4).

---

## 2. Prerequisite infrastructure — provision in this order

Each step is a discrete operator action. Steps marked **[OPERATOR DECISION]**
need a human choice before the command can be finalised.

### 2.0 Project, APIs, identity

```bash
# Already done: project ${GCP_PROJECT}, region us-west1, AR repo `samus`.
gcloud config set project ${GCP_PROJECT}
gcloud config set run/region us-west1

# Enable required APIs (idempotent).
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  firestore.googleapis.com \
  cloudscheduler.googleapis.com \
  cloudbuild.googleapis.com
```

**Runtime service account.** Do not run on the default compute SA. Create one
dedicated SA so IAM is auditable and least-privilege:

```bash
gcloud iam service-accounts create samus-runtime \
  --display-name="Samus Cloud Run runtime"
# SA email: ${SAMUS_RUNTIME_SA}@${GCP_PROJECT}.iam.gserviceaccount.com
```

Grant it exactly:
- `roles/secretmanager.secretAccessor` (read secrets — can be scoped per-secret)
- `roles/datastore.user` (Firestore read/write — §2.3)
- `roles/storage.objectAdmin` on the artifact bucket only (§2.3)
- `roles/run.invoker` so it can call sibling internal services (§2.4)

> **Note:** AWS access (SQS/DDB) is NOT a GCP IAM grant — it is an AWS IAM
> user's access key, delivered as a secret (§2.6 / §8).

> **Note (W-6).** The experience feed (§7) writes export objects to a separate
> GCS bucket. If the feed runs as a Cloud Run job, it gets its OWN runtime SA
> (`samus-experience-export`) with `storage.objectCreator` on the export
> bucket only — see §7.4. The core `samus-runtime` SA does not need export-
> bucket write.

### 2.1 Artifact Registry — already exists

`us-west1-docker.pkg.dev/${GCP_PROJECT}/samus/` exists (verified). The
`samus-base` image is referenced by every workcell Dockerfile as
`ARG BASE_IMAGE=us-west1-docker.pkg.dev/${GCP_PROJECT}/samus/samus-base:latest`
— so the base image MUST be pushed there first (§4.1). No new repo needed.

### 2.2 Neo4j AuraDB — [OPERATOR DECISION D-3]

The GCP Samus runs on its **own dedicated Neo4j AuraDB instance** — managed,
Bolt-compatible (explicitly NOT Amazon Neptune, whose openCypher dialect and
HTTP/WS protocol are not Bolt-compatible and would require a driver rewrite).

This is a **fresh, single AuraDB instance** with its own graph. It is **not**
the operator's local Neo4j Desktop DBMS and **not** the ecosystem Hivemind —
the GCP Samus is standalone (see the Revision note and §1's standalone
boundary). There is **no migration of the ecosystem's existing multi-database
graph**: the GCP Samus starts from an empty or minimally seeded graph.

Provisioning detail is in **§5**. It is listed here because the AuraDB
connection URI + credentials are a prerequisite secret for the `samus-memory`
service and must exist before §4 deploys begin.

### 2.3 Persistent state to replace the ephemeral filesystem

Three distinct on-disk concerns live under `/opt/samus/data` today. They have
different durability and concurrency needs, so they get different stores.

| On-disk artifact today | What it is | Recommended store | Rationale |
|---|---|---|---|
| `finance/stripe_events.jsonl` | Stripe webhook **idempotency** log — every event id seen, append-only, read on each webhook to reject replays. | **Firestore (Native mode)** collection `stripe_events`, doc id = Stripe `event.id`. | Needs *strongly-consistent, cross-instance, conditional-create* writes. Firestore's `create()` fails if the doc exists → atomic dedup. JSONL on a per-instance tmpfs gives neither cross-instance visibility nor atomicity. |
| `finance/upsell_queue.jsonl` | Append-only **state machine** for the 3-touch upsell nurture sequence; folded-log read pattern. | **Firestore** collection `upsell_queue` — one doc per transition row (doc id = `event_id`), queried by `(customer_id, source_offer_code, touch_num)`. | Same cross-instance requirement; the existing fold-the-log read maps cleanly to a Firestore query + client-side latest-wins. |
| `*_audit.jsonl` ledgers (crm, intake, feedback, voice, …) + `SAMUS_ARTIFACT_ROOT` generated files (seo reports, proposals) | Tamper-evident audit chains + generated customer deliverables. | **GCS bucket** `gs://${GCP_PROJECT}-samus-data/` with prefix-per-workcell (`audit/crm/…`, `artifacts/seo/…`). | Audit ledgers are append-mostly and rarely read hot; artifacts are write-once blobs. GCS object-per-record (or daily-rolled object) is the cheap, durable fit. Firestore would also work but GCS is cheaper for large/seldom-read blobs. |

```bash
# Firestore — Native mode, same region as Cloud Run.
gcloud firestore databases create --location=us-west1 --type=firestore-native

# GCS artifact bucket — uniform bucket-level access, same region.
gcloud storage buckets create gs://${GCP_PROJECT}-samus-data \
  --location=us-west1 --uniform-bucket-level-access
```

> **CODE-CHANGE SURFACE — required before finance can deploy.** This is the
> single largest code change in the migration. The following files write
> JSONL via `backend.common.persistence.JsonlLedger` and must gain a
> Firestore/GCS backend:
> - `backend/finance/webhook.py` — `_event_ledger()` / `event_log_path()`
>   (idempotency). **Hard blocker:** without this, a Cloud Run finance
>   instance that scales to zero forgets every event id and will re-process
>   replayed Stripe events (double receipts, double fulfilment).
> - `backend/finance/upsell_queue.py` — `_ledger()` / `queue_path()` and the
>   `_read_all_rows()` fold.
> - `backend/common/persistence.py` — cleanest fix is to make `JsonlLedger`
>   pluggable: a `FirestoreLedger` / `GcsLedger` sibling selected by a
>   `SAMUS_LEDGER_BACKEND` env var (`jsonl` default for local, `firestore`
>   for Cloud Run). This keeps the local Compose stack unchanged.
> - `backend/common/storage.py` — `SAMUS_ARTIFACT_ROOT` resolution gains a
>   `gs://` scheme branch.
> - `backend/common/audit_ledger.py` — the canonical tamper-evident ledger,
>   if it also writes to `/opt/samus/data`.
>
> **The migration cannot complete without this code change.** It is called
> out as **work item W-1** in §9. Until W-1 lands, finance/crm/intake/voice/
> feedback on Cloud Run will lose state on every scale-to-zero.

### 2.4 Inter-service networking model

Replace Docker DNS names with Cloud Run service URLs delivered as env vars,
and authenticate calls with OIDC id-tokens.

- Each `*_URL` env var (`FINANCE_URL`, `GATEWAY_URL`, `MEMORY_URL`, …) is set
  to the callee's `https://<svc>-<hash>-uw.a.run.app` URL instead of
  `http://samus-<svc>:8080`. The URL is stable for the life of the service;
  capture it from `gcloud run services describe <svc> --format='value(status.url)'`
  immediately after first deploy and feed it into dependents (§4 ordering
  exists precisely so callees deploy before callers).
- **HMAC stays.** The `VerifyHMACMiddleware` (`SAMUS_SHARED_HMAC_KEY`) is the
  application-layer trust boundary and is unchanged — it already does not
  depend on the network being private.
- **Add Cloud Run IAM as defence-in-depth.** Internal services deploy with
  `--no-allow-unauthenticated`. Callers must attach a Google-signed OIDC
  id-token (audience = callee URL). The `samus-runtime` SA has `run.invoker`,
  and the standard library path is `google.auth` fetching an id-token from
  the metadata server. **CODE-CHANGE SURFACE W-2:** the inter-workcell HTTP
  client (whatever issues the `*_URL` POSTs) must add an
  `Authorization: Bearer <id-token>` header when running on Cloud Run. If
  W-2 is deferred, internal services must instead deploy with
  `--allow-unauthenticated` + `--ingress internal` and rely on HMAC + the
  internal-ingress boundary alone (acceptable for a first cut; tighten later).

> **Operator decision D-4 — internal auth.** Recommend: first cut ships
> `--ingress internal --allow-unauthenticated` (HMAC-only, no W-2 needed);
> a hardening pass later adds W-2 and flips to `--no-allow-unauthenticated`.

### 2.5 SQS worker model — [OPERATOR DECISION D-1, see §1.2]

Recommended: each of the 9 workers is its own Cloud Run service deployed with:
```
--min-instances=1 --max-instances=1 --no-cpu-throttling --ingress internal --no-allow-unauthenticated
--port=8080            # the worker has no HTTP server, but Cloud Run requires
                       # a listening port within the startup window — see note
```
- `--min-instances=1` keeps one instance warm so `poll_loop()` runs continuously.
- `--no-cpu-throttling` (a.k.a. "CPU always allocated" / `--cpu-throttling`
  off) is **mandatory** — without it Cloud Run throttles CPU to near-zero
  between requests and the poll loop stalls.
- `--max-instances=1` — competing consumers on one SQS queue are fine, but a
  single warm instance is cheapest and the queues are low-volume.

> **Cloud Run startup-probe caveat for workers.** A Cloud Run service must
> open a TCP listener on `$PORT` within the startup window or the revision is
> marked unhealthy. The worker entrypoints (`python -m backend.<svc>.worker`)
> run `poll_loop()` and never bind a port. **CODE-CHANGE SURFACE W-3:** the
> worker module must start a trivial HTTP health server (e.g. a thread serving
> `GET /health -> 200` on `$PORT`) alongside the poll loop. This is a ~15-line
> addition to `backend/common/worker_base.py::run_forever`. Alternatively, use
> Cloud Run **jobs** for workers (jobs do not require a port) — but jobs lose
> always-on long-polling (decision D-1). W-3 is the recommended path.

### 2.6 Secrets — Secret Manager

`Bootstrap-CloudSecrets.ps1` mirrors secrets from DPAPI; the live
project (`gcloud secrets list`) shows **17 secrets already present**:
`anthropic-api-key` (deprecated — replaced by `openai-api-key`),
`aws-access-key-id`, `aws-secret-access-key`,
`gmail-inbox-email`, `gmail-oauth-client-id`, `gmail-oauth-client-secret`,
`gmail-oauth-token`, `hivemind-password`, `openai-api-key`, `places-api-key`,
`sendgrid-api-key`, `sendgrid-from-email`, `shared-hmac-key`, `stripe-api-key`,
`stripe-webhook-secret`, `vapi-api-key`, `vapi-webhook-secret`.

**Still MISSING for a Cloud Run deploy (must be created):**

| Secret | Source | Used by |
|---|---|---|
| `neo4j-uri` | the **dedicated standalone** AuraDB console (the `neo4j+s://<dbid>.databases.neo4j.io` URI) | memory |
| `neo4j-user` | AuraDB user, e.g. `neo4j_samus` (or AuraDB default `neo4j`) | memory |
| `samus-ledger-secret-key` | new dedicated audit-signing key (currently empty → falls back to HMAC key) | all (audit ledger) |
| `vapi-assistant-id`, `vapi-phone-number-id` | Vapi dashboard UUIDs (account-binding) | voice |
| `google-pagespeed-api-key` | GCP API key for PSI | seo, fulfillment |
| `gateway-bearer-token` | operator-console bearer (if `bearer_required=true`) | gateway |
| `experience-export-bucket` | name of the GCS export bucket for the experience feed (§7); a config value, not a credential — may instead be a plain env var | experience-export job |

> Note `hivemind-password` already exists. **It is reused as the password
> secret for the new standalone AuraDB instance** — bind it to the
> `samus-memory` service's `NEO4J_PASSWORD` and *update it with a new version*
> holding the standalone AuraDB instance's generated password. The secret name
> is historical; on this standalone deployment it is simply "the AuraDB
> password," nothing to do with the ecosystem Hivemind. (A cleaner name —
> `samus-neo4j-password` — is optional; if renamed, update the
> `Bootstrap-CloudSecrets.ps1` mapping and §4.3.) `aws-access-key-id` /
> `aws-secret-access-key` exist and are reused as-is; see §8 risk on key
> rotation.

Secrets are bound to a Cloud Run service with `--set-secrets`, e.g.
`--set-secrets=STRIPE_API_KEY=stripe-api-key:latest`. Extend
`Bootstrap-CloudSecrets.ps1`'s `$Mappings` array with the missing entries and
re-run; it is idempotent.

### 2.7 Custom domain (optional, do after first deploy)

`samus-intake` and `samus-gateway` are currently expected at
`api.hustleforge.tech`. Map a domain with
`gcloud run domain-mappings create --service samus-gateway --domain api.hustleforge.tech`
once DNS is controllable. Not on the critical path for first deploy — the
`*.run.app` URLs work immediately.

### 2.8 Experience-feed export bucket (NEW — §7)

The experience feed needs a GCS bucket the ecosystem-side puller can read.
Provision it now so §7's job can be wired during the deploy:

```bash
# Export bucket for the one-way experience feed. Same region; uniform access.
gcloud storage buckets create gs://${GCP_PROJECT}-samus-experience \
  --location=us-west1 --uniform-bucket-level-access
```

This bucket is intentionally **separate** from `gs://${GCP_PROJECT}-samus-data`
(§2.3): the data bucket holds operational artifacts the GCP Samus owns; the
experience bucket holds only the curated export deltas and has a different
read-access posture (the ecosystem host's puller identity reads it — §7.4).

---

## 3. Per-service Cloud Run configuration

All services share this baseline (matches the Compose `x-samus-common` posture):

```
--region=us-west1
--platform=managed
--service-account=${SAMUS_RUNTIME_SA}@${GCP_PROJECT}.iam.gserviceaccount.com
--port=8080
--execution-environment=gen2
--set-env-vars=PYTHONUNBUFFERED=1,SAMUS_PORT=8080,SAMUS_ENV=production,AWS_REGION=us-west-1,AWS_DEFAULT_REGION=us-west-1,SAMUS_ARTIFACT_ROOT=gs://${GCP_PROJECT}-samus-data/artifacts,SAMUS_LEDGER_BACKEND=firestore
--set-secrets=SAMUS_SHARED_HMAC_KEY=shared-hmac-key:latest,AWS_ACCESS_KEY_ID=aws-access-key-id:latest,AWS_SECRET_ACCESS_KEY=aws-secret-access-key:latest,SAMUS_LEDGER_SECRET_KEY=samus-ledger-secret-key:latest
```

Per-service overrides below. Memory/CPU mirror the Compose `deploy.resources`
limits (Cloud Run minimum CPU is 1; 0.5-CPU Compose services round up to
`--cpu=1`). Concurrency: FastAPI workcells are async + mostly I/O-bound, so
the Cloud Run default `--concurrency=80` is fine for HTTP services; workers
get `--concurrency=1` since they do not serve real traffic.

| Service | Ingress | unauth? | min | max | CPU | Mem | Extra env / secrets |
|---|---|---|---|---|---|---|---|
| `samus-gateway` | all | yes¹ | 1 | 4 | 1 | 512Mi | every `<SVC>_URL` (17 of them), `SQS_*_QUEUE_URL`, `gateway-bearer-token` |
| `samus-intake` | all | yes | 0 | 4 | 1 | 256Mi | `DDB_ONBOARDING_LEADS_TABLE`, `SAMUS_INTAKE_ALLOWED_ORIGINS`, `SAMUS_INTAKE_RATE_LIMIT_ENABLED=1`, optional `SAMUS_INTAKE_CAPTCHA_SECRET` |
| `samus-finance` | all | yes | 1² | 2 | 1 | 512Mi | `STRIPE_API_KEY`, `STRIPE_WEBHOOK_SECRET`, `SENDGRID_*`, `GATEWAY_URL`, `SAMUS_AUTO_FULFILL_OFFERS`, `SAMUS_LEDGER_BACKEND=firestore` |
| `samus-voice` | all | yes | 1² | 2 | 1 | 512Mi | `VAPI_*` secrets, `MEMORY_URL`; **drop** `NGROK_AUTHTOKEN`/`NGROK_RESERVED_DOMAIN` (Cloud Run URL replaces the tunnel) |
| `samus-feedback` | all | yes | 0 | 2 | 1 | 256Mi | `SAMUS_FEEDBACK_AUDIT_PATH` → GCS prefix |
| `samus-memory` | internal | no³ | 1⁴ | 2 | 1 | 512Mi | `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`(=hivemind-password), `NEO4J_DATABASE=neo4j`⁵, `SAMUS_KG_TIER_MODE=label`⁶, `NEO4J_REQUIRED=true`, `OPENAI_API_KEY` |
| `samus-crm` | internal | no³ | 0 | 4 | 1 | 384Mi | all 8 `DDB_*` table names, `SAMUS_CRM_AUDIT_PATH`→GCS |
| `samus-prospecting` | internal | no³ | 0 | 4 | 1 | 512Mi | `GOOGLE_PLACES_API_KEY`, `OPENAI_API_KEY` |
| `samus-seo` | internal | no³ | 0 | 4 | 1 | 512Mi | `OPENAI_API_KEY`, `GOOGLE_PAGESPEED_API_KEY`, `GATEWAY_URL` |
| `samus-fulfillment` | internal | no³ | 0 | 4 | 1 | 512Mi | `GOOGLE_PAGESPEED_API_KEY` |
| `samus-proposal` | internal | no³ | 0 | 2 | 1 | 256Mi | `GATEWAY_URL` |
| `samus-strategy` | internal | no³ | 0 | 2 | 1 | 384Mi | `SAMUS_CRM_URL`, `SAMUS_GATEWAY_URL` |
| `samus-leadgen`, `samus-scaffold`, `samus-outreach`, `samus-optimizer` | internal | no³ | 0 | 4 | 1 | 256–512Mi | — |
| `samus-signal_filter`, `samus-path_optimizer`, `samus-template_recovery`, `samus-portfolio_controller`, `samus-entropy` | internal | no³ | 0 | 2 | 1 | 384Mi | deterministic, zero-LLM — minimal config |
| **9× `samus-<svc>-worker`** | internal | no³ | **1** | **1** | 1 | 384Mi | `--no-cpu-throttling`; `SQS_<SVC>_QUEUE_URL`; same per-workcell secrets as the app sibling |

¹ Webhook/edge services keep `--allow-unauthenticated` because Stripe/Vapi/
  browsers cannot present a Google id-token; app-layer signature/HMAC/CORS is
  the real gate.
² `min-instances=1` for finance and voice so a Stripe/Vapi webhook never hits
  a cold start (webhook providers have short timeouts and limited retries).
³ Internal services: `--no-allow-unauthenticated` is the *target*; first cut
  may ship `--allow-unauthenticated --ingress internal` until W-2 lands (D-4).
⁴ `samus-memory` `min=1` to keep the AuraDB Bolt connection pool warm.
⁵ See §5.2 — the standalone AuraDB instance is single-database, so
  `NEO4J_DATABASE` must be set to `neo4j`, NOT `samus`. The code default is
  `samus` (`backend/common/settings.py` line 140) and MUST be overridden.
⁶ See §5.2 — `SAMUS_KG_TIER_MODE` selects how the internal private/`hivemind`
  two-tier split is represented on the single AuraDB database (W-4). Default
  recommendation `label` (label-namespacing within `neo4j`).

> **Concurrency caveat for finance.** The Stripe webhook idempotency check is
> now Firestore-conditional-create (atomic), so `--concurrency=80` is safe. If
> W-1 is *not* done and finance is still on JSONL, finance MUST run
> `--concurrency=1 --max-instances=1` as a stopgap to serialise writes — and
> even then a scale-to-zero loses the log. W-1 is non-negotiable for finance.

---

## 4. Ordered deploy sequence

> **Pre-flight:** §2 fully done; **W-3 (worker health port) and W-4 (KG tier
> mapping) merged 2026-05-21**; W-1 (Firestore ledger) still **outstanding** —
> must merge and test locally before finance/crm/intake/voice/feedback deploy;
> W-2 optional per D-4; W-6 (experience feed, §7) can land *after* the core
> stack is live — it is not a deploy gate.

### 4.1 Build and push images

The workcell Dockerfiles `FROM` `samus-base`. The base must be pushed first.
Build context is the **Hustleforge repo root** (parent of `Samus/`).

```bash
# From the repo root (D:\Hustleforge or the samus worktree root).
# 1. Base image — every workcell depends on it.
docker build -f Samus/docker/base/Dockerfile \
  -t us-west1-docker.pkg.dev/${GCP_PROJECT}/samus/samus-base:latest .
docker push us-west1-docker.pkg.dev/${GCP_PROJECT}/samus/samus-base:latest

# 2. Each workcell. Loop over the 21 names. Tag with a git short-SHA so
#    rollback (§6) can pin an exact revision.
TAG=$(git rev-parse --short HEAD)
for svc in crm entropy feedback finance fulfillment gateway intake leadgen \
           memory optimizer outreach path_optimizer portfolio_controller \
           proposal prospecting scaffold seo signal_filter strategy \
           template_recovery voice ; do
  # NOTE: feedback's Dockerfile lives under docker/workcells/ses/ (the
  # workcell module is `feedback` but the image dir is `ses` — see compose).
  dir=$svc ; [ "$svc" = "feedback" ] && dir=ses
  docker build -f Samus/docker/workcells/$dir/Dockerfile \
    --build-arg BASE_IMAGE=us-west1-docker.pkg.dev/${GCP_PROJECT}/samus/samus-base:latest \
    -t us-west1-docker.pkg.dev/${GCP_PROJECT}/samus/samus-$svc:$TAG .
  docker push us-west1-docker.pkg.dev/${GCP_PROJECT}/samus/samus-$svc:$TAG
done
```

> **Known GCP gotcha (memory: `GCP Cloud Build AR scope block`).** Do NOT use
> Cloud Build to push to Artifact Registry for this project — post-April-2024
> projects hit a Cloud Build runtime-VM scope block on AR pushes even with IAM
> correct. Build locally with `docker build` + `docker push` (as above) — this
> is the same pivot already baked into `Deploy-SamusLocal.ps1`. The local
> Docker daemon authenticates to AR via `gcloud auth configure-docker us-west1-docker.pkg.dev`.

### 4.2 Deploy order

Deploy **callees before callers** so each caller's `*_URL` env var can be
filled with a real URL. Dependency tiers:

```
Tier 0  (no inbound deps):  memory, crm, signal_filter, path_optimizer,
                            template_recovery, portfolio_controller, entropy,
                            leadgen, outreach, optimizer, finance
Tier 1  (call Tier 0):      prospecting, scaffold, fulfillment, seo, proposal,
                            strategy, voice
Tier 2  (calls everything): gateway
Tier 3  (calls gateway):    intake
Workers (any time after their app sibling — they only need SQS, not URLs)
Experience feed (§7):       any time after samus-memory + samus-crm are live
```

> Bootstrap note: `gateway` references 17 `*_URL`s and several Tier-1
> services reference `GATEWAY_URL`/`SAMUS_GATEWAY_URL` — a genuine cycle. Break
> it by deploying `gateway` in Tier 2 with placeholder/own URLs, capturing its
> URL, then `gcloud run services update` the Tier-1 services that need
> `GATEWAY_URL`. One re-update pass; not a redeploy.

### 4.3 Draft `gcloud run deploy` commands

**Common shell setup:**
```bash
PROJECT=${GCP_PROJECT} ; REGION=us-west1
REPO=us-west1-docker.pkg.dev/$PROJECT/samus
SA=samus-runtime@$PROJECT.iam.gserviceaccount.com
TAG=$(git rev-parse --short HEAD)
COMMON="--region=$REGION --service-account=$SA --port=8080 \
  --execution-environment=gen2 \
  --set-env-vars=PYTHONUNBUFFERED=1,SAMUS_ENV=production,AWS_REGION=us-west-1,AWS_DEFAULT_REGION=us-west-1,SAMUS_ARTIFACT_ROOT=gs://${GCP_PROJECT}-samus-data/artifacts,SAMUS_LEDGER_BACKEND=firestore \
  --set-secrets=SAMUS_SHARED_HMAC_KEY=shared-hmac-key:latest,AWS_ACCESS_KEY_ID=aws-access-key-id:latest,AWS_SECRET_ACCESS_KEY=aws-secret-access-key:latest,SAMUS_LEDGER_SECRET_KEY=samus-ledger-secret-key:latest"
```

**Tier 0 — memory (the AuraDB consumer):**
```bash
gcloud run deploy samus-memory --image=$REPO/samus-memory:$TAG $COMMON \
  --ingress=internal --no-allow-unauthenticated \
  --min-instances=1 --max-instances=2 --cpu=1 --memory=512Mi \
  --set-env-vars=NEO4J_DATABASE=neo4j,NEO4J_REQUIRED=true,SAMUS_KG_TIER_MODE=label \
  --update-secrets=NEO4J_URI=neo4j-uri:latest,NEO4J_USER=neo4j-user:latest,NEO4J_PASSWORD=hivemind-password:latest,OPENAI_API_KEY=openai-api-key:latest
MEMORY_URL=$(gcloud run services describe samus-memory --region=$REGION --format='value(status.url)')
```
> `NEO4J_DATABASE=neo4j` is mandatory — the standalone AuraDB instance is
> single-database; the code default of `samus` would fail. `SAMUS_KG_TIER_MODE`
> picks the internal tier representation (§5.2 / W-4).

**Tier 0 — crm:**
```bash
gcloud run deploy samus-crm --image=$REPO/samus-crm:$TAG $COMMON \
  --ingress=internal --no-allow-unauthenticated \
  --min-instances=0 --max-instances=4 --cpu=1 --memory=384Mi \
  --set-env-vars=DDB_PROSPECTS_TABLE=samus_prospects,DDB_CONTACTS_TABLE=samus_contacts,DDB_CONVERSATIONS_TABLE=samus_conversations,DDB_CALL_STATE_TABLE=samus_call-State,DDB_OPPORTUNITIES_TABLE=samus_opportunities,DDB_OPERATOR_TASKS_TABLE=samus_operator_tasks,DDB_ARTIFACTS_TABLE=samus_artifacts,DDB_ONBOARDING_LEADS_TABLE=samus_onboarding_leads
CRM_URL=$(gcloud run services describe samus-crm --region=$REGION --format='value(status.url)')
```

**Tier 0 — finance (webhook receiver):**
```bash
gcloud run deploy samus-finance --image=$REPO/samus-finance:$TAG $COMMON \
  --ingress=all --allow-unauthenticated \
  --min-instances=1 --max-instances=2 --cpu=1 --memory=512Mi \
  --set-env-vars=EMAIL_BACKEND=sendgrid \
  --update-secrets=STRIPE_API_KEY=stripe-api-key:latest,STRIPE_WEBHOOK_SECRET=stripe-webhook-secret:latest,SENDGRID_API_KEY=sendgrid-api-key:latest,SENDGRID_FROM_EMAIL=sendgrid-from-email:latest
FINANCE_URL=$(gcloud run services describe samus-finance --region=$REGION --format='value(status.url)')
# GATEWAY_URL is filled in the Tier-2 re-update pass.
```

**Tier 0 — the simple internal workcells** (leadgen, outreach, optimizer,
signal_filter, path_optimizer, template_recovery, portfolio_controller,
entropy) — all share one shape:
```bash
for svc in leadgen outreach optimizer signal_filter path_optimizer \
           template_recovery portfolio_controller entropy ; do
  gcloud run deploy samus-$svc --image=$REPO/samus-$svc:$TAG $COMMON \
    --ingress=internal --no-allow-unauthenticated \
    --min-instances=0 --max-instances=2 --cpu=1 --memory=384Mi
done
# Capture URLs:
for svc in leadgen outreach optimizer signal_filter path_optimizer \
           template_recovery portfolio_controller entropy ; do
  printf '%s_URL=%s\n' "$(echo $svc | tr a-z A-Z)" \
    "$(gcloud run services describe samus-$svc --region=$REGION --format='value(status.url)')"
done
```

**Tier 1 — prospecting (example; scaffold/fulfillment/seo/proposal/strategy/
voice follow the same pattern with their own secrets from §3):**
```bash
gcloud run deploy samus-prospecting --image=$REPO/samus-prospecting:$TAG $COMMON \
  --ingress=internal --no-allow-unauthenticated \
  --min-instances=0 --max-instances=4 --cpu=1 --memory=512Mi \
  --update-secrets=GOOGLE_PLACES_API_KEY=places-api-key:latest,OPENAI_API_KEY=openai-api-key:latest
```

**Tier 1 — voice (note: NO ngrok env vars):**
```bash
gcloud run deploy samus-voice --image=$REPO/samus-voice:$TAG $COMMON \
  --ingress=all --allow-unauthenticated \
  --min-instances=1 --max-instances=2 --cpu=1 --memory=512Mi \
  --set-env-vars=MEMORY_URL=$MEMORY_URL \
  --update-secrets=VAPI_API_KEY=vapi-api-key:latest,VAPI_WEBHOOK_SECRET=vapi-webhook-secret:latest,VAPI_ASSISTANT_ID=vapi-assistant-id:latest,VAPI_PHONE_NUMBER_ID=vapi-phone-number-id:latest
```

**Tier 2 — gateway (consumes every Tier-0/1 URL):**
```bash
gcloud run deploy samus-gateway --image=$REPO/samus-gateway:$TAG $COMMON \
  --ingress=all --allow-unauthenticated \
  --min-instances=1 --max-instances=4 --cpu=1 --memory=512Mi \
  --set-env-vars=LEADGEN_URL=$LEADGEN_URL,PROSPECTING_URL=$PROSPECTING_URL,SCAFFOLD_URL=$SCAFFOLD_URL,FULFILLMENT_URL=$FULFILLMENT_URL,MEMORY_URL=$MEMORY_URL,CRM_URL=$CRM_URL,FINANCE_URL=$FINANCE_URL,SIGNAL_FILTER_URL=$SIGNAL_FILTER_URL,PATH_OPTIMIZER_URL=$PATH_OPTIMIZER_URL,TEMPLATE_RECOVERY_URL=$TEMPLATE_RECOVERY_URL,PORTFOLIO_CONTROLLER_URL=$PORTFOLIO_CONTROLLER_URL,ENTROPY_URL=$ENTROPY_URL \
  --set-secrets=...,GATEWAY_BEARER_TOKEN=gateway-bearer-token:latest \
  --update-env-vars=SQS_LEADGEN_QUEUE_URL=...,SQS_PROSPECTING_QUEUE_URL=...   # the live SQS URLs
GATEWAY_URL=$(gcloud run services describe samus-gateway --region=$REGION --format='value(status.url)')
```

**Tier 2 fix-up — re-update services that needed `GATEWAY_URL`:**
```bash
for svc in finance seo proposal strategy ; do
  gcloud run services update samus-$svc --region=$REGION \
    --update-env-vars=GATEWAY_URL=$GATEWAY_URL
done
gcloud run services update samus-strategy --region=$REGION \
  --update-env-vars=SAMUS_GATEWAY_URL=$GATEWAY_URL,SAMUS_CRM_URL=$CRM_URL
```

**Tier 3 — intake:**
```bash
gcloud run deploy samus-intake --image=$REPO/samus-intake:$TAG $COMMON \
  --ingress=all --allow-unauthenticated \
  --min-instances=0 --max-instances=4 --cpu=1 --memory=256Mi \
  --set-env-vars=DDB_ONBOARDING_LEADS_TABLE=samus_onboarding_leads,SAMUS_INTAKE_ALLOWED_ORIGINS=https://hustleforge.tech,SAMUS_INTAKE_RATE_LIMIT_ENABLED=1
```

**Workers — one per SQS-bearing workcell** (`--command` overrides the image CMD):
```bash
for svc in leadgen prospecting scaffold fulfillment feedback outreach \
           optimizer proposal seo ; do
  gcloud run deploy samus-$svc-worker --image=$REPO/samus-$svc:$TAG $COMMON \
    --ingress=internal --no-allow-unauthenticated \
    --min-instances=1 --max-instances=1 --no-cpu-throttling \
    --cpu=1 --memory=384Mi --concurrency=1 \
    --command=python --args=-m,backend.$svc.worker \
    --update-env-vars=SQS_$(echo $svc|tr a-z A-Z)_QUEUE_URL=...   # live SQS URL
done
```
> Worker services require W-3 (a health port) — see §2.5. Without W-3 the
> revision will fail its startup probe.

**Experience feed (§7) — deploy after `samus-memory` + `samus-crm` are live:**
the draft `gcloud run jobs create` command and Cloud Scheduler binding are in
**§7.4**. It is not part of the tiered core deploy and is not a deploy gate.

### 4.4 Post-deploy verification

```bash
# Every service reports Ready.
gcloud run services list --region=us-west1 \
  --format='table(metadata.name,status.conditions[0].status,status.url)'

# Gateway health.
curl -fsS "$GATEWAY_URL/health"

# Memory ↔ AuraDB round-trip (internal service → needs an id-token):
TOKEN=$(gcloud auth print-identity-token)
curl -fsS -H "Authorization: Bearer $TOKEN" "$MEMORY_URL/health"

# AWS auth from inside Cloud Run — tail logs after a deploy and confirm no
# QueueNotConfigured / NoCredentialsError:
gcloud run services logs read samus-prospecting-worker --region=us-west1 --limit=50
```

---

## 5. Neo4j — dedicated standalone AuraDB instance

> **What changed (revision note):** the GCP Samus is standalone. §5 no longer
> describes a cross-ecosystem migration. There is no `neo4j-admin database
> dump` of the operator's local five-database DBMS, no "import samus.dump then
> replay hivemind.cypher into a shared instance," and no shared-AuraDB
> requirement. The GCP Samus gets ONE fresh AuraDB instance with its own
> graph. The old §5.4 (migrating existing graph data) and §5.5 (dual-connect
> the local stack to AuraDB) are **removed** — they only existed to serve the
> shared-instance design. What remains is provisioning a clean instance (§5.1),
> resolving the *internal* two-tier private/`hivemind` pattern on one
> single-database instance (§5.2), RBAC users (§5.3), and an optional minimal
> seed (§5.4).

### 5.1 AuraDB tier and sizing — [OPERATOR DECISION D-5]

The GCP Samus memory graph is small — it starts empty and accumulates this one
deployment's operational knowledge. The working set is well within **AuraDB
Free** (1 instance, 200k nodes / 400k relationships cap, auto-pauses after 3
days idle) or **AuraDB Professional** (no node cap, no auto-pause, configurable
RAM from ~1GB up, ~$65/mo entry).

> **Recommendation (D-5, unchanged):** AuraDB Free is tempting for cost but
> its **3-day idle auto-pause** is hostile to a production service — a paused
> instance returns connection errors and `samus-memory` would fail
> `NEO4J_REQUIRED=true` health. Use **AuraDB Professional** (smallest RAM
> tier) for the standalone production instance. Free tier is acceptable only
> for a throwaway test.

AuraDB instances are created from the Neo4j Aura console (console.neo4j.io) —
there is no `gcloud`-native provisioning. Create **one new instance** for the
GCP Samus. The console emits a connection URI of the form
`neo4j+s://<dbid>.databases.neo4j.io` and a generated password shown exactly
once → store it as a new version of the `hivemind-password` secret (§2.6) and
the URI as `neo4j-uri`.

### 5.2 The two-tier private/`hivemind` pattern on one instance — [OPERATOR DECISION D-6]

**What the code actually does (verified in `backend/`).** Samus's memory layer
talks to Neo4j through `backend/common/graph_client.py`. That client targets a
single database chosen by `NEO4J_DATABASE` (default `samus`,
`backend/common/settings.py` line 140). The client's `session()` method does
accept a per-call `database=` override and its docstring names `samus` and
`hivemind` as the two example databases — so the *idea* of a private tier plus
a shared `hivemind` tier exists structurally in the code. But:

- There is **no in-code "promote knowledge with confidence ≥ 0.8 to a shared
  hivemind database" routine** inside the Samus codebase today. The "promote
  high-confidence knowledge to the shared Hivemind" mechanism is an
  *ecosystem-wide* design (it lives in the ecosystem's KG-injection branches,
  not in this `samus` worktree).
- Within Samus, the closest live behaviour is **operator-gated promotion**:
  `backend/intake/youtube_ingest.py` writes `:YouTubeInsight` nodes with
  `status='draft'` for an operator to later promote, and `backend/crm/__init__`
  lists "Hivemind graph projection (mirror Prospect→Contact→Conversation edges
  to Neo4j)" as **Phase-2 deferred** work — not implemented.

**Consequence for the standalone deploy.** Because the GCP Samus is standalone
*and* the multi-database promotion is not actually wired in code, the old
"single AuraDB instance is single-database → `USE samus` fails" problem
collapses to a small, purely-internal decision: how to represent a *private*
tier and a *promotable/`hivemind`-tier* on the one AuraDB `neo4j` database.

The single hard fact stays: **an AuraDB instance is single-database** — every
AuraDB instance exposes exactly one user database, always named `neo4j`;
`CREATE DATABASE` / `USE samus` are Enterprise-server / self-managed features
not available on Aura. So `NEO4J_DATABASE` MUST be `neo4j` regardless.

Three resolution options for the internal two-tier representation:

| Option | What it means | Verdict |
|---|---|---|
| **6a. Two AuraDB instances** | One AuraDB instance for the private tier, a second for the `hivemind` tier. Two URIs, two secrets. | True isolation, but ~2× cost (~$130/mo on Pro) for a split that is internal bookkeeping only. Over-engineered for a standalone deployment whose `hivemind` tier is *not even consumed by anyone else* — the ecosystem ingests via the §7 feed, not by connecting to this instance. **Rejected.** |
| **6b. One instance, two label namespaces** | One AuraDB instance, the `neo4j` database. Private-tier nodes carry a `:Private` (or `tier:'private'` property); promotable nodes carry `:Hivemind` (or `tier:'hivemind'`). A node is "promoted" within the standalone instance by re-labelling / setting the tier property. `backend/memory/` Cypher gains a tier filter. | Cheapest; one instance; the private/promoted split is a property, not a DBMS feature. Matches the "promotion is a state change, not a cross-DB copy" reality. **Recommended.** Requires **W-4** (now small — see below). |
| **6c. One instance, single tier (no split yet)** | Treat the whole `neo4j` database as one tier for the first cut. Promotion is deferred until §7's experience feed actually needs a "what is promotable" flag. | Smallest possible scope — but §7's feed *does* need to know which nodes are high-confidence/promotable, so the tier flag is needed anyway. 6c just defers 6b by one step. |

> **Recommendation D-6 (REWRITTEN — standalone): option 6b.** Provision ONE
> AuraDB Professional instance for the GCP Samus. Represent the
> private/`hivemind` two-tier split with a node **label or `tier` property**
> on the single `neo4j` database — `tier:'private'` (default for everything
> ingested) and `tier:'hivemind'` (set when a node is promoted, e.g. an
> operator promotes a `:YouTubeInsight` from `status='draft'`, or a
> confidence-threshold rule promotes a knowledge node). Set
> `NEO4J_DATABASE=neo4j` everywhere and `SAMUS_KG_TIER_MODE=label`. **W-4 is
> now a much smaller change than the old cross-ecosystem version:** it adds a
> `tier` property convention to `backend/memory/` writes/queries and a
> `promote_node(node_id)` helper that flips `tier` to `hivemind`. It is
> entirely internal to the standalone instance — no cross-agent graph, no
> cross-ecosystem coordination. The `tier:'hivemind'` set is exactly the set
> §7's experience feed exports to the ecosystem.

### 5.3 AuraDB users — [OPERATOR DECISION D-7]

The local Samus historically authenticates as `neo4j_samus` (not admin
`neo4j`). AuraDB Professional supports custom users and role-based grants
(`CREATE USER`, `GRANT`). After the standalone instance is up:

```cypher
// Run once in the AuraDB console / cypher-shell as the admin user.
CREATE USER neo4j_samus SET PASSWORD '<from Secret Manager>' CHANGE NOT REQUIRED;
GRANT ROLE editor TO neo4j_samus;       // read+write, no schema/admin
```

Store the `neo4j_samus` password as the `hivemind-password` secret version
bound to `samus-memory` (§2.6), or a dedicated `samus-neo4j-password` secret.

> Because the instance is standalone and single-tenant (only the GCP Samus
> connects to it), elaborate per-tier RBAC is unnecessary — the `tier`
> split (§5.2) is application-level, not a security boundary. If the operator
> prefers minimal RBAC for the first cut, the AuraDB default `neo4j` admin
> user works; record it as a known gap to close. Label/property-scoped GRANTs
> are available on Pro if finer control is later wanted.

### 5.4 Optional minimal seed

The standalone instance starts **empty**. That is the intended state — the GCP
Samus accumulates its own operational graph from live traffic. Two optional
seed paths, operator's choice:

- **No seed (recommended).** Let `samus-memory` create its own indexes on
  first start (`GraphClient.init_schema()` issues the `CREATE INDEX IF NOT
  EXISTS` statements from `backend/common/graph_schema.py`). The graph fills
  organically.
- **Minimal seed.** If the operator wants the GCP Samus to start with a small
  base of curated knowledge (e.g. product/playbook reference nodes), run the
  `knowledge_ingest` pod once against the new instance with a small curated
  document set (`POST /api/knowledge/ingest`, `trust_level=internal`). This is
  *seeding from a document set*, not *migrating the ecosystem graph* — there is
  no `.dump` import.

**Schema/connectivity verification (after first `samus-memory` start):**
```cypher
// On the standalone AuraDB instance:
SHOW INDEXES;                              // confirm init_schema() ran
MATCH (n) RETURN labels(n) AS label, count(*) ORDER BY label;  // expect empty/seed-only
```

---

## 6. Cutover and rollback

### 6.1 Cutover sequence (zero-payment-loss for the Stripe webhook)

The risky cutover is **G** — the Stripe webhook currently flows
`Stripe → ngrok (millard-unruffable-reginia.ngrok-free.dev) → local finance`.

1. **Deploy `samus-finance` to Cloud Run** (§4.3) and confirm
   `curl -fsS "$FINANCE_URL/health"` is green. Do NOT change Stripe yet.
2. **Verify the Cloud Run finance webhook with a Stripe test event.** In the
   Stripe dashboard add a *second* webhook endpoint (do not edit the live one)
   pointing at `$FINANCE_URL/stripe_webhook`, send a test
   `checkout.session.completed`, and confirm the Cloud Run logs show a
   verified signature and a Firestore idempotency write.
3. **Confirm idempotency store is shared.** Because both the local and Cloud
   Run finance now write to the *same* Firestore `stripe_events` collection
   (W-1), an event processed by one is rejected as a duplicate by the other —
   so a brief window where both endpoints are live cannot double-fulfil.
4. **Repoint the live webhook.** Edit the live Stripe webhook endpoint URL
   from the ngrok URL to `$FINANCE_URL/stripe_webhook`. Stripe sends new
   events to the new URL immediately.
5. **Drain.** Leave the local finance container running for ~24h so any
   in-flight ngrok-delivered event and any Stripe retry of a pre-cutover
   event still lands somewhere. The shared Firestore log makes this safe.
6. **Remove the temporary test endpoint** from Stripe; **stop the ngrok
   tunnel**; optionally stop the local finance container.
7. **Voice (Vapi) webhook** — analogous: Vapi's `serverUrl` was previously
   PATCHed by the embedded ngrok tunnel. Set the Vapi assistant `serverUrl`
   (dashboard or API) to `$VOICE_URL/vapi/webhook` and confirm an inbound
   test call. The `NGROK_*` env vars are intentionally omitted from the
   Cloud Run voice service (§3) so the tunnel never starts.
8. **Intake** — repoint the marketing site's onboarding form `POST` target
   (and CORS origin) from the old `samus-intake-2026` placeholder to the new
   `samus-intake` URL (or map `api.hustleforge.tech`, §2.7).
9. **Neo4j — no cutover needed.** The GCP Samus is standalone with its OWN
   fresh AuraDB instance (§5). There is no repointing of the operator's local
   Neo4j and no decommission step: the local Neo4j Desktop DBMS belongs to the
   ecosystem and is untouched by this deployment. The standalone AuraDB
   instance is verified by §4.4's memory health round-trip; if it fails, fix
   the instance or its secret — there is no fallback DB to revert to because
   the local Neo4j was never this deployment's database.

### 6.2 Rollback

Cloud Run keeps every revision; rollback is traffic re-pointing, not redeploy.

- **Per-service rollback:**
  ```bash
  gcloud run services update-traffic samus-finance --region=us-west1 \
    --to-revisions=samus-finance-00001-abc=100
  ```
- **Whole-stack rollback to the local Compose host:** the local stack was
  never torn down during cutover (step 5 keeps it warm). To fall back:
  1. Repoint the Stripe webhook back to the ngrok URL and restart the ngrok
     tunnel + local finance.
  2. Repoint Vapi `serverUrl` back to the local tunnel.
  3. Memory: the local Compose stack still uses the local Neo4j Desktop DBMS —
     unchanged and unaffected by the GCP deploy — so a fall-back to local is
     immediate. The standalone AuraDB instance simply sits idle (or is deleted
     to stop billing) if the GCP deploy is abandoned.
  This is why §6.1 sequences webhook cutover *last among edges* — every step
  before its point-of-no-return is reversible, and the Neo4j question no
  longer has a point-of-no-return at all (the two databases are independent).
- **Build rollback:** images are tagged with git short-SHA (§4.1), so any
  prior `samus-<svc>:<sha>` can be redeployed directly.

---

## 7. Experience feedback to the ecosystem (NEW)

> **Requirement.** The GCP Samus is operationally standalone, but the operator
> wants the Hustleforge **ecosystem to gain experiences from it** — the GCP
> Samus's operational learnings must flow BACK into the ecosystem's shared
> Hivemind. This section designs that mechanism. It is a **one-directional
> learning feed**: the GCP Samus *exports* curated experiences; the ecosystem
> *ingests* them. The GCP Samus never reads ecosystem state, so the standalone
> boundary (§1) is preserved.

### 7.1 What "experiences" concretely covers — [OPERATOR DECISION D-8]

"Experiences" is deliberately scoped to *durable, generalisable learning* — not
raw operational telemetry. Recommended payload, three classes:

| Class | Concretely | Source in the GCP Samus | Why it's an "experience" |
|---|---|---|---|
| **(1) KG knowledge promotions** | Nodes that crossed into the `hivemind` tier (§5.2) — `tier:'hivemind'` nodes: promoted `:YouTubeInsight` distillations, promoted `:KnowledgeChunk` records, any node an operator or a confidence rule marked high-confidence. | The standalone AuraDB `neo4j` database, `tier:'hivemind'` set. | This is *exactly* the "promote high-confidence knowledge to shared Hivemind" pattern — the GCP Samus's promotion target becomes the ecosystem's ingest source. |
| **(2) Operational outcomes** | Per-workcell outcome rollups: SEO audit results, prospecting hit-rates, outreach reply-rates, fulfillment success/failure, optimizer bandit arm statistics. Aggregated, not per-event. | DDB `samus_task_state`, `samus_feedback_events`, optimizer bandit state; the `entropy` workcell already rolls these up. | Tells the ecosystem "this approach worked / this one failed" — generalisable strategy signal. |
| **(3) CRM / deal outcomes** | Closed-won / closed-lost opportunities, deal sizes, objection→angle pairs, conversion funnel stats. Anonymised to outcome shape — no raw customer PII. | DDB `samus_opportunities`, `samus_conversations`, the feedback engine's objection/angle log. | The highest-value experience: real money outcomes the ecosystem's other agents can learn sales/strategy from. |

> **Operator-confirm point D-8.** The recommendation is to ship **all three
> classes**, with class (3) **anonymised at the export boundary** — strip
> customer name / email / phone, keep industry, deal size band, offer code,
> objection category, outcome. The operator must confirm: (i) all three
> classes, or a subset for the first cut; (ii) the anonymisation rule for
> class (3) — what fields are PII and must be dropped before an experience
> leaves the GCP Samus. **Default if unconfirmed:** class (1) only (KG
> promotions) — it is the lowest-PII-risk class and is the literal
> "promote-to-hivemind" mechanism, so it is the safe first cut.

### 7.2 Export channel — how the GCP Samus emits experiences — [recommendation]

Two channels were considered:

- **An event stream (Pub/Sub, or the existing AWS SQS).** Real-time, but
  over-built: experiences are *aggregated learnings*, not events; a per-event
  stream would push the aggregation/curation work onto the consumer and add a
  always-on subscriber on the ecosystem side. The ecosystem also has no
  always-on listener — its ingest pattern is the 4-hourly `Pull-...` scheduled
  task (see §7.3). A stream and a poll-based consumer is an impedance
  mismatch.
- **Periodic export of curated deltas to a GCS bucket. ✅ Recommended.** A
  small Cloud Run **job** runs on a Cloud Scheduler cadence (D-9), reads the
  three experience classes (§7.1), curates + anonymises them, and writes a
  single timestamped **export object** to `gs://${GCP_PROJECT}-samus-experience/`.
  This matches how the GCP Samus already persists artifacts (GCS, §2.3) and —
  critically — matches the ecosystem's existing *pull* ingest model exactly
  (§7.3). It is cheap (a job, not an always-on service), auditable (each
  export is one immutable object), and idempotent (the consumer dedups by
  export id).

**Export object shape** — one NDJSON-or-JSON object per run, written to
`gs://${GCP_PROJECT}-samus-experience/experiences/<UTC-ISO>.json`:

```jsonc
{
  "export_id": "2026-05-20T120000Z",        // = object basename, dedup key
  "source": "gcp-samus-standalone",
  "schema_version": 1,
  "window": { "from": "...", "to": "..." }, // delta window since last export
  "kg_promotions": [                         // class (1) — tier:'hivemind' nodes
    { "node_id": "...", "labels": ["YouTubeInsight"], "props": { ... },
      "confidence": 0.86, "promoted_at": "..." }
  ],
  "operational_outcomes": [ /* class (2) rollups */ ],
  "deal_outcomes": [ /* class (3) — ANONYMISED per D-8 */ ]
}
```

The export job tracks a **high-water mark** (last export window end) in a
Firestore doc `experience_export/_state` so each run emits only the delta
since the previous run — bounded object size, no re-export of old learnings.

### 7.3 Ecosystem ingest — building on the Pull-SamusCloudState pattern

`D:\tools\hustleforge-git\Pull-SamusCloudState.ps1` +
`Register-SamusCloudPullTask.ps1` already establish the exact pattern needed:
a host-side scheduled task that, every 4 hours, pulls cloud-Samus state down
to local disk. The experience feed reuses that pattern rather than inventing a
new transport.

**Recommended: a sixth sink in `Pull-SamusCloudState.ps1` — "Sink 6: Experience
feed."** The puller already authenticates `gcloud` as the operator and walks
GCP surfaces; adding one more sink that lists + downloads new objects from
`gs://${GCP_PROJECT}-samus-experience/experiences/` is a small, idiomatic
extension. The sink:

1. Lists export objects in the bucket newer than the last-ingested marker
   (a small state file under the anchor root, e.g.
   `E:\Hustleforge\Samus\data\experience_feed\_last_ingested.txt`).
2. Downloads each new export object (`gcloud storage cp`).
3. Hands each export to the **ecosystem Hivemind ingest** — this is the one
   place the feed touches the ecosystem. Two sub-options:
   - **3a (recommended).** Write the downloaded export objects to a staging
     dir and let an ecosystem-side ingester (the existing `knowledge_ingest`
     pod pattern, or a Hivemind-injection script from the ecosystem's
     `feat/<agent>-kg-hivemind` branches) replay `kg_promotions` into the
     **ecosystem's** `hivemind` Neo4j database, and fold `operational_outcomes`
     / `deal_outcomes` into the ecosystem's KG as outcome nodes. The puller
     just stages; ingest is an ecosystem-owned step. This keeps the puller
     free of Neo4j-write logic and respects the ecosystem's own KG governance
     (confidence gating, the ≥0.8 promotion rule lives on the ecosystem side).
   - **3b.** The sink writes directly into the ecosystem `hivemind` database
     via `cypher-shell` (the puller already discovers `cypher-shell` for
     Sink 4). Simpler operationally but couples the puller to ecosystem KG
     schema — only pick this if the operator wants a single script.
4. Advances the last-ingested marker.

The export objects are **anchored on host disk** as a side effect (same
durability rationale as the rest of the puller) under
`E:\Hustleforge\Samus\data\experience_feed\<UTC-ISO>.json`, and the existing
Sink-5 retention pass trims old ones (newest always kept).

> **Why not a new scheduled task:** `Register-SamusCloudPullTask.ps1` already
> registers a 4-hourly S4U task with the right identity model (LocalMachine-
> DPAPI fallback, `gcloud` as the operator). Adding Sink 6 to the existing
> puller means **zero new task registration** — the experience feed ingests on
> the same 4-hourly cadence the operator already trusts and monitors.

### 7.4 Wiring — work-item W-6

The export side is one small Cloud Run job plus a scheduler trigger:

```bash
# Export-side runtime SA — least-privilege, write-only to the export bucket.
gcloud iam service-accounts create samus-experience-export \
  --display-name="Samus experience-feed exporter"
# Grant: storage.objectCreator on gs://${GCP_PROJECT}-samus-experience only,
#        datastore.user (read the experience classes + high-water-mark doc),
#        + read access to the DDB tables it rolls up (via the AWS key secret).

# The job runs the new exporter module from the samus image.
gcloud run jobs create samus-experience-export \
  --image=$REPO/samus-memory:$TAG \
  --region=$REGION --service-account=samus-experience-export@$PROJECT.iam.gserviceaccount.com \
  --set-env-vars=EXPERIENCE_EXPORT_BUCKET=${GCP_PROJECT}-samus-experience,SAMUS_KG_TIER_MODE=label \
  --set-secrets=NEO4J_URI=neo4j-uri:latest,NEO4J_USER=neo4j-user:latest,NEO4J_PASSWORD=hivemind-password:latest,AWS_ACCESS_KEY_ID=aws-access-key-id:latest,AWS_SECRET_ACCESS_KEY=aws-secret-access-key:latest \
  --command=python --args=-m,backend.memory.experience_export

# Cloud Scheduler trigger — D-9 sets the cadence.
gcloud scheduler jobs create http samus-experience-export-trigger \
  --schedule="0 */4 * * *" --uri="<job-run-endpoint>" --http-method=POST \
  --oauth-service-account-email=samus-experience-export@$PROJECT.iam.gserviceaccount.com
```

**W-6 — code changes (new work-item, see §9):**
- `backend/memory/experience_export.py` (NEW) — reads the three experience
  classes (§7.1: `tier:'hivemind'` KG nodes via the graph client; DDB rollups
  via the existing AWS clients), applies the D-8 anonymisation rule to class
  (3), tracks the Firestore high-water mark, writes one export object to GCS.
- `backend/memory/` — the `tier` property + `promote_node()` helper (this is
  W-4; W-6 consumes it).
- `D:\tools\hustleforge-git\Pull-SamusCloudState.ps1` — add **Sink 6**
  (experience-feed download + stage) and a `experience_feed\` subdir under the
  anchor root; extend the manifest counts. (Same file also needs the §1.4
  service-name-suffix fix if D-2 standardises names.)
- Ecosystem-side ingester (option 3a) — a script that replays staged export
  objects into the ecosystem `hivemind` database. Lives in the ecosystem repo,
  not this `samus` worktree; flagged here as a cross-repo dependency.

> **Operator decision D-9 — export cadence.** Recommend **every 4 hours**, to
> line up with the existing `Pull-SamusCloudState` task so export and ingest
> share a rhythm. Daily is acceptable if the operator wants smaller, less
> frequent batches. Faster than hourly is not useful — experiences are
> aggregated learnings, not events.

> **Operator-confirm — ingest target.** §7.3 step 3 writes into the
> *ecosystem's* shared `hivemind` Hivemind database. The operator must confirm
> the ecosystem-side ingester applies the ecosystem's own confidence/governance
> gate (the ≥0.8 promotion rule, quorum, etc.) — the GCP Samus's
> `tier:'hivemind'` flag is a *recommendation to promote*, and the ecosystem
> remains the authority on what enters its collective graph. The feed must not
> bypass ecosystem KG governance.

---

## 8. Open risks

1. **W-1 is a hard blocker, not a nice-to-have.** Until `webhook.py` and
   `upsell_queue.py` write to Firestore, finance on Cloud Run loses the Stripe
   idempotency log and the upsell queue on every scale-to-zero / new revision.
   Do **not** repoint the live Stripe webhook (§6.1 step 4) before W-1 ships
   and is verified. Interim mitigation `--min-instances=1 --max-instances=1
   --concurrency=1` reduces but does not eliminate the loss (a new revision
   still starts with an empty filesystem).
2. **AWS keys cross-cloud.** Cloud Run services authenticate to AWS SQS/DDB
   with a long-lived AWS access key delivered via Secret Manager. This is a
   standing credential in two clouds. Risks: (a) the key in
   `aws-access-key-id` was minted for the local/VM host — confirm it is not
   IP-restricted in AWS IAM or Cloud Run calls will be denied; (b) no
   automatic rotation. Mitigation: a dedicated least-privilege AWS IAM user
   scoped to exactly the 8 SQS queues + 13 DDB tables, rotated on a schedule.
   (Reference memory: `Samus AWS infra` — account 820242924193, us-west-1.)
   The §7 export job also uses this key (DDB read) — same risk surface.
3. **Egress cost / latency to AWS.** Every workcell makes outbound calls to
   AWS `us-west-1` from GCP `us-west1`. Cross-cloud egress is billed
   per-GB and adds ~1-3ms latency. SQS long-poll worker traffic is small;
   DDB-heavy CRM traffic is the one to watch. Not a blocker, monitor.
4. **Standalone AuraDB internal tiering (D-6) — W-4 landed 2026-05-21.** The
   private/`hivemind` two-tier split is now a `tier` property on the single
   AuraDB `neo4j` database (`SAMUS_KG_TIER_MODE=label`), with `promote_node()`
   / `list_hivemind_nodes()` helpers in `backend/memory/tiers.py`. The code is
   covered by tests and dormant locally (`kg_tier_mode` default `off`).
   *Residual risk:* the convention has not yet run against a live AuraDB
   instance, and the §7 export (W-6) depends on `tier:'hivemind'` being set on
   the right nodes — verify after first `samus-memory` deploy with the §5.4
   `MATCH (n) RETURN ... n.tier` check.
5. **Worker health-port (W-3) — landed 2026-05-21.** `serve_worker` now starts
   a `$PORT` health server on Cloud Run, so the 9 worker services satisfy the
   startup probe. *Residual risk:* none in code; confirm with the §4.4 worker
   log check after first deploy.
6. **`min-instances` cost floor.** `samus-memory`, `samus-finance`,
   `samus-voice`, `samus-gateway` and all 9 workers run `min-instances≥1` with
   CPU allocated — that is **13 always-on instances**. At Cloud Run's
   1-vCPU/512Mi pricing this is a non-trivial monthly floor (rough order:
   13 × ~$25-40/mo idle ≈ $325-520/mo before any request traffic). The §7
   experience feed is a **Cloud Run job** (not an always-on service), so it
   adds only per-run compute (a few minutes every 4h ≈ negligible, well under
   $5/mo) — it does **not** raise the always-on floor. Operator should confirm
   the $325-520/mo floor is acceptable vs. the local-host cost, or reconsider
   the worker model (D-1 option b: Cloud Run jobs on a schedule trade latency
   for a near-zero idle cost). The standalone AuraDB instance adds ~$65/mo
   (AuraDB Professional, D-5) on top of the Cloud Run floor.
7. **Internal-ingress + unauthenticated first cut (D-4).** Shipping internal
   services as `--allow-unauthenticated --ingress internal` before W-2 means
   the only inter-service trust is HMAC + the internal-ingress boundary. A
   compromised service in the project could call siblings. Acceptable for an
   initial cut; W-2 (OIDC id-tokens) closes it.
8. **Stripe webhook double-window.** During §6.1 steps 2-5 two finance
   endpoints are live. Safe *only because* W-1 makes the idempotency log
   shared (Firestore). If W-1 is incomplete, skip the dual-endpoint test and
   accept a hard cutover with a short event-loss risk window instead.
9. **`feedback`/`ses` image-dir mismatch.** The `feedback` workcell builds
   from `docker/workcells/ses/Dockerfile` (the dir is `ses`, the module is
   `feedback`). The §4.1 build loop handles this with a special-case, but any
   hand-run `docker build` must remember it or it builds the wrong image.
10. **Cloud Build AR-push block.** Confirmed project-level issue (memory:
    `GCP Cloud Build AR scope block`). All image builds MUST be local
    `docker build` + `docker push`. Do not let a CI pipeline silently switch
    to Cloud Build.
11. **Experience feed — PII leakage at the boundary (NEW).** Class (3) deal
    outcomes (§7.1) carry customer-identifying data in the GCP Samus's CRM. If
    the D-8 anonymisation rule is incomplete, raw customer PII flows into the
    ecosystem Hivemind — a privacy regression and a wider blast radius. The
    export job MUST anonymise class (3) *before* writing the export object;
    the export bucket should never contain un-anonymised PII. Treat W-6's
    anonymisation step as security-relevant code (review under the
    adversarial-interpretation lens).
12. **Experience feed — bypassing ecosystem KG governance (NEW).** The GCP
    Samus's `tier:'hivemind'` flag is only a *recommendation*. If §7.3's
    ecosystem-side ingester (option 3a/3b) writes straight into the ecosystem
    `hivemind` database without applying the ecosystem's own confidence/quorum
    gate, a standalone deployment's local opinion silently becomes ecosystem
    canon. The ingester must route through ecosystem KG governance — see the
    §7.4 operator-confirm note.

---

## 9. Work-items checklist (code changes gating the deploy)

| ID | Change | Files | Gates |
|---|---|---|---|
| **W-1** | Pluggable ledger backend: `JsonlLedger` → `FirestoreLedger`/`GcsLedger`, selected by `SAMUS_LEDGER_BACKEND`. | `backend/common/persistence.py`, `backend/finance/webhook.py`, `backend/finance/upsell_queue.py`, `backend/common/storage.py`, `backend/common/audit_ledger.py` | finance, crm, intake, voice, feedback on Cloud Run |
| **W-2** | Inter-workcell HTTP client attaches an OIDC id-token. | the common HTTP client used for `*_URL` calls | flipping internal services to `--no-allow-unauthenticated` (optional first cut) |
| **W-3** | ✅ **DONE 2026-05-21.** Worker modules bind a trivial `GET /health` server on `$PORT` alongside the poll loop. Landed in `serve_worker` (the `python -m backend.<svc>.worker` entrypoint): when `$PORT` is set — Cloud Run injects it — `start_health_server` runs a daemon-thread `ThreadingHTTPServer` answering `GET /health` and `/` with 200; when `$PORT` is unset (local Compose) it is skipped, so the local stack is unchanged. Covered by `tests/test_worker_health.py`. | `backend/common/worker_base.py` (`serve_worker` + `start_health_server`) | all 9 worker services |
| **W-4** | ✅ **DONE 2026-05-21.** *(REVISED — standalone)* `backend/memory/` gains a `tier` property convention (`private` / `hivemind`) on the single AuraDB `neo4j` database, selected by `SAMUS_KG_TIER_MODE` (Settings field `kg_tier_mode`, default `off`). New `backend/memory/tiers.py` holds `stamp_default_tier` (knowledge-ingest stamps `tier:'private'` when mode=`label`), `promote_node()` (flips a node to `tier:'hivemind'`) and `list_hivemind_nodes()` (the W-6 export source). `graph_client.py` gains `promote_node` / `nodes_in_tier` + a `POST /graph/promote` endpoint on `samus-memory`. Internal to the standalone instance — **no** cross-ecosystem multi-DB code. Covered by `tests/test_kg_tiers.py` + the W-4 cases in `tests/test_common_graph_client.py`. | `backend/common/{config,settings,graph_schema,graph_client}.py`, `backend/memory/{tiers,knowledge_ingest,app}.py` | `samus-memory` on the standalone AuraDB instance (D-6 option 6b); also a prerequisite for W-6 |
| W-5 (ops) | Extend `Bootstrap-CloudSecrets.ps1` `$Mappings` with `neo4j-uri`, `neo4j-user`, `samus-ledger-secret-key`, `vapi-assistant-id`, `vapi-phone-number-id`, `google-pagespeed-api-key`, `gateway-bearer-token`. | `D:\tools\hustleforge-git\Bootstrap-CloudSecrets.ps1` | §2.6 |
| **W-6** | *(NEW — experience feed, §7)* (a) new `backend/memory/experience_export.py` — exports the 3 experience classes (KG promotions, operational outcomes, anonymised deal outcomes) to the GCS export bucket on a high-water-mark delta; (b) add **Sink 6** to `Pull-SamusCloudState.ps1` to download + stage export objects; (c) ecosystem-side ingester replaying staged exports into the ecosystem `hivemind` (cross-repo). | `backend/memory/experience_export.py` (new), `backend/memory/*`, `D:\tools\hustleforge-git\Pull-SamusCloudState.ps1`, ecosystem repo (ingester) | the experience feed only — **not** a core-stack deploy gate; lands after the 30-service stack is live |

---

## 10. Operator decision summary

| ID | Decision | Recommendation |
|---|---|---|
| D-1 | SQS worker model: service / job / co-process | Always-on Cloud Run **service** (`min=1`, `--no-cpu-throttling`) |
| D-2 | Service naming: keep `-2026` or standardise | Standardise on `samus-<workcell>` (then fix the `Pull-SamusCloudState.ps1` Sink-3 filter) |
| D-3 | Neo4j target | **Dedicated standalone AuraDB instance** — fresh graph, NOT the local Neo4j, NOT the ecosystem Hivemind, NOT Neptune |
| D-4 | Internal-service auth for first cut | `--ingress internal --allow-unauthenticated` first, W-2 + `--no-allow-unauthenticated` later |
| D-5 | AuraDB tier | **AuraDB Professional** smallest RAM tier (Free's 3-day auto-pause is unsafe for prod) — *unchanged by the standalone clarification* |
| D-6 | Two-tier private/`hivemind` representation on one AuraDB instance | *(REVISED)* **Option 6b** — one standalone instance, the `neo4j` database, private/`hivemind` split as a `tier` label/property (`SAMUS_KG_TIER_MODE=label`, W-4). No cross-ecosystem multi-DB. |
| D-7 | AuraDB users | `CREATE USER neo4j_samus` + `GRANT ROLE editor`; elaborate per-tier RBAC unnecessary on a single-tenant standalone instance |
| **D-8** | *(NEW)* Experience-feed scope + anonymisation | Ship all 3 classes (KG promotions, operational outcomes, deal outcomes); **anonymise class (3)** at the export boundary. Default if unconfirmed: class (1) only. **Operator must confirm scope + PII rule.** |
| **D-9** | *(NEW)* Experience-feed export cadence | **Every 4 hours**, to line up with the existing `Pull-SamusCloudState` task; daily acceptable. |

---

*End of runbook. The infrastructure steps herein have not been executed — that
remains a plan for the operator. Code-gate status (2026-05-21): **W-3 (worker
health port) and W-4 (KG tier mapping) are merged**; **W-1 (Firestore ledger)
is the one remaining hard code gate** — required before any production deploy
of finance/crm/intake/voice/feedback. The dedicated standalone AuraDB instance
must still be provisioned (§5) before `samus-memory`. The §7 experience feed
(W-6) is post-deploy work — it does not gate the 30-service core stack.*
