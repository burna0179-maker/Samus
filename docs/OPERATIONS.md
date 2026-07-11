# Operations Guide

## Purpose

Describe how to configure, start, inspect, stop, recover, and deploy Samus.

## Scope

Operational workflows supported by repository scripts and container definitions. Environment-specific credentials and private infrastructure details are intentionally omitted.

## Intended Audience

Operators, SREs, DevOps engineers, platform engineers, and maintainers.

## Source of Truth

Scripts, Compose files, Dockerfiles, application health routes, and cloud deployment files override this guide.

## Prerequisites

- Windows with PowerShell for the primary operator scripts, or a Linux host capable of running Compose
- Docker Desktop or Docker Engine with Compose
- Python 3.11 for host-side tools and tests
- Provider credentials only for capabilities being exercised
- Access to configured AWS resources when queue-backed or DynamoDB-backed paths are enabled

## Secret Handling

Do not commit:

- `.env`, `.env.local`, or generated Compose environment files;
- API keys or webhook secrets;
- AWS access credentials;
- OAuth tokens;
- graph passwords;
- private service URLs.

The Windows startup wrapper loads secrets into the process environment for the Compose invocation and should scrub them afterward. Validate this behavior before relying on it as a security boundary.

## Local Start

```powershell
Set-Location <repository-root>
.\scripts\Start-SamusStack.ps1
```

The wrapper should:

1. validate required secret references;
2. populate the Compose process environment;
3. start the configured stack;
4. retain logs in the normal Docker logging path;
5. remove transient environment values on exit.

## Health Check

```powershell
Invoke-RestMethod http://127.0.0.1:8100/health
docker compose -f .\docker\compose\docker-compose.samus.yml ps
```

Inspect a specific service:

```powershell
docker logs --tail 200 samus-gateway
docker inspect samus-gateway
```

Do not assume a container is healthy solely because its process is running.

## Stop

```powershell
.\scripts\Stop-SamusStack.ps1
```

For direct Compose control:

```powershell
docker compose -f .\docker\compose\docker-compose.samus.yml down
```

Avoid deleting volumes until data ownership and backup requirements are understood.

## Rebuild

```powershell
docker compose `
  -f .\docker\compose\docker-compose.samus.yml `
  build --no-cache <service>

docker compose `
  -f .\docker\compose\docker-compose.samus.yml `
  up -d --force-recreate <service>
```

Use a scoped service rebuild where possible. Rebuilding the complete stack makes failures harder to isolate.

## Test and Validation

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check backend tests
```

The supplied repository snapshot contains 416 `tests/test_*.py` files and 4,582 directly declared test functions. These are structural counts, not a statement that the suite passed in the current environment.

## Queue Operations

Queue-backed workcells require:

- target queue URLs;
- worker credentials;
- task-state and idempotency storage;
- dead-letter configuration;
- visibility timeouts appropriate for handler duration.

Before enabling a new queue path, verify:

1. HTTP and worker contract parity;
2. idempotency behavior;
3. redelivery safety;
4. poison-message handling;
5. DLQ inspection;
6. replay authorization;
7. task-state observability.

## Data and Ledgers

### DynamoDB

Provision required tables before production use. Local file fallback can keep development moving but should trigger visible warnings.

### JSONL

JSONL ledgers require:

- writable mounted storage;
- rotation and archive policy;
- disk-space monitoring;
- file-permission checks;
- reconciliation after partial failures.

### Neo4j

Verify:

- database availability;
- credentials and TLS expectations;
- schema initialization;
- query allowlists;
- circuit-breaker status;
- projection idempotency.

## Incident Triage

### Service unhealthy

1. Check container state.
2. Read recent logs.
3. Query `/health`.
4. Check dependent provider availability.
5. Confirm required environment keys are present without printing values.
6. Check disk and volume permissions.
7. Inspect circuit breakers and queue backlog.

### Queue backlog rising

1. Confirm workers are polling.
2. Check visibility timeout versus processing duration.
3. Inspect failure counts and DLQ depth.
4. Verify provider rate limits and credentials.
5. Scale workers only after confirming handlers are idempotent.

### Duplicate side effects

1. Stop the affected producer if risk is ongoing.
2. Identify the idempotency key or provider event ID.
3. Inspect task-state and audit ledgers.
4. Reconcile external provider records.
5. Repair the claim-before-side-effect boundary.
6. Add a regression test before replay.

### LLM budget denial

Budget denial should activate deterministic fallback. Inspect:

- global spend;
- workcell quota;
- model restriction;
- circuit state;
- output validation failures.

Do not bypass the budgeter by adding direct provider calls.

## Cloud Deployment

Cloud Run deployment artifacts exist, but readiness is per service.

Review each service for:

- stateless request behavior;
- local-disk assumptions;
- background-thread lifecycle;
- queue-polling suitability;
- request timeout;
- concurrency safety;
- secret binding;
- cross-cloud AWS latency and egress;
- health and startup probes.

Long-running SQS polling may require a different runtime than request-oriented Cloud Run services.

## Backup and Recovery

Minimum recoverable assets:

- configuration templates;
- encrypted or external secret references;
- DynamoDB table definitions and backups;
- graph schema and backups;
- JSONL audit archives;
- immutable manifest signing material;
- customer artifacts subject to retention policy.

A recovery test is stronger evidence than a backup declaration. Record recovery commands and results per deployment.

## Operational Evidence to Capture

For interview and production-readiness review, retain sanitized examples of:

- Compose health output;
- a queue dispatch lifecycle;
- a trace across gateway and worker;
- a DLQ replay;
- a budget-denied deterministic fallback;
- a failed provider call that preserved committed state;
- a container hardening inspection;
- a test run summary.
