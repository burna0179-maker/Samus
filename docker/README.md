# Samus Docker + Cloud Run packaging

Deployable artifacts for the Samus multi-service stack: one shared base image
plus 21 workcell images that extend it.

For the full operator runbook (prerequisites, secrets, build + push + deploy,
rollback, failover), see **`DEPLOY.md`** in this directory.

## Layout

```
Samus/docker/
  base/
    Dockerfile          shared python:3.13-slim base + tini + samus user
    constraints.txt     major-version pin guard rails
    entrypoint.sh       umask + PYTHONPATH + $PORT default, then exec "$@"
  workcells/
    <workcell>/Dockerfile   one per workcell, FROM the base image
  compose/
    docker-compose.samus.yml   local Compose stack (workcells + SQS workers)
  cloudbuild.yaml       DEPRECATED reference of the build/deploy topology
  cloudbuild-intake.yaml DEPRECATED reference (intake-only build attempt)
  boot.sh               local dev: build, up, healthcheck, down
  DEPLOY.md             the operator runbook
  .dockerignore         keeps .venv / __pycache__ / .env out of the build context
```

The build context for every image is the **Hustleforge repo root**, because
the Dockerfiles `COPY Samus/...` from there.

## The 21 workcells

`gateway` plus 20 domain workcells, all sharing `base/Dockerfile`:

```
gateway  leadgen  prospecting  scaffold  fulfillment  memory  feedback
outreach  optimizer  proposal  seo  finance  voice  intake  crm  strategy
signal_filter  path_optimizer  template_recovery  portfolio_controller  entropy
```

Notes:

- The `feedback` workcell builds from the directory `workcells/ses/` (it
  handles AWS SES bounce/complaint events delivered via SNS).
- Each Dockerfile is a thin, uniform template: `ARG BASE_IMAGE` defaulting to
  `us-west1-docker.pkg.dev/${GCP_PROJECT}/samus/samus-base:latest`, then
  `COPY Samus/backend`, the shared entrypoint, a `/health` HEALTHCHECK, and a
  `uvicorn backend.<workcell>.app:app` CMD on port 8080. New workcell
  Dockerfiles should match `workcells/gateway/Dockerfile` as the reference.

## Local build + up

```bash
cd d:/Hustleforge
docker compose -f Samus/docker/compose/docker-compose.samus.yml build
docker compose -f Samus/docker/compose/docker-compose.samus.yml up -d

# or, easier:
bash Samus/docker/boot.sh           # build + up + healthcheck sweep
bash Samus/docker/boot.sh --check   # re-run healthchecks
bash Samus/docker/boot.sh --down    # stop everything
```

Only the gateway is published to the host (`http://127.0.0.1:8100/health`).
Every other workcell is reachable only on the internal Compose network. The
Compose stack also runs SQS worker sidecars and a one-shot `samus-data-init`
container.

## Cloud Run deploy

The current deploy path is a **local `docker build` + `docker push` to
Artifact Registry, then `gcloud run deploy`** — Cloud Build is no longer used
(its runtime VMs cannot push to AR for this project). See `DEPLOY.md`
sections 4–6 for the full procedure and the `Deploy-SamusLocal.ps1` helper.

Images and services land in:

- Project `${GCP_PROJECT}`, region `us-west1`, Artifact Registry repo `samus`
- Image host `us-west1-docker.pkg.dev/${GCP_PROJECT}/samus/...`
- Cloud Run services named `samus-<workcell>-2026` (gateway is bare `samus-2026`)

Each service runs as non-root UID 10001, listens on container port 8080, and
takes `SAMUS_SHARED_HMAC_KEY` plus AWS credentials from Secret Manager.
