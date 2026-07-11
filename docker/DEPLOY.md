# Samus -> GCP Cloud Run: Operator Runbook

Audience: operator. Technical, comfortable with commands,
newer to gcloud. Read top-to-bottom the first time, then jump to section 7
for day-to-day redeploys.

Updated: 2026-05-19 — rewritten for the local-build-and-push deploy path and
the 21-workcell stack. Mirrors `Samus/docker/workcells/*/Dockerfile`,
`Samus/docker/base/Dockerfile`, and `Samus/docker/cloudbuild.yaml` (kept as a
reference, not a submittable pipeline — see section 6) at this commit.

---

## 1. Architecture Overview

The Samus stack is **21 FastAPI workcells** (gateway + 20 domain workcells)
sharing one base image. The same image set runs in two deployment targets:

```
  Docker Compose            <-- local dev: Windows host + HustleForge-VM (192.168.1.240)
  GCP Cloud Run (us-west1)  <-- this runbook
```

Both targets share the **same persistence layer**: AWS SQS / DynamoDB / SNS
in region `us-west-1`. That cross-cloud share is what makes failover work
(section 5).

### 1.1 The 21 workcells

16 existed before this revision; 5 (`signal_filter`, `path_optimizer`,
`template_recovery`, `portfolio_controller`, `entropy`) are being added in
parallel. The 5 new ones are HTTP-only, internal-network-only, container
port 8080, no host port. After integration the stack is 21:

| #  | Workcell               | Cloud Run service                  | Ingress    |
|----|------------------------|------------------------------------|------------|
| 1  | gateway                | `samus-2026`                       | internal   |
| 2  | leadgen                | `samus-leadgen-2026`               | internal   |
| 3  | prospecting            | `samus-prospecting-2026`           | internal   |
| 4  | scaffold               | `samus-scaffold-2026`              | internal   |
| 5  | fulfillment            | `samus-fulfillment-2026`           | internal   |
| 6  | memory                 | `samus-memory-2026`                | internal   |
| 7  | feedback               | `samus-feedback-2026`              | **all**    |
| 8  | outreach               | `samus-outreach-2026`              | internal   |
| 9  | optimizer              | `samus-optimizer-2026`             | internal   |
| 10 | proposal               | `samus-proposal-2026`              | internal   |
| 11 | seo                    | `samus-seo-2026`                   | internal   |
| 12 | finance                | `samus-finance-2026`               | **all**    |
| 13 | voice                  | `samus-voice-2026`                 | **all**    |
| 14 | intake                 | `samus-intake-2026`                | internal   |
| 15 | crm                    | `samus-crm-2026`                   | internal   |
| 16 | strategy               | `samus-strategy-2026`              | internal   |
| 17 | signal_filter          | `samus-signal-filter-2026`         | internal   |
| 18 | path_optimizer         | `samus-path-optimizer-2026`        | internal   |
| 19 | template_recovery      | `samus-template-recovery-2026`     | internal   |
| 20 | portfolio_controller   | `samus-portfolio-controller-2026`  | internal   |
| 21 | entropy                | `samus-entropy-2026`               | internal   |

Notes on the table:

- The **gateway** lives at the bare `samus-2026` service (no `-gateway`
  suffix) — it is the canonical entry point and the only service pinned to
  `min-instances=1`. Every other Cloud Run service follows the
  `samus-<workcell>-2026` pattern.
- The **feedback** workcell builds from the directory
  `Samus/docker/workcells/ses/Dockerfile` (it handles AWS SES bounce /
  complaint events). The directory is named `ses/` for historical reasons;
  the image, the env var, and the service are all `feedback`.
- Cloud Run service names use **hyphens**; the underscore workcell names
  (`signal_filter`, `path_optimizer`, `template_recovery`,
  `portfolio_controller`) become `signal-filter`, `path-optimizer`, etc. in
  service and image names. The `SAMUS_SERVICE` env var keeps the underscore
  form.

### 1.2 GCP target

| Setting           | Value                                               |
|-------------------|-----------------------------------------------------|
| Project           | `${GCP_PROJECT}`                                    |
| Region            | `us-west1`                                          |
| Artifact Registry | repo `samus` in `us-west1`                          |
| Image host        | `us-west1-docker.pkg.dev/${GCP_PROJECT}/samus/...`  |
| Runtime SA        | `${SAMUS_RUNTIME_SA}@${GCP_PROJECT}.iam.gserviceaccount.com` |

All services run `--no-allow-unauthenticated`. Three are `ingress=all`
because they receive third-party webhook callbacks from outside GCP:

- **feedback** — AWS SNS delivers SES bounce / complaint notifications.
- **finance** — Stripe POSTs `checkout.session.completed` webhooks.
- **voice**   — Vapi POSTs end-of-call webhooks.

Each of the three verifies inbound auth itself (SNS signature, Stripe HMAC,
Vapi HMAC) inside the workcell, so `--no-allow-unauthenticated` still blocks
unauthenticated bypass of Cloud Run's identity layer — `ingress=all` only
controls *network reachability*, not *authentication*.

### 1.3 AWS persistence (cross-cloud, shared)

SQS, DynamoDB, and SNS live in AWS region `us-west-1` (hyphenated — note
this differs from the GCP `us-west1`). Persistence is **shared by every
deployment target** — the Compose stack and the Cloud Run services talk to
the *same* queues and tables. Nothing is per-target. Cloud Run workcells
reach AWS using IAM keys bound from Secret Manager (section 3).

---

## 2. Prerequisites

### 2.1 gcloud CLI

If `gcloud --version` fails, install it. Pick one:

```powershell
# Option A: winget (if winget is present)
winget install --id Google.CloudSDK

# Option B: direct installer (works headless / no winget)
$url = "https://dl.google.com/dl/cloudsdk/channels/rapid/GoogleCloudSDKInstaller.exe"
Invoke-WebRequest -Uri $url -OutFile "$env:TEMP\GoogleCloudSDKInstaller.exe"
Start-Process "$env:TEMP\GoogleCloudSDKInstaller.exe" -Wait

# Option C: zero-install — use Cloud Shell in the browser
#   https://shell.cloud.google.com/?project=${GCP_PROJECT}
```

Verify:

```powershell
gcloud --version
```

### 2.2 Docker

The local-build deploy path runs `docker build` and `docker push` on this
host. Docker Desktop must be running:

```powershell
docker version
```

### 2.3 Authenticate gcloud + set the active project

```powershell
gcloud auth login
gcloud config set project ${GCP_PROJECT}
gcloud config set run/region us-west1
gcloud config list
```

### 2.4 Authenticate Docker to Artifact Registry

This is the step that lets `docker push` write to AR. It wires a credential
helper into `~/.docker/config.json` so `docker push` to
`us-west1-docker.pkg.dev` reuses your gcloud login:

```powershell
gcloud auth configure-docker us-west1-docker.pkg.dev
```

Run it once per host. Re-run it only if `docker push` later starts failing
with a 401/403 auth error.

### 2.5 Required GCP APIs

```powershell
gcloud services enable `
    run.googleapis.com `
    artifactregistry.googleapis.com `
    secretmanager.googleapis.com `
    --project=${GCP_PROJECT}
```

(`cloudbuild.googleapis.com` is no longer required — see section 6.)

### 2.6 Artifact Registry repo

Images push to `us-west1-docker.pkg.dev/${GCP_PROJECT}/samus/...`. Verify
the `samus` repo exists, create it if not:

```powershell
gcloud artifacts repositories describe samus `
    --location=us-west1 --project=${GCP_PROJECT}

# if missing:
gcloud artifacts repositories create samus `
    --repository-format=docker `
    --location=us-west1 `
    --project=${GCP_PROJECT}
```

### 2.7 Runtime service-account IAM

Cloud Run services run as `${SAMUS_RUNTIME_SA}@${GCP_PROJECT}.iam.gserviceaccount.com`.
That SA needs to read the bound secrets at container start:

```powershell
gcloud projects add-iam-policy-binding ${GCP_PROJECT} `
    --member="serviceAccount:${SAMUS_RUNTIME_SA}@${GCP_PROJECT}.iam.gserviceaccount.com" `
    --role="roles/secretmanager.secretAccessor"
```

Your *own* account needs `roles/run.admin` (to deploy) and, on
`samus-runtime@...`, `roles/iam.serviceAccountUser` (to deploy a service
that runs *as* that SA):

```powershell
$ME = gcloud config get-value account
gcloud projects add-iam-policy-binding ${GCP_PROJECT} `
    --member="user:$ME" --role="roles/run.admin"
gcloud iam service-accounts add-iam-policy-binding `
    ${SAMUS_RUNTIME_SA}@${GCP_PROJECT}.iam.gserviceaccount.com `
    --member="user:$ME" --role="roles/iam.serviceAccountUser" `
    --project=${GCP_PROJECT}
```

---

## 3. Secrets in GCP Secret Manager (before first deploy)

Each `gcloud run deploy` binds secrets with `--set-secrets=`. Every secret a
service references **must exist in Secret Manager before that service is
deployed**, or the deploy fails.

| Secret name             | Used by service(s)                       | Source (DPAPI store name)         |
|-------------------------|------------------------------------------|------------------------------------|
| `shared-hmac-key`       | every workcell (all 21 services)         | `Samus/SharedHmacKey`              |
| `aws-access-key-id`     | every workcell (all 21 services)         | `Samus/AwsAccessKeyId`             |
| `aws-secret-access-key` | every workcell (all 21 services)         | `Samus/AwsSecretAccessKey`         |
| `places-api-key`        | prospecting                              | `Samus/GooglePlacesApiKey`         |
| `openai-api-key`        | prospecting, memory, seo                 | `Samus/OpenaiApiKey`               |
| `hivemind-password`     | memory                                   | `Samus/HivemindPassword`           |
| `stripe-api-key`        | finance                                  | `Samus/StripeApiKey`               |
| `stripe-webhook-secret` | finance                                  | `Samus/StripeWebhookSecret`        |
| `sendgrid-api-key`      | finance (outbound transactional email)   | `Samus/SendgridApiKey`             |
| `sendgrid-from-email`   | finance (from address; bound as a secret for env-injection consistency) | `Samus/SendgridFromEmail` |
| `vapi-api-key`          | voice                                    | `Samus/VapiApiKey`                 |
| `vapi-webhook-secret`   | voice                                    | `Samus/VapiWebhookSecret`          |

The 5 new workcells (signal_filter, path_optimizer, template_recovery,
portfolio_controller, entropy) carry only the three universal secrets
(`shared-hmac-key`, `aws-access-key-id`, `aws-secret-access-key`).

Create the secrets WITHOUT leaving values in chat history or terminal
scrollback. Recommended path: Cloud Shell + `read -rs` so the value never
appears on stdout, never lands in `.bash_history`, and is unset right after
the version is added.

Open Cloud Shell: <https://shell.cloud.google.com/?project=${GCP_PROJECT}>

Run this block per secret, swapping the name each time. It creates the
secret if it does not exist and adds a new version if it does (safe to
re-run after a rotation):

```bash
NAME=shared-hmac-key   # change per secret
read -rsp "${NAME}: " V; echo
if gcloud secrets describe "$NAME" --project=${GCP_PROJECT} >/dev/null 2>&1; then
  printf %s "$V" | gcloud secrets versions add "$NAME" \
      --data-file=- --project=${GCP_PROJECT}
else
  printf %s "$V" | gcloud secrets create "$NAME" \
      --replication-policy=automatic \
      --data-file=- --project=${GCP_PROJECT}
fi
unset V
```

Pull each value from DPAPI on the host first
(`Get-HfSecret -Scope Samus -Name SharedHmacKey`) and paste it into the
Cloud Shell prompt — terminal echo is suppressed.

**Cross-cloud note:** `aws-access-key-id` / `aws-secret-access-key` must be
the current rotated keys in DPAPI. Cloud Run workcells call AWS SQS /
DynamoDB / SNS with them.

Verify all secrets are present:

```powershell
gcloud secrets list --project=${GCP_PROJECT} --format="table(name)"
```

---

## 4. Build + push the images (the current deploy path)

The deploy is two phases per workcell: **(a)** build the image locally and
push it to Artifact Registry, then **(b)** `gcloud run deploy` that image.
Two helpers are available:

- `Deploy-SamusLocal.ps1` under `D:\tools\hustleforge-git\` — deploys the
  21 HTTP app services (original 13-service script, updated for full stack).
- `Deploy-SamusCloudRun.ps1` under `Samus/scripts/` — full 30-service
  deployment (21 app + 9 SQS workers) with tiered ordering, URL discovery,
  and Cloud Scheduler integration via `Register-CloudSchedulerJobs.ps1`.

The manual steps here are what the helpers do, so you can run them by hand
to debug or to deploy a single service.

All `docker build` commands run from the **Hustleforge repo root** (parent
of `Samus/`) because the Dockerfiles `COPY Samus/...` from there.

### 4.1 Build + push the base image first

Every workcell image is `FROM` the base. Build and push it before any
workcell:

```powershell
cd D:\Hustleforge
$REG  = "us-west1-docker.pkg.dev/${GCP_PROJECT}/samus"

docker build -f Samus/docker/base/Dockerfile -t "$REG/samus-base:latest" .
docker push "$REG/samus-base:latest"
```

### 4.2 Build + push each workcell image

Each workcell build takes `--build-arg BASE_IMAGE=...` so the workcell layers
on the base you just pushed. The image name uses **hyphens** even where the
workcell name has an underscore. Pattern:

```powershell
cd D:\Hustleforge
$REG = "us-west1-docker.pkg.dev/${GCP_PROJECT}/samus"

# <workcell-dir> is the directory under Samus/docker/workcells/
# <image-name>   is the hyphenated image name
# (these match for all but: ses/ -> feedback, and the underscore -> hyphen ones)
docker build `
    --build-arg "BASE_IMAGE=$REG/samus-base:latest" `
    -f Samus/docker/workcells/<workcell-dir>/Dockerfile `
    -t "$REG/samus-<image-name>:latest" `
    .
docker push "$REG/samus-<image-name>:latest"
```

The full set, directory -> image name:

| Workcell dir            | Image name                  |
|-------------------------|-----------------------------|
| `gateway`               | `samus-gateway`             |
| `leadgen`               | `samus-leadgen`             |
| `prospecting`           | `samus-prospecting`         |
| `scaffold`              | `samus-scaffold`            |
| `fulfillment`           | `samus-fulfillment`         |
| `memory`                | `samus-memory`              |
| `ses`                   | `samus-feedback`            |
| `outreach`              | `samus-outreach`            |
| `optimizer`             | `samus-optimizer`           |
| `proposal`              | `samus-proposal`            |
| `seo`                   | `samus-seo`                 |
| `finance`               | `samus-finance`             |
| `voice`                 | `samus-voice`               |
| `intake`                | `samus-intake`              |
| `crm`                   | `samus-crm`                 |
| `strategy`              | `samus-strategy`            |
| `signal_filter`         | `samus-signal-filter`       |
| `path_optimizer`        | `samus-path-optimizer`      |
| `template_recovery`     | `samus-template-recovery`   |
| `portfolio_controller`  | `samus-portfolio-controller`|
| `entropy`               | `samus-entropy`             |

`Deploy-SamusLocal.ps1` iterates this table for you. Run the manual form
when you only need to rebuild one workcell.

---

## 5. Deploy to Cloud Run

Once an image is in Artifact Registry, deploy it. Below is the gateway
(canonical service, `min/max-instances=1`) and the generic pattern for the
rest. `Deploy-SamusLocal.ps1` runs these for you after the push step.

### 5.1 Gateway

```powershell
$REG = "us-west1-docker.pkg.dev/${GCP_PROJECT}/samus"
$SA  = "${SAMUS_RUNTIME_SA}@${GCP_PROJECT}.iam.gserviceaccount.com"

gcloud run deploy samus-2026 `
    --image="$REG/samus-gateway:latest" `
    --region=us-west1 --platform=managed --port=8080 `
    --service-account="$SA" `
    --ingress=internal --no-allow-unauthenticated `
    --min-instances=1 --max-instances=1 `
    --set-env-vars="SAMUS_SERVICE=gateway,AWS_REGION=us-west-1,AWS_DEFAULT_REGION=us-west-1" `
    --set-secrets="SAMUS_SHARED_HMAC_KEY=shared-hmac-key:latest,AWS_ACCESS_KEY_ID=aws-access-key-id:latest,AWS_SECRET_ACCESS_KEY=aws-secret-access-key:latest"
```

### 5.2 Generic internal workcell

For every internal workcell (the 17 with `internal` ingress) the deploy is
the same shape — only the service name, image, `SAMUS_SERVICE`, and the
extra secrets change:

```powershell
gcloud run deploy samus-<workcell>-2026 `
    --image="$REG/samus-<image-name>:latest" `
    --region=us-west1 --platform=managed --port=8080 `
    --service-account="$SA" `
    --ingress=internal --no-allow-unauthenticated `
    --set-env-vars="SAMUS_SERVICE=<workcell>,AWS_REGION=us-west-1,AWS_DEFAULT_REGION=us-west-1" `
    --set-secrets="SAMUS_SHARED_HMAC_KEY=shared-hmac-key:latest,AWS_ACCESS_KEY_ID=aws-access-key-id:latest,AWS_SECRET_ACCESS_KEY=aws-secret-access-key:latest"
```

Per-workcell secret extras (append to `--set-secrets`):

- **prospecting** — `GOOGLE_PLACES_API_KEY=places-api-key:latest,OPENAI_API_KEY=openai-api-key:latest`
- **memory** — `OPENAI_API_KEY=openai-api-key:latest,NEO4J_PASSWORD=hivemind-password:latest`
- **seo** — `OPENAI_API_KEY=openai-api-key:latest`

The 5 new workcells use the generic form unchanged — `SAMUS_SERVICE` is the
underscore name (`signal_filter`, `path_optimizer`, `template_recovery`,
`portfolio_controller`, `entropy`), the service/image name uses hyphens.

### 5.3 Webhook workcells (ingress=all)

feedback / finance / voice are deployed with `--ingress=all` instead of
`--ingress=internal`. finance additionally sets `EMAIL_BACKEND=sendgrid` and
binds the Stripe + SendGrid secrets; voice binds the Vapi secrets; feedback
takes only the three universal secrets. Example — finance:

```powershell
gcloud run deploy samus-finance-2026 `
    --image="$REG/samus-finance:latest" `
    --region=us-west1 --platform=managed --port=8080 `
    --service-account="$SA" `
    --ingress=all --no-allow-unauthenticated `
    --set-env-vars="SAMUS_SERVICE=finance,AWS_REGION=us-west-1,AWS_DEFAULT_REGION=us-west-1,EMAIL_BACKEND=sendgrid" `
    --set-secrets="SAMUS_SHARED_HMAC_KEY=shared-hmac-key:latest,STRIPE_API_KEY=stripe-api-key:latest,STRIPE_WEBHOOK_SECRET=stripe-webhook-secret:latest,SENDGRID_API_KEY=sendgrid-api-key:latest,SENDGRID_FROM_EMAIL=sendgrid-from-email:latest,AWS_ACCESS_KEY_ID=aws-access-key-id:latest,AWS_SECRET_ACCESS_KEY=aws-secret-access-key:latest"
```

The exact secret bindings for all 21 services are enumerated in
`Samus/docker/cloudbuild.yaml` — that file is no longer submitted but is
kept as the authoritative map of build args and deploy flags (section 6).

---

## 6. Why not Cloud Build (and what `cloudbuild.yaml` is now for)

The original deploy path was `gcloud builds submit` against
`Samus/docker/cloudbuild.yaml` — Cloud Build would build every image on its
own runtime VMs and push to Artifact Registry. **That path dead-ended.**

For GCP projects created after April 2024 (which `${GCP_PROJECT}` is), the
Cloud Build runtime VM runs with a restricted OAuth scope. Even with the
Cloud Build service account holding `roles/artifactregistry.writer`, the
`docker push` step inside the build returns **403**. The build VM's scope is
the blocker, not the IAM policy — so no amount of IAM tweaking fixes it.
`cloudbuild-intake.yaml` was a last attempt to route around it (fetch an
access token from the metadata server and `docker login` inline); it is also
abandoned.

**The current path:** build images locally with `docker build` on this host
(your Docker Desktop, full scope), `docker push` to Artifact Registry (auth
via `gcloud auth configure-docker`, section 2.4), then `gcloud run deploy`.
This is exactly what sections 4 and 5 above describe, and what
`Deploy-SamusLocal.ps1` automates.

`Samus/docker/cloudbuild.yaml` and `cloudbuild-intake.yaml` are **kept in the
repo, marked DEPRECATED in a header, not submitted**. They are retained as a
precise, current reference: the `docker build` args, the `--build-arg
BASE_IMAGE=...` wiring, every `gcloud run deploy` flag, the `--set-secrets`
binding for all 21 services, and the build ordering (base first, then
workcells) are all enumerated there and kept accurate. When you need to know
the exact flags a given service is deployed with, read `cloudbuild.yaml` —
just do not run `gcloud builds submit` on it.

---

## 7. Day-to-day redeploy

To ship a code change to one workcell (say `prospecting`):

```powershell
cd D:\Hustleforge
$REG = "us-west1-docker.pkg.dev/${GCP_PROJECT}/samus"
$SA  = "${SAMUS_RUNTIME_SA}@${GCP_PROJECT}.iam.gserviceaccount.com"

# 1. rebuild + push (base only needs rebuilding if requirements changed)
docker build --build-arg "BASE_IMAGE=$REG/samus-base:latest" `
    -f Samus/docker/workcells/prospecting/Dockerfile `
    -t "$REG/samus-prospecting:latest" .
docker push "$REG/samus-prospecting:latest"

# 2. redeploy — Cloud Run cuts a new revision and shifts traffic to it
gcloud run deploy samus-prospecting-2026 `
    --image="$REG/samus-prospecting:latest" `
    --region=us-west1
```

Step 2 only needs `--image` and `--region` on a redeploy — Cloud Run keeps
the ingress, SA, env vars, and secret bindings from the prior revision.

To ship a change that touches `Samus/requirements.txt` or
`Samus/docker/base/`, rebuild + push the base first (section 4.1), then
rebuild + push **every** workcell, then redeploy them. Use
`Deploy-SamusLocal.ps1` for the full-stack case.

There is **no DNS cutover** and nothing to flip — see section 8.

---

## 8. Failover between deployment targets

Failover between Docker Compose and Cloud Run is **automatic and DNS-free**.
Every workcell that consumes work long-polls the same AWS SQS queues. SQS is
a **competing-consumers** broker: a message is delivered to exactly one
consumer, whichever asks first. If the local Compose stack stops polling
(host off, `docker compose down`), the Cloud Run replicas remain — or scale
up from zero on the next request — and pick up the work.

There is **no cutover script, no DNS record to change, no load balancer to
re-point**. Both targets are always eligible; whichever is up wins the next
message. Bringing the local stack back simply adds it back to the pool and
work round-robins again.

Caveat: this assumes the Cloud Run side is actually *polling* SQS, not just
serving HTTP — see section 10.4.

---

## 9. Rollback

Each `gcloud run deploy` produces a new immutable revision per service. To
revert one service to a known-good prior revision:

```powershell
gcloud run revisions list `
    --service=samus-prospecting-2026 `
    --region=us-west1 --project=${GCP_PROJECT}

gcloud run services update-traffic samus-prospecting-2026 `
    --to-revisions=samus-prospecting-2026-00003-abc=100 `
    --region=us-west1 --project=${GCP_PROJECT}
```

Old revisions stay parked at 0% traffic and can be re-routed instantly — no
rebuild required for a one-service rollback.

To roll the **whole stack** back to a prior commit, check that commit out,
rebuild + push every image at that commit (section 4), and redeploy
(section 5). Old images stay in Artifact Registry indefinitely unless you
set a cleanup policy on the `samus` repo.

---

## 10. Post-deploy verification + known gaps

### 10.1 Probe one service

```bash
SERVICE=samus-prospecting-2026
URL=$(gcloud run services describe "$SERVICE" --region=us-west1 \
      --format='value(status.url)' --project=${GCP_PROJECT})
TOKEN=$(gcloud auth print-identity-token)
curl -H "Authorization: Bearer $TOKEN" "$URL/health"
```

Expected: `200 OK`, JSON body with `"status": "ok"` (or `"degraded"` if a
downstream is unreachable — the body names which subsystem).

A 403 means the runtime SA is missing `roles/secretmanager.secretAccessor`
on a bound secret, or your account lacks `roles/run.invoker` on the service.

### 10.2 Python version (watch, probably fine)

`Samus/docker/base/Dockerfile` builds on `python:3.13-slim-bookworm`. The
host-fleet venv is 3.11. Pydantic v2, FastAPI, and boto3 all support
3.11–3.13, so this is most likely a non-issue — but if Cloud Run logs show
an `ImportError` / `AttributeError` on first request after a deploy, this is
the suspect. Fix: pin the base to `python:3.11-slim-bookworm`, or re-resolve
requirements on 3.13.

### 10.3 Neo4j unreachable from Cloud Run

The Hustleforge Neo4j DBMS runs on the host at `127.0.0.1:7687`. Cloud Run
cannot reach the host's loopback. The memory workcell sets
`neo4j_required=false` and degrades gracefully — graph operations return
`{"status":"unavailable"}` and the rest of the workcell keeps running. For
real graph queries from Cloud Run, point memory at a managed Neo4j Aura
instance and bind a new `neo4j-aura-uri` secret.

### 10.4 Cloud Run does not host the SQS workers

The Compose stack runs **10 SQS worker sidecars** (crm, leadgen,
prospecting, scaffold, fulfillment, feedback, outreach, optimizer, proposal,
seo) plus a one-shot `samus-data-init` container. Those workers are the
`backend.<svc>.worker` modules that long-poll SQS. Cloud Run scales to zero
between requests, so a forever-polling worker is the wrong fit — Cloud Run
hosts only the **HTTP-app side** (`uvicorn backend.<svc>.app:app`) of each
workcell.

Two ways to put workers on Cloud Run (neither built yet):

- **Cloud Run Jobs + Cloud Scheduler** — a Job that runs every N minutes,
  drains the queue, exits. Predictable cost, slight latency.
- **EventBridge -> Lambda fan-out from SQS -> HTTP-call the Cloud Run
  workcell** — keeps Cloud Run request-driven; the poll loop moves into
  AWS-native eventing.

Until one of these exists, the failover loop (section 8) only covers the
HTTP request path, not the queue-worker path, on the Cloud Run side.

### 10.5 Only base + gateway + feedback are CVE-scanned

The reference `cloudbuild.yaml` gates HIGH/CRITICAL CVEs (Trivy) on the base
image, the gateway, and feedback (the public SNS endpoint). The other
workcell images inherit the base scan but are not scanned individually.
Acceptable for an internal-ingress mesh; revisit if any internal workcell
ever gets `ingress=all`. When building locally, you can run the same gate by
hand: `trivy image --severity=HIGH,CRITICAL --ignore-unfixed --exit-code=1
us-west1-docker.pkg.dev/${GCP_PROJECT}/samus/samus-<name>:latest`.

### 10.6 Autoscaling

Only the gateway is pinned (`min=1, max=1`). Every other service uses Cloud
Run defaults and cold-starts on first request. For a warm-standby failover
posture, add `--min-instances=1` to the queue-driven workcells.

---

## 11. Email deliverability (finance receipts)

The finance workcell sends transactional receipts via SendGrid on every
processed Stripe webhook. Three configuration choices affect inbox placement
(vs. spam) and operator visibility.

### 11.1 Domain Authentication (drops the "via sendgrid.net" tag)

When Gmail/Yahoo show "HustleForge **via sendgrid.net**" next to the sender,
the From domain (`hustleforge.tech`) is not fully SPF/DKIM/DMARC-aligned
under the SendGrid account's sending subdomain. The "via" annotation costs
reputation and nudges borderline messages to spam.

Fix: SendGrid Dashboard -> **Settings -> Sender Authentication ->
Authenticate Your Domain**. SendGrid generates 3 CNAME records pointing at
an `em<N>.<your-domain>` subdomain; add them at your DNS provider. SendGrid
verifies and signs outbound mail with DKIM aligned to the From domain. Gmail
drops the "via" tag once DNS propagates (minutes to a few hours). If the
Sender Authentication table has stale `em<N>` entries (Failed/Pending), delete
them — the Verified one is what is in use.

### 11.2 Reply-To routing

Receipt sends use the `SENDGRID_REPLY_TO` env var (DPAPI source:
`Samus/SendgridReplyTo`). Empty = no Reply-To header; mail clients reply to
From. Two common setups:

- **Production / branded** — From = `receipts@hustleforge.tech` (verified,
  monitored), Reply-To = empty.
- **Solo-operator with monitoring forwarder** (current setup) — From =
  `ahartman@hustleforge.tech`, Reply-To = empty. `ahartman@` forwards to
  `samushustleforge@gmail.com` (the billing-monitor inbox).
- **Reply-To override** — when From is unmonitored, set
  `Samus/SendgridReplyTo` to a directly-monitored inbox. Plumbed end-to-end
  (sendgrid.py -> email_backend.py -> settings -> deploy env -> DPAPI loader).

### 11.3 Sender reputation hygiene

- Use a domain you control as From (not gmail.com / yahoo.com) — free
  webmail senders get aggressive spam treatment through third-party relays.
- Add a List-Unsubscribe header for any marketing/nurture mail (not required
  for transactional receipts, but Gmail looks favorably on it). Future work.
- Avoid spammy subject patterns (ALL CAPS, "FREE", excessive punctuation).
  The current "Receipt for SEO Audit — $149.00 USD" pattern is fine.
- Monitor SendGrid Dashboard -> **Activity** for bounce/spam rates. Above 5%
  bounces or 0.1% spam-complaints risks sender suspension.

### 11.4 Verify the loop end-to-end after any change

```powershell
.\Samus\scripts\Test-StripeWebhookLocal.ps1 -TestEmail you@yourdomain.com
```

Sends a synthetic `checkout.session.completed` -> finance container processes
it -> SendGrid sends a real receipt. Inspect the headers (Gmail: "Show
original") for SPF=pass, DKIM=pass, DMARC=pass alignment.
