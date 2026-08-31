# Samus

[![CI](https://github.com/burna0179-maker/Samus/actions/workflows/ci.yml/badge.svg)](https://github.com/burna0179-maker/Samus/actions/workflows/ci.yml)
[![Tests](https://github.com/burna0179-maker/Samus/actions/workflows/tests.yml/badge.svg)](https://github.com/burna0179-maker/Samus/actions/workflows/tests.yml)
[![Coverage](https://codecov.io/gh/burna0179-maker/Samus/branch/main/graph/badge.svg)](https://codecov.io/gh/burna0179-maker/Samus)
[![Typecheck](https://github.com/burna0179-maker/Samus/actions/workflows/typecheck.yml/badge.svg)](https://github.com/burna0179-maker/Samus/actions/workflows/typecheck.yml)
[![CodeQL](https://github.com/burna0179-maker/Samus/actions/workflows/codeql.yml/badge.svg)](https://github.com/burna0179-maker/Samus/actions/workflows/codeql.yml)
[![Gitleaks](https://github.com/burna0179-maker/Samus/actions/workflows/gitleaks.yml/badge.svg)](https://github.com/burna0179-maker/Samus/actions/workflows/gitleaks.yml)
[![Trivy](https://github.com/burna0179-maker/Samus/actions/workflows/trivy.yml/badge.svg)](https://github.com/burna0179-maker/Samus/actions/workflows/trivy.yml)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/burna0179-maker/Samus/badge)](https://securityscorecards.dev/viewer/?uri=github.com/burna0179-maker/Samus)

[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](https://www.python.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Docker](https://img.shields.io/badge/docker-22%20images-2496ED?logo=docker&logoColor=white)](docker/DEPLOY.md)
[![Docs](https://img.shields.io/badge/docs-REPOSITORY__REVIEW__GUIDE-informational)](REPOSITORY_REVIEW_GUIDE.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## Repository at a glance

| | |
|---|---|
| **FastAPI apps** | 22 workcells (`backend/*/app.py`) |
| **SQS worker sidecars** | 15 standalone workers (`backend/*/worker.py`) |
| **Docker images** | 22 workcell Dockerfiles + shared base (`docker/`) |
| **Test files** | 505 |
| **Test functions** | ~5,617 |
| **Architecture Decision Records** | 8 (`docs/adr/`) |
| **Codex chapters** | 12 (`docs/codex/`) — design contract that precedes the code |
| **Deployment targets** | Docker Compose (dev) · Ubuntu VM · GCP Cloud Run (29 services, us-west1) |
| **Persistence** | AWS DynamoDB · SQS · SES · SNS · Neo4j 5.26 · JSONL ledgers |
| **Version** | v2.2.1 |

---

**Service account:** svc_Samus
**Code root:** `D:\Hustleforge\Samus`
**Data root:** `E:\Hustleforge\Samus`
**Runtime:** Python 3.11 · Docker Compose (host dev / HustleForge-VM / GCP Cloud Run)
**Status:** 22 FastAPI apps + 15 SQS worker sidecars, v2.2.1

Samus is HustleForge's sales engine. Twenty-two FastAPI workcells plus fifteen
SQS worker sidecars run as a Docker Compose project (gateway + crm + prospecting
+ outreach + voice + intake + seo + finance + feedback + memory + strategy +
proposal + scaffold + fulfillment + leadgen + optimizer + entropy + signal_filter
+ path_optimizer + portfolio_controller + template_recovery + governance)
backed by AWS (SQS / DynamoDB / SNS / SES) and Neo4j (Hivemind, on the host).

**Implementation reference:** [ARCHITECTURE.md](ARCHITECTURE.md) — module layout,
capability registry, governance, LLM budget chain, DynamoDB tables, SQS queues,
tests, inbound trust boundaries.

**Release history:** [CHANGELOG.md](CHANGELOG.md) — per-version engineering notes
v1.1.0 through v2.2.1. `ARCHITECTURE.md` is a clean reference; all release
narrative lives in the changelog.

**Ecosystem cross-reference:** [Architecture_Samus.md](Architecture_Samus.md) —
agent-level conventions Samus satisfies regardless of which workcells ship:
heartbeat (Pattern B to Major), audit ledger contract, governance escalation
contract, cross-agent pointers (Major / Darwin / Hivemind / Anita).

---

## Quick start

### Host (Windows, dev)

```powershell
# 1. Load DPAPI secrets into env + start the Compose stack
.\scripts\Start-SamusStack.ps1

# 2. Verify the gateway is up (defaults to host port 8100)
Invoke-WebRequest http://localhost:8100/health

# 3. Stop the stack and scrub secrets from env
.\scripts\Stop-SamusStack.ps1
```

`Start-SamusStack.ps1` pulls every secret (HivemindPassword, SharedHmacKey,
AwsAccessKeyId, AwsSecretAccessKey, plus optional GooglePlacesApiKey,
AnthropicApiKey, StripeApiKey, StripeWebhookSecret, VapiApiKey,
VapiWebhookSecret, SendGridApiKey, NgrokAuthtoken) from the DPAPI store via
`_shared/scripts/Hustleforge.Secrets.psm1`, exports them for the lifetime of
the `docker compose up`, and scrubs them in `finally` before returning.
Missing required secrets abort before any container starts.

### HustleForge-VM (Ubuntu 24.04, 192.168.1.240)

SSH from the operator workstation; same `docker-compose.samus.yml` lives at
`/home/hustleforge/agents/samus/`. Secrets land in `.env` (mode 0600). Caddy
terminates TLS for the three public webhook surfaces (gateway, feedback/SES,
voice/Vapi).

### GCP Cloud Run (`${GCP_PROJECT}`, `us-west1`, AR repo `samus`)

Per-workcell containers publish as `samus-<workcell>-2026`. AWS persistence
remains cross-cloud in `us-west-1`; secrets bind via GCP Secret Manager. See
[`docker/cloudbuild-intake.yaml`](docker/cloudbuild-intake.yaml) for the
single-step bash build pattern that handles AR auth, and
[`scripts/Deploy-SamusCloudRun.ps1`](scripts/) for the operator-side driver.

---

## Security posture

Trust boundaries are enforced at the platform library, not per-workcell.

- **Per-service HMAC identity + caller grants** — `backend/common/security.py`
  + `middleware.VerifyHMACMiddleware`. Every inter-service call carries an
  HMAC-signed `X-Samus-Caller` identity folded into the MAC; a
  `CALLER_GRANTS` matrix (deny-by-default, `SAMUS_AUTHZ_MODE=off|audit|enforce`)
  controls which service can call which.
- **SSRF-safe fetch** — `backend/common/safe_fetch.py`. Rejects non-http(s)
  schemes; blocks private/loopback/link-local/multicast/reserved/CGNAT IPs;
  re-validates every redirect hop. Wired into the SEO crawler and every
  external fetch in the platform.
- **Immutable baseline** — `backend/identity/immutable_manifest.py` +
  Ed25519-signed `backend/identity/immutable_baseline.json`. Drift in the 12
  protected identity/governance files aborts boot when
  `SAMUS_ENV=production`. Legitimate change requires an operator re-sign.
- **Rate limiting** — `backend/common/rate_limit.py` in-process fixed-window
  limiter wired to LLM-backed and outbound-action routes (voice, outreach,
  seo, proposal, finance meter-event).
- **Atomic idempotency** — Stripe webhook `claim_event_id()` uses
  `O_CREAT|O_EXCL` per-event claim files taken before any side effect.
- **Secret handling** — Windows: DPAPI store via `Hustleforge.Secrets.psm1`,
  exported to env for the container lifetime only, scrubbed in a `finally`.
  VM: `.env` (mode 0600). Cloud Run: GCP Secret Manager binding.
- **LLM cost governance** — `backend/common/llm_client.py` +
  `llm_global_budget.py` + `llm_budget.py`. Every LLM call passes a four-layer
  chain: global daily $-cap → model floor → circuit breaker → per-workcell
  token quota. Max one call per job, enforced in the wrapper.

Full security review: [`docs/SECURITY.md`](docs/SECURITY.md). Trust-boundary
documentation and disclosure boundaries: [`docs/SECURITY.md`](docs/SECURITY.md).

---

## Operational scripts (`scripts/`)

Samus's operator surface is PowerShell + Python. The scripts below are the
production interface; every recurring job runs through one of them.

**Stack lifecycle**

| Script | Purpose |
|---|---|
| `Start-SamusStack.ps1` / `Stop-SamusStack.ps1` | DPAPI → env → `docker compose up`/`down`; secret scrub on exit |
| `Deploy-SamusCloudRun.ps1` | Cloud Run per-workcell deploy driver (Artifact Registry auth + Cloud Build submit) |
| `Samus.Secrets.psm1` | per-agent DPAPI secret store (legacy fallback for `_shared` module) |

**Scheduled tasks (Windows Task Scheduler)**

| Script | Cadence |
|---|---|
| `Register-InboxPollSchedule.ps1` | periodic Gmail intake poll |
| `Register-MorningBriefSchedule.ps1` | 08:00 daily operator brief |
| `Register-OutreachDailySchedule.ps1` | daily outreach batch |
| `Register-ProspectingDailySchedule.ps1` | 07:30 daily prospecting run (Yuba → Sac geo-ring state machine) |
| `Register-ProductionHealthSchedule.ps1` | production health probe |
| `Register-CloudSchedulerJobs.ps1` | GCP Cloud Scheduler job registration |
| `Register-SamusMorningCampaign.ps1` | morning outbound campaign |

**Operator daily loop**

| Script | Purpose |
|---|---|
| `Send-Morning.ps1` / `Show-Morning.ps1` | send / display the morning briefing (call list + follow-ups + economics + guidance laws) |
| `Start-MorningDial.ps1` | outbound dial of morning call list via Vapi |
| `Log-Call.ps1` → `backend.crm.log_call` | log a call outcome to CRM |
| `Create-Opportunity.ps1` → `backend.crm.create_opportunity` | create an Opportunity |
| `Open-VoiceConsole.ps1` | open browser-based single-Vapi-call console (`-StartStackIfDown` brings stack up first) |
| `Run-EndOfDayReview.ps1` | end-of-day review |

**Manual runs**

| Script | Purpose |
|---|---|
| `Run-ProspectingDaily.ps1` | one-off prospecting run |
| `Run-OutreachDaily.ps1` | one-off outreach batch |
| `Run-SeoAudit.ps1` / `Run-SeoDelivery.ps1` | one-off SEO audit + delivery |
| `Run-Fulfill.ps1` | manual fulfillment-worker tick |
| `Run-Retainer-Cycles.ps1` | manual retainer-workcell cycle |
| `Run-ProductionHealth.ps1` | health probe on demand |
| `Test-StripeWebhookLocal.ps1` | local Stripe webhook signature smoke |

**Host-side observers**

| File | Purpose |
|---|---|
| `health_monitor.py` + `run_health_monitor.bat` | every-5-min `/health` probe across the stack |
| `Watch-DialRun.ps1` | live dial-run monitor |
| `Analyze-DialBatch.ps1` | dial batch post-mortem |

Disposable one-shots (`_oneshot_*.ps1`) live alongside; delete after use. Full
inventory in [`scripts/README.md`](scripts/README.md).

---

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

505 test files, ~5,617 test functions. Coverage spans every workcell
(`tests/test_<workcell>_*.py`), the shared platform library
(`tests/test_common_*.py`), end-to-end integration walks
(`tests/test_e2e_integration.py`), governance and Codex validation, identity
and boot integrity, and stake-sentence enforcement. Test fixtures isolate env
+ LLM budget + strategy bandit state to per-process tempfiles
(`tests/conftest.py`); no AWS credentials required.

CI (`.github/workflows/ci.yml`) runs ruff lint + format check, the platform
library test subset, and a pip-audit supply-chain check on every push to main
and every PR.

---

## Ecosystem integration

- **Heartbeat → Major (`:8434`)** — Pattern B (HTTP + observable file) per
  [Architecture_Samus.md §5](Architecture_Samus.md#5-heartbeat-pattern-b).
- **Hivemind (`bolt://localhost:7687`)** — Neo4j graph writes via the memory
  workcell; circuit breaker pauses after repeated failures.
- **Darwin governance (`:9000`)** — HIGH/CRITICAL classifications escalate via
  Major; Samus does not call Darwin directly.
- **LLM inference** — primary LM Studio (host, `:1234`); fallback Anthropic
  Messages API via the centralized `backend/common/llm_client.py` wrapper with
  per-workcell adaptive token budgets.

When peer agents are absent (Major / Darwin / Hivemind not running) the
relevant outbound calls fail gracefully and Samus keeps serving traffic.

---

## Documentation

**Reference**

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — module layout, capability registry,
  governance, LLM budget, DynamoDB tables, SQS queues, trust boundaries
- [`CHANGELOG.md`](CHANGELOG.md) — per-version engineering notes v1.1.0 → v2.2.1
- [`docs/DESIGN.md`](docs/DESIGN.md) — design rationale and rejected alternatives
- [`docs/SECURITY.md`](docs/SECURITY.md) — trust model, controls, disclosure boundaries
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — start/stop/rebuild, health checks,
  queue ops, incident triage, cloud deployment checklist
- [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) — setup, branch discipline,
  adding a workcell, tests, lint, PR expectations
- [`docs/CLOUD_OPS_PLAYBOOK.md`](docs/CLOUD_OPS_PLAYBOOK.md) — Cloud Run
  fleet ops (contains infrastructure identifiers; sanitize before public release)
- [`docs/adr/`](docs/adr/) — Architecture Decision Records
- [`docs/codex/`](docs/codex/) — Samus Protocol Codex (design contract that
  precedes the code; `check_action` gate enforces at runtime)

**Engineering judgment**

- [`ENGINEERING_DECISIONS.md`](ENGINEERING_DECISIONS.md) — the ten decisions
  that most shaped the architecture, with rejected alternatives
- [`ARCHITECTURAL_TRADEOFFS.md`](ARCHITECTURAL_TRADEOFFS.md) — tradeoffs with
  operational verdicts
- [`SYSTEM_EVOLUTION.md`](SYSTEM_EVOLUTION.md) — v1.0 → v2.2.1 as an
  engineering narrative
- [`LESSONS_LEARNED.md`](LESSONS_LEARNED.md) — what I got wrong and changed
- [`FAILURE_MODES.md`](FAILURE_MODES.md) — failure catalog: handled, partial, gaps
- [`KNOWN_TECHNICAL_DEBT.md`](KNOWN_TECHNICAL_DEBT.md) — debt inventory with
  severity and remediation cost
- [`SCALABILITY.md`](SCALABILITY.md) / [`PERFORMANCE.md`](PERFORMANCE.md) —
  scaling seams and performance characteristics

---

## `recovery/`

Sixty-plus design artifacts recovered from prior-iteration transcripts. Many
have been ported into live workcells (see
[Architecture_Samus.md §11](Architecture_Samus.md#11-what-s-shipped-vs-still-in-recovery)
for the full ported-from table). Still designed-not-built: the full
cross-worker `ingest_result` feedback loop on top of `fulfillment/dag.py`;
end-to-end orchestration across the 7-module adaptive-agent intelligence
stack; live OAuth wiring for LinkedIn / Facebook social posts; the
ChromaDB-backed vector knowledge layer (optional/lazy, gated behind
`SAMUS_VECTOR_STORE_ENABLED`). Reintroducing any of these registers through
the existing seams (`capabilities.SERVICE_CAPABILITIES`,
`dispatch_policy.register`, `schema_registry.register`,
`governance.register_risk`).

Anything in `recovery/` referenced by the live stack is a bug by design.

---

## Depth layer (v0.3)

v0.3 signed axioms loaded from `axioms/`; EFH evaluator at
`backend/governance/efh_evaluator.py` blocks outreach/CRM commits that breach
inviolable axioms. See [`axioms/inviolable_axioms.yaml`](axioms/inviolable_axioms.yaml)
for the seven ecosystem-scoped axioms and
[`protocol_contract.yaml`](protocol_contract.yaml) for the v0.3.5 AL wire
declaration.
