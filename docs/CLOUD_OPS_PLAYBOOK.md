# Samus Cloud Operations Playbook

> Deployed 2026-06-25 | GCP project `${GCP_PROJECT}` | Region `us-west1`

---

## 1. Fleet overview

29 Cloud Run services: 20 app workcells + 9 SQS workers.

| Tier | Services | Ingress | Notes |
|------|----------|---------|-------|
| 0 (no deps) | crm, finance, leadgen, outreach, optimizer, signal-filter, path-optimizer, template-recovery, portfolio-controller, entropy | internal (finance: all) | Finance is the Stripe webhook receiver |
| 1 (call Tier 0) | prospecting, scaffold, fulfillment, seo, proposal, strategy, voice, feedback | mixed | Voice + feedback are webhook receivers (all) |
| 2 (gateway) | gateway | all | Central dispatch; `SAMUS_AUTHZ_MODE=audit` |
| 3 (intake) | intake | all | Public onboarding form endpoint |
| Workers | 9x `samus-<svc>-worker` | internal | Always-on (`min=1`), CPU unthrottled |

**Gateway URL:** `https://samus-gateway-${GCP_PROJECT_NUMBER}.us-west1.run.app`
**Service account:** `${SAMUS_RUNTIME_SA}@${GCP_PROJECT}.iam.gserviceaccount.com`

---

## 2. Health checks

### Quick fleet check
```powershell
gcloud run services list --region=us-west1 --project=${GCP_PROJECT}
```

### Gateway health
```bash
curl -fsS https://samus-gateway-${GCP_PROJECT_NUMBER}.us-west1.run.app/health
# Expected: {"status":"ok","service":"gateway","ready":true}
```

### Per-service health (public services only)
```bash
curl -fsS https://samus-finance-${GCP_PROJECT_NUMBER}.us-west1.run.app/health
curl -fsS https://samus-voice-${GCP_PROJECT_NUMBER}.us-west1.run.app/health
curl -fsS https://samus-intake-${GCP_PROJECT_NUMBER}.us-west1.run.app/health
```

Internal services require an id-token:
```bash
TOKEN=$(gcloud auth print-identity-token --audiences=https://samus-crm-${GCP_PROJECT_NUMBER}.us-west1.run.app)
curl -fsS -H "Authorization: Bearer $TOKEN" https://samus-crm-${GCP_PROJECT_NUMBER}.us-west1.run.app/health
```

---

## 3. Scheduled jobs

| Job | Schedule (PT) | Target |
|-----|---------------|--------|
| `samus-prospecting-daily` | 07:30 daily | `samus-prospecting` /api/prospecting/run_daily |
| `samus-morning-brief` | 08:00 daily | `samus-gateway` /api/gateway/morning_brief |
| `samus-outreach-daily` | 09:00 daily | `samus-outreach` /api/outreach/run_daily |
| `samus-inbox-poll` | Every 2h, 8a-6p | `samus-intake` /api/intake/poll_inbox |

### Manage jobs
```powershell
# List
gcloud scheduler jobs list --location=us-west1 --project=${GCP_PROJECT}

# Fire manually
gcloud scheduler jobs run samus-prospecting-daily --location=us-west1 --project=${GCP_PROJECT}

# Pause / Resume
gcloud scheduler jobs pause samus-inbox-poll --location=us-west1 --project=${GCP_PROJECT}
gcloud scheduler jobs resume samus-inbox-poll --location=us-west1 --project=${GCP_PROJECT}

# Re-register all (idempotent)
powershell -ExecutionPolicy Bypass -File Samus\scripts\Register-CloudSchedulerJobs.ps1
```

---

## 4. Deploying changes

### Full redeploy (build + push + deploy)
```powershell
cd D:\Hustleforge
powershell -ExecutionPolicy Bypass -File Samus\scripts\Deploy-SamusCloudRun.ps1
```

### Deploy only (images already in Artifact Registry)
```powershell
powershell -ExecutionPolicy Bypass -Command '& Samus\scripts\Deploy-SamusCloudRun.ps1 -SkipBuild -SkipPush'
```

### Deploy specific services
```powershell
powershell -ExecutionPolicy Bypass -Command '& Samus\scripts\Deploy-SamusCloudRun.ps1 -SkipBuild -SkipPush -OnlyServices @("gateway","finance")'
```

### Deploy without workers
```powershell
powershell -ExecutionPolicy Bypass -File Samus\scripts\Deploy-SamusCloudRun.ps1 -SkipWorkers
```

> **Note:** `-OnlyServices` must be a PowerShell array `@("a","b")`, not a comma-separated string, when invoked via `-Command`.

### Constraints
- All services use **gen2** execution environment which requires **>= 512Mi** memory.
- Gateway requires `SAMUS_AUTHZ_MODE=audit` or `enforce` (crashes on `off` in production).
- Tiered deploy order matters: Tier 0 first, then 1, then gateway (2), then intake (3). The script handles this automatically.

---

## 5. Rollback

Cloud Run keeps every revision. Rollback is instant traffic re-pointing.

### Per-service rollback
```bash
# List revisions
gcloud run revisions list --service=samus-gateway --region=us-west1 --project=${GCP_PROJECT}

# Route 100% to a previous revision
gcloud run services update-traffic samus-gateway --region=us-west1 --project=${GCP_PROJECT} \
  --to-revisions=samus-gateway-00001-abc=100
```

### Full stack rollback to local
1. Repoint Stripe webhook back to the ngrok URL
2. Repoint Vapi `serverUrl` back to the local tunnel
3. Restart local Compose stack — local Neo4j is untouched by the GCP deploy

---

## 6. Secrets

18 secrets in GCP Secret Manager. The service account `samus-runtime` has `secretAccessor` role.

| Secret | Used by |
|--------|---------|
| `shared-hmac-key` | All services (inter-service HMAC) |
| `openai-api-key` | prospecting, seo, outreach, strategy, voice, intake + workers (LLM reasoning) |
| `stripe-api-key` | finance |
| `stripe-webhook-secret` | finance |
| `sendgrid-api-key` | finance |
| `sendgrid-from-email` | finance |
| `vapi-api-key` | voice |
| `vapi-webhook-secret` | voice |
| `places-api-key` | prospecting |
| `aws-access-key-id` | All (SQS/DynamoDB) |
| `aws-secret-access-key` | All (SQS/DynamoDB) |
| `samus-ledger-secret-key` | Services using Firestore ledger |
| `gmail-*` (4 secrets) | intake (inbox polling) |
| `hivemind-password` | memory workcell (deferred) |
| `anthropic-api-key` | Deprecated — not referenced |

### Rotate a secret
```bash
# Create new version
echo -n "NEW_VALUE" | gcloud secrets versions add shared-hmac-key --data-file=- --project=${GCP_PROJECT}

# Redeploy affected services to pick up :latest
powershell -ExecutionPolicy Bypass -Command '& Samus\scripts\Deploy-SamusCloudRun.ps1 -SkipBuild -SkipPush -OnlyServices @("gateway")'
```

---

## 7. Storage

| Resource | Purpose |
|----------|---------|
| `gs://${GCP_PROJECT}-samus-data/artifacts` | Artifact storage (proposals, audits, callsheets) |
| Firestore `(default)` DB, us-west1 | Ledger backend + Stripe webhook idempotency |
| `us-west1-docker.pkg.dev/${GCP_PROJECT}/samus` | Docker image registry |

### Inspect artifacts
```bash
gsutil ls gs://${GCP_PROJECT}-samus-data/artifacts/
```

### Inspect Firestore
```bash
gcloud firestore export gs://${GCP_PROJECT}-samus-data/firestore-backup --project=${GCP_PROJECT}
```

---

## 8. Logs and debugging

### Tail logs for a service
```bash
gcloud run services logs tail samus-gateway --region=us-west1 --project=${GCP_PROJECT}
```

### Read recent logs
```bash
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=samus-gateway" \
  --project=${GCP_PROJECT} --limit=50 --format="table(timestamp,textPayload)"
```

### Filter for errors
```bash
gcloud logging read "resource.type=cloud_run_revision AND severity>=ERROR" \
  --project=${GCP_PROJECT} --limit=20 --format="table(timestamp,resource.labels.service_name,textPayload)"
```

### Cloud Console links
- **Services:** https://console.cloud.google.com/run?project=${GCP_PROJECT}
- **Logs:** https://console.cloud.google.com/logs?project=${GCP_PROJECT}
- **Scheduler:** https://console.cloud.google.com/cloudscheduler?project=${GCP_PROJECT}
- **Secrets:** https://console.cloud.google.com/security/secret-manager?project=${GCP_PROJECT}
- **Firestore:** https://console.cloud.google.com/firestore?project=${GCP_PROJECT}

---

## 9. Webhook endpoints

| Provider | Cloud Run URL | Auth |
|----------|---------------|------|
| Stripe | `https://samus-finance-${GCP_PROJECT_NUMBER}.us-west1.run.app/stripe_webhook` | Stripe-Signature header |
| Vapi | `https://samus-voice-${GCP_PROJECT_NUMBER}.us-west1.run.app/vapi/webhook` | Vapi HMAC |
| SES/SendGrid | `https://samus-feedback-${GCP_PROJECT_NUMBER}.us-west1.run.app/ses/webhook` | Provider signature |

**Stripe cutover:** Follow the zero-loss sequence in `docker/CLOUD_RUN_DEPLOY.md` section 6.1 — add a second test endpoint first, verify, then swap the live one.

---

## 10. Scaling

Default scaling is conservative (free-tier friendly):

| Service type | min | max | CPU throttle |
|-------------|-----|-----|-------------|
| Gateway | 1 | 4 | Yes (throttled at idle) |
| Finance, Voice | 1 | 2 | Yes |
| Internal workcells | 0 | 2-4 | Yes |
| Workers | 1 | 1 | No (always-on) |

### Adjust scaling
```bash
gcloud run services update samus-gateway --region=us-west1 --project=${GCP_PROJECT} \
  --min-instances=2 --max-instances=8
```

---

## 11. Cost control

- **Scale-to-zero** services (min=0) incur no cost when idle.
- **Always-on** services (finance, voice, gateway, 9 workers = 12 instances minimum) are the baseline cost.
- **LLM cost** is capped by the CODB scaler: 5% of MRR reinvested, $0.50/day floor, $25/day ceiling (env vars in `$LlmEnv`).
- **Firestore** is on free tier (1 GiB storage, 50K reads/day, 20K writes/day).
- Workers can be paused by setting `min-instances=0`:
  ```bash
  gcloud run services update samus-leadgen-worker --region=us-west1 --project=${GCP_PROJECT} \
    --min-instances=0
  ```

---

## 12. FIM (File Integrity Monitoring)

The immutable baseline (`backend/identity/immutable_baseline.json`) and charter are operator-signed with Ed25519. After any code change to baselined files:

```powershell
cd D:\Hustleforge\Samus

# 1. Regenerate hashes
.venv\Scripts\python.exe scripts\seed_immutable_manifest.py

# 2. Re-sign baseline
.venv\Scripts\python.exe scripts\sign_immutable_manifest.py --target baseline --ed25519 --yes

# 3. Re-sign charter (if changed)
.venv\Scripts\python.exe scripts\sign_immutable_manifest.py --target charter --ed25519 --yes

# 4. Redeploy gateway (where boot-time FIM check runs)
powershell -ExecutionPolicy Bypass -Command '& Samus\scripts\Deploy-SamusCloudRun.ps1 -SkipBuild -SkipPush -OnlyServices @("gateway")'
```

> Must run in the operator's console session (DPAPI key access required).

---

## 13. Deferred items

| Item | Blocker | Action when ready |
|------|---------|-------------------|
| Memory workcell | AuraDB not provisioned (~$65/mo, deferred for budget) | Provision AuraDB Professional (console.neo4j.io, us-west-1), add `neo4j-uri`/`neo4j-user` secrets, uncomment memory in Deploy script, redeploy. Tracked in `codb_registry.yaml` so Samus is aware. |
| `SAMUS_AUTHZ_MODE=enforce` | Audit period | Change gateway env var from `audit` to `enforce` after reviewing authz logs |
| Custom domain | DNS setup | Map `api.hustleforge.tech` via Cloud Run domain mapping |
