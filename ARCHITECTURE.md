# Samus Architecture Reference

**Version:** 2.2.0  
**Runtime:** Python 3.11 · FastAPI · Uvicorn · Docker Compose  
**Platform Services:** AWS (SQS, DynamoDB, SNS) · Neo4j 5.26  
**Deployment Targets:** Windows · Ubuntu · Google Cloud Run

---

# Purpose

This document serves as the technical architecture reference for the Samus platform.

It describes the platform's architectural principles, repository organization, runtime model, deployment topology, shared runtime, business workcells, governance model, observability strategy, and extension mechanisms.

Operational procedures, deployment runbooks, and day-to-day administration are intentionally documented separately to keep this document focused on software architecture.

---

# Design Goals

Samus is designed around a small set of architectural objectives.

- Modular business capabilities with explicit ownership boundaries.
- Shared platform infrastructure rather than duplicated implementation.
- Operational consistency across all services.
- Policy-aware execution and governance.
- Observable system behavior.
- Incremental evolution without large-scale rewrites.
- Support for multiple deployment environments from a common codebase.

These goals guide architectural decisions throughout the platform.

---

# Platform Overview

Samus is a distributed software platform developed by **HustleForge LLC**.

Business capabilities are implemented as independently deployable workcells that execute within a common runtime. Platform services provide shared infrastructure for messaging, configuration, governance, persistence, observability, security, deployment, and operational tooling.

This separation allows business capabilities to evolve independently while maintaining consistent operational behavior across the platform.

---

# Reading This Document

This document is organized progressively.

- **Project Layout** describes the repository structure.
- **Shared Runtime** explains the platform infrastructure.
- **Workcells** describe business capability implementations.
- **Deployment** documents runtime topology.
- **Governance** explains execution policies.
- **Observability** documents monitoring and diagnostics.
- **Release History** records architectural evolution.

Readers interested in understanding the platform should begin with the Project Layout before proceeding into subsystem documentation.

## 1. Project Layout

```
Samus/
  pyproject.toml                # project metadata, deps, ruff + pytest config
  requirements.txt              # pip-installable top-level deps (for local dev / venv)
  ARCHITECTURE.md               # this file
  Architecture_Samus.md         # ecosystem-level cross-ref (port map, auth boundaries)

  backend/
    __init__.py

    common/                     # shared library used by every workcell
      __init__.py
      app_factory.py            # create_base_app() — FastAPI bootstrap
      autonomy.py               # MAPE-K cycle (observe/orient/decide/act)
      aws.py                    # boto3 client factory (SQS / SNS / SES / DDB)
      aws_runtime.py            # AwsRuntime + AwsWorkerSettings for standalone workers
      capabilities.py           # SERVICE_CAPABILITIES registry + check_capability()
      config.py                 # Pydantic Settings, LRU-cached get_settings()
      correlation.py            # ContextVar trace-id + CorrelationMiddleware
      dates.py                  # utc_now, iso_now, hours_from_now
      dlq.py                    # dead-letter queue: enqueue, read, replay, archive
      dynamodb.py               # DynamoDB table ops
      email_backend.py          # adapter selector (sendgrid | ses)
      email_backends/
        sendgrid.py             # SendGrid Mail Send v3
        ses.py                  # (deferred; outreach/ses_adapter.py still authoritative)
      events.py                 # build_audit_event, _deterministic_hash
      governance.py             # classify_risk, approval_decision
      graph_client.py           # Neo4j driver, circuit breaker, write/query ops
      graph_schema.py           # entity schemas, relationships, indexes, query allowlist
      http_client.py            # signed_post_json (HMAC-signed outbound POST)
      idempotency.py            # IdempotencyStore (OrderedDict LRU)
      llm_budget.py             # per-workcell LLM token budget store + adaptive quota
                                #   + circuit breaker (consecutive-error trip + cooldown)
      llm_client.py             # anthropic_messages() canonical Anthropic entry point
                                #   (cache_system + model floor + global $-cap chained)
      llm_global_budget.py      # cross-workcell $/day hard cap (PK="GLOBAL" in samus_llm_budgets)
      llm_pricing.py            # model-aware $/MTok pricing table (Haiku/Sonnet/Opus)
      logging.py                # JsonFormatter, configure_logging
      metrics.py                # Prometheus counters + histograms + LLM gauges
      middleware.py             # MetricsMiddleware, NonceStore, VerifyHMACMiddleware
      models.py                 # TaskEnvelope Pydantic model
      neo4j_runtime.py          # Neo4jRuntime — lazy driver, write_task_lineage()
      persistence.py            # JsonlLedger (append-only JSONL audit file)
      queue_contracts.py        # QueueEnvelope Pydantic model — SQS message contract
      replay_worker.py          # replay_gateway_dlq()
      retry.py                  # CircuitState, retry_request with exp backoff
      security.py               # HMAC sign/verify, nonce generation, safe_compare
      settings.py               # bootstrap_settings() — .env loader, AppSettings
      sqs.py                    # SQS enqueue/poll/delete/parse_body
      sqs_worker.py             # background SQS consumer loop for workcells
      state.py                  # Pluggable StateBackend ABC + LocalStateBackend
      storage.py                # get_service_ledger helper
      text.py                   # slugify, dedupe_preserve
      worker_base.py            # BaseSqsWorker ABC — standalone SQS worker loop

    gateway/                    # edge service, only externally exposed container
      __init__.py
      app.py                    # POST /dispatch/{target}, POST /autonomy/plan,
                                # GET /dlq/{service}, GET /dlq/archive,
                                # GET /admin/llm_budgets
      router.py                 # resolve_target() — target name -> base URL
      service.py                # dispatch_to_target() — HTTP fallback with DLQ
      sqs_dispatch.py           # enqueue_dispatch() — SQS path, builds QueueEnvelope
      queue_app.py              # lightweight FastAPI for queue-backed dispatch

    leadgen/                    # lead scoring + qualification workcell
    prospecting/                # Places API + callsheet (LLM-personalized) workcell
                                #   scorer.py — continuous 4x25-pt lead score
                                #   exclusions.py — government-type + operator
                                #     denylist filter applied at discovery time
                                #   enrichment.py — 3-stage owner-contact cascade
    scaffold/                   # asset generation workcell
    fulfillment/                # execution planning workcell
    memory/                     # k/v + Neo4j graph workcell
    feedback/                   # SES bounce/complaint SNS receiver
    outreach/                   # SES outbound + FSM advance_call / log_outcome
    optimizer/                  # multi-armed bandit / portfolio optimizer
    proposal/                   # proposal generation + validation
    seo/                        # site audit + page optimization + content drafts
                                #   security_audit.py — passive security &
                                #     trust-posture audit (zero-LLM, GET-only)
                                #   report.py — customer SEO report incl. the
                                #     "Security & Trust Posture" section
    finance/                    # Stripe ingest + CODB + runway + payment links
                                #   webhook.py — Stripe webhook (rejects test-mode
                                #     events in production)
                                #   metering.py — per-call-minute Stripe usage
                                #     metering for the AI receptionist
    voice/                      # Vapi outbound caller + inbound webhook receiver
                                #   inbound.py — AI Digital Receptionist
                                #     end-of-call handling for receptionist clients
                                #   inbound_storage.py — per-call recording /
                                #     transcript / voicemail / call.json artifacts
                                #   receptionist_config.py — per-client
                                #     ReceptionistConfig YAML loader
                                #   provision.py — operator CLI: buy + bind a
                                #     Vapi DID to an inbound assistant
                                #   client_summary.py — weekly/monthly call
                                #     digest emailed to receptionist clients
    intake/                     # public onboarding form receiver
                                #   (hustleforge.tech/onboarding -> /intake/onboarding)
                                #   rate_limit.py — DynamoDB per-IP fixed-window
                                #     rate limiter (fails open)
                                #   captcha.py — optional Cloudflare-Turnstile
                                #     CAPTCHA verification (fails closed)
    crm/                        # CRM workcell — owns the 7 customer-pipeline tables
                                #   (prospects, contacts, conversations, call-State,
                                #    opportunities, operator_tasks, artifacts)
                                #   log_call.py — operator CLI: log a hand-dialed
                                #     call (Conversation + CallState)
                                #   create_opportunity.py — operator CLI: open a
                                #     tracked Opportunity for a later-stage deal
    strategy/                   # portfolio + bandit + event-driven triggers workcell
                                #   portfolio_manager.py (UCB1 + hierarchical bandit,
                                #   reward-density-weighted; 1 LLM call per
                                #   propose_allocation()), triggers.py (6 detectors incl.
                                #   predictive forecast_density), optimizer.py +
                                #   capability_marketplace.py + credit_ledger.py +
                                #   trust_scorer.py (all pure-logic, no LLM)
                                #   v1.4.0 reward-density layer (all pure-logic, no LLM):
                                #   reward_density.py + momentum_tracker.py +
                                #   regret_engine.py + saturation_monitor.py +
                                #   predictive_allocator.py + policy_compiler.py
    # --- autonomous workcell expansion (v1.3.0) — all plan_execution, zero LLM ---
    signal_filter/              # prospect pre-qualification gate — enrichment +
                                #   7-axis ProspectSignal + should_enqueue (>=0.62)
    path_optimizer/             # efficiency_ema-driven execution-route selection
    template_recovery/          # deterministic scaffold-fallback library (no LLM)
    portfolio_controller/       # portfolio signals + token-quota/priority rebalance
    entropy/                    # entropy_score engine + countermeasure mapping
    tools/
      preflight.py              # stack verification: env, AWS identity, SQS, DDB, Neo4j

  docker/
    base/
      Dockerfile                # samus-base image (python:3.11-slim, tini)
      entrypoint.sh             # umask, PYTHONPATH, dir creation
      constraints.txt           # pip version pins
    compose/
      docker-compose.samus.yml  # full local stack: 21 HTTP apps + 10 SQS worker sidecars
      .env                      # DPAPI-pre-populated runtime env (gitignored)
      .env.example              # template (safe to track)
    workcells/
      gateway/Dockerfile        # port 8080 (container) -> 8100 (host)
      leadgen/Dockerfile
      prospecting/Dockerfile
      scaffold/Dockerfile
      fulfillment/Dockerfile
      memory/Dockerfile
      ses/Dockerfile            # feedback workcell image
      outreach/Dockerfile
      optimizer/Dockerfile
      proposal/Dockerfile
      seo/Dockerfile
      finance/Dockerfile
      voice/Dockerfile
      intake/Dockerfile

  scripts/                      # host-side operator surface (NOT containerized)
    Start-SamusStack.ps1        # DPAPI -> env, docker compose up, scrub on exit
    Stop-SamusStack.ps1
    Samus.Secrets.psm1          # per-agent secrets module (legacy fallback)
    # --- morning briefing (08:00 daily) ---
    Register-MorningBriefSchedule.ps1
    Send-Morning.ps1            # ships brief to SendGrid + Discord; transcript -> logs/morning_brief/
    Show-Morning.ps1
    # --- prospecting (07:30 daily, v1.2.0) ---
    Run-ProspectingDaily.ps1    # geo-ring state machine wrapping process_discovery
    Register-ProspectingDailySchedule.ps1
    # --- CRM operator surface ---
    Log-Call.ps1                # log a hand-dialed call into the CRM (backend.crm.log_call)
    # --- voice dialer ---
    Start-MorningDial.ps1       # Vapi outbound dialer against today's CSV (DRY-RUN by default)
    # --- one-shot deep SEO audit ---
    Run-SeoAudit.ps1            # any-URL operator wrapper around backend.seo.audit_and_report
    _run_seo_audit.py           # python helper invoked by Run-SeoAudit.ps1
    health_monitor.py

  tests/                        # pytest; 1850+ tests (16s full suite)
    conftest.py                 # env isolation, tmp data dir, LLM budget tmpfile
    test_common_*.py
    test_<workcell>_*.py
    test_e2e_integration.py

  recovery/                     # operator-only specs from recovered transcripts
    vapi_sales_agent_config.md  # Morgan SDR full design
    voice_sales_tactics.md      # 11-section sales playbook
    ...
```

---

## 2. Runtime Overview

Twenty-one FastAPI workcells + ten SQS worker sidecars compose the local stack. Four
of the twenty-one (gateway, feedback, voice, intake) are also reachable from the public
internet through Caddy in production; the rest stay on the internal Docker network.
AWS provides durable state (SQS / DynamoDB / SNS / SES); Neo4j on the host
(`host.docker.internal:7687`) provides the Hivemind graph.

### Two Execution Modes

- **HTTP mode** (no SQS URLs set): gateway sends signed HTTP POST to worker `/work` endpoint.
- **SQS mode** (queue URLs set): gateway enqueues to SQS; standalone worker process polls,
  processes, writes DynamoDB state, publishes SNS events.

Both modes coexist. The gateway checks `QUEUE_URLS[target]`; if set, it uses SQS.
Otherwise, it falls back to direct HTTP dispatch. Worker FastAPI apps keep their HTTP
`/work` endpoints for direct calls and health checks.

### Service Topology

```
                                public internet
                                       |
                       [ Caddy / Cloud Run ingress ]
                          |     |        |        |
                          v     v        v        v
                    gateway feedback   voice    intake        samus-edge
                    :8100   :8090      :8080    :8080         (external)
                       |       |          |        |
                       +-------+----------+--------+
                                     |
                                     v                          samus-internal
              +----+----+----+----+----+----+----+----+----+----+----+----+
              |    |    |    |    |    |    |    |    |    |    |    |    |
           leadgen prospecting scaffold fulfillment memory outreach optimizer
           proposal seo finance voice intake (each :8080 on internal net)
                                     |
                                     v
                              samus-data-init (one-shot chown of volume)

   AWS:
     SQS queues          gateway enqueue + worker poll (9 sidecars)
     DynamoDB tables     samus_task_state, samus_idempotency, samus_suppression,
                         samus_feedback_events, samus_onboarding_leads, samus_llm_budgets
     SNS topic           task lifecycle event fanout

   Neo4j (host):         memory workcell writes to `samus` database via
                         host.docker.internal:7687

   External APIs:        Anthropic (prospecting, seo via llm_client),
                         Google Places (prospecting), Vapi (voice),
                         Stripe (finance), SendGrid (outreach, finance)
```

Each container runs Uvicorn behind tini as non-root user `samus` (UID 10001) in a
read-only filesystem with `no-new-privileges`, all Linux capabilities dropped, a
pids limit, and CPU/memory caps. All services use `restart: unless-stopped`.

### Standalone SQS Workers

Each workcell with a `worker.py` ships a sidecar container that pulls the same image
as its HTTP-app counterpart but runs `python -m backend.<svc>.worker` instead of
uvicorn. The worker:

1. Reads `AwsWorkerSettings.from_env()` to get queue URL, table names, SNS ARN.
2. Instantiates `AwsRuntime` (SQS, DynamoDB, SNS clients).
3. Runs `BaseSqsWorker.run_forever()` as a blocking synchronous poll loop.
4. For each message: validates via `QueueEnvelope`, checks idempotency, calls
   `handle()`, updates task state, publishes SNS event, deletes message on success.
5. Poison messages (malformed JSON) are deleted immediately to prevent infinite loops.
6. Failed messages are left in the queue for SQS retry / DLQ policy.

Workcells with queue sidecars (10): crm, leadgen, prospecting, scaffold, fulfillment,
feedback, outreach, optimizer, proposal, seo. Workcells that are HTTP-only: gateway,
memory, finance, voice, intake, strategy (no `samus-<svc>-jobs` queue provisioned —
`worker.py`, where present, raises `NotImplementedError` until a queue lands).

### Data-Volume Initialization

Named docker volumes are created root-owned with 0755 perms by default; all workcells
run as non-root `samus` (UID 10001) and cannot mkdir under `/opt/samus/data` on a
fresh volume. The `samus-data-init` one-shot container runs at compose-up under
root, chowns the mount, and exits. Every other service `depends_on` it so they start
only after the volume is samus-writable. This eliminates the silent "audit ledger
warning" failure mode that used to hit on first boot.

### Boot Lifecycle (`Start-SamusStack.ps1`)

PowerShell-only on Windows. Pulls every secret from DPAPI (Windows credential store)
via `_shared/scripts/Hustleforge.Secrets.psm1`, exports each into the child docker
compose process's environment for the lifetime of the up command, scrubs them in
`finally` before the script returns.

```
Required: HivemindPassword, SharedHmacKey, AwsAccessKeyId, AwsSecretAccessKey
Optional: GooglePlacesApiKey, AnthropicApiKey, StripeApiKey, StripeWebhookSecret,
          VapiApiKey, VapiWebhookSecret, SendGridApiKey, NgrokAuthtoken
```

Missing required secrets abort before any container starts. Missing optional secrets
log a single grey line and the affected workcell degrades gracefully at runtime
(e.g. `vapi_error=vapi_api_key_unset`, `stripe_error=stripe_api_key_unset`).

### Multi-Target Deployment

- **Host dev** — docker compose on Windows operator workstation.
- **HustleForge-VM** (Ubuntu 24.04, `192.168.1.240`) — same compose stack at
  `/home/hustleforge/agents/samus/` for LAN-reachable testing.
- **GCP Cloud Run** (project `samus-2026`, region `us-west1`) — individual workcells
  published as `samus-<workcell>-2026` services for public ingress. AWS persistence
  remains cross-cloud (SQS/DDB/SNS in us-west-1); secrets loaded from GCP Secret
  Manager via env-var binding.

---

## 3. Shared Library (`backend/common/`)

### 3.1 App Factory (`app_factory.py`)

`create_base_app()` returns a FastAPI instance with:

1. `CorrelationMiddleware` — propagates or generates `X-Samus-Trace-Id` via ContextVar.
2. `MetricsMiddleware` — records `samus_http_requests_total` (Counter) and
   `samus_http_request_duration_seconds` (Histogram) per service/method/path/status.
3. `VerifyHMACMiddleware` — enforces HMAC-SHA256 on every inbound request except
   `/health` and `/metrics`. The gateway service itself is exempted (it is the signer).
   Validates timestamp freshness (configurable window, default 300 s), nonce uniqueness
   (in-process NonceStore, 10000-item ring buffer), and signature via constant-time
   `hmac.compare_digest`.
4. Built-in `GET /health` and `GET /metrics` (Prometheus exposition format).
5. Logging configured once at startup via `configure_logging()` — structured JSON to
   stdout, including `trace_id` from the ContextVar.

### 3.2 Configuration (`config.py`)

`Settings` is a Pydantic `BaseModel` whose fields read from environment variables at
construction time via `bootstrap_settings()` in `settings.py`. `get_settings()` is
LRU-cached (maxsize=1); `reload_settings()` drops the cache for tests + secret rotation.

Selected fields (full list in `config.py`):

| Env var                       | Field                              | Default                       |
|-------------------------------|------------------------------------|-------------------------------|
| `SAMUS_SERVICE`               | `service_name`                     | `"unknown"`                   |
| `SAMUS_SHARED_HMAC_KEY`       | `shared_hmac_key`                  | `""`                          |
| `AWS_REGION`                  | `aws_region`                       | `"us-west-1"`                 |
| `DDB_TASK_STATE_TABLE`        | `ddb_task_state_table`             | `"samus_task_state"`          |
| `DDB_IDEMPOTENCY_TABLE`       | `ddb_idempotency_table`            | `"samus_idempotency"`         |
| `DDB_SUPPRESSION_TABLE`       | `ddb_suppression_table`            | `"samus_suppression"`         |
| `DDB_FEEDBACK_TABLE`          | `ddb_feedback_table`               | `"samus_feedback_events"`     |
| `DDB_ONBOARDING_LEADS_TABLE`  | `ddb_onboarding_leads_table`       | `"samus_onboarding_leads"`    |
| `DDB_LLM_BUDGETS_TABLE`       | `ddb_llm_budgets_table`            | `"samus_llm_budgets"`         |
| `NEO4J_URI`                   | `neo4j_uri`                        | `"bolt://localhost:7687"`     |
| `NEO4J_DATABASE`              | `neo4j_database`                   | `"samus"`                     |
| `GOOGLE_PLACES_API_KEY`       | `google_places_api_key`            | `""`                          |
| `ANTHROPIC_API_KEY`           | `anthropic_api_key`                | `""`                          |
| `STRIPE_API_KEY`              | `stripe_api_key`                   | `""`                          |
| `STRIPE_WEBHOOK_SECRET`       | `stripe_webhook_secret`            | `""`                          |
| `VAPI_API_KEY`                | `vapi_api_key`                     | `""`                          |
| `VAPI_WEBHOOK_SECRET`         | `vapi_webhook_secret`              | `""`                          |
| `VAPI_ASSISTANT_ID`           | `vapi_assistant_id`                | `""`                          |
| `VAPI_PHONE_NUMBER_ID`        | `vapi_phone_number_id`             | `""`                          |
| `NGROK_AUTHTOKEN`             | `ngrok_authtoken`                  | `""`                          |
| `NGROK_RESERVED_DOMAIN`       | `ngrok_reserved_domain`            | `""`                          |
| `EMAIL_BACKEND`               | `email_backend`                    | `"sendgrid"`                  |
| `SENDGRID_API_KEY`            | `sendgrid_api_key`                 | `""`                          |
| `SAMUS_AUTO_FULFILL_OFFERS`   | `auto_fulfill_offer_codes` (list)  | `[]`                          |
| `SAMUS_INTAKE_ALLOWED_ORIGINS`| `intake_allowed_origins` (list)    | `["https://hustleforge.tech"]`|
| `SAMUS_INTAKE_DEDUP_WINDOW_S` | `intake_dedup_window_seconds`      | `86400`                       |
| `SAMUS_LLM_BASE_TOKENS`       | `llm_workcell_base_token_budget`   | `100_000`                     |
| `SAMUS_LLM_BUDGET_EMA_ALPHA`  | `llm_budget_ema_alpha`             | `0.05`                        |
| `SAMUS_LLM_BUDGET_FLOOR_PCT`  | `llm_budget_floor_pct`             | `0.10`                        |
| `SAMUS_LLM_GLOBAL_DAILY_DOLLAR_CAP` | `llm_global_daily_dollar_cap` | `1.0` (operator-chosen production cap; see §5) |
| `SAMUS_LLM_CHEAP_MODEL_PATTERN`     | `llm_cheap_model_pattern`     | `^claude-haiku-`              |
| `SAMUS_LLM_CIRCUIT_BREAKER_THRESHOLD` | `llm_circuit_breaker_threshold` | `10`                       |
| `SAMUS_LLM_CIRCUIT_BREAKER_COOLDOWN_SEC` | `llm_circuit_breaker_cooldown_sec` | `300`                |
| `SAMUS_LLM_GLOBAL_BUDGET_PATH`| (json fallback path)               | `/opt/samus/data/llm_global_budget.json` |
| `SAMUS_PORTFOLIO_DOLLAR_CAP`  | `portfolio_workcell_dollar_cap`    | `0.20` (informational slice of the $1/day cap) |
| `SAMUS_PORTFOLIO_MIN_SIGNAL_CHANGE` | `portfolio_min_signal_change` | `0.15` (15% EV step threshold) |
| `SAMUS_PORTFOLIO_TICK_INTERVAL_SEC` | `portfolio_tick_interval_sec` | `900` (15 min)                 |
| `SAMUS_PORTFOLIO_SNAPSHOT_PATH` | (json fallback path)             | `/opt/samus/data/strategy/portfolio_snapshots.json` |
| `SAMUS_PORTFOLIO_TRIGGER_LEDGER` | (audit jsonl path)              | `/opt/samus/data/strategy/portfolio_triggers.jsonl` |
| `SAMUS_YT_MIN_TRANSCRIPT_CHARS` | (env-only gate threshold)        | `1000` (top-N gate; tests dial to `1`) |
| `SAMUS_SEO_SECURITY_AUDIT_ENABLED` | `seo_security_audit_enabled`  | `True` (passive SEO security audit; see §4.4) |
| `SAMUS_INTAKE_RATE_LIMIT_ENABLED` | `intake_rate_limit_enabled`    | `True` (public-intake per-IP rate limiter; §6.4) |
| `SAMUS_INTAKE_RATE_LIMIT_PER_MINUTE` | `intake_rate_limit_per_minute` | `5` per source IP          |
| `SAMUS_INTAKE_RATE_LIMIT_PER_HOUR` | `intake_rate_limit_per_hour`  | `30` per source IP            |
| `SAMUS_INTAKE_RATE_LIMIT_GLOBAL_PER_HOUR` | `intake_rate_limit_global_per_hour` | `600` cross-IP backstop |
| `SAMUS_INTAKE_CAPTCHA_SECRET` | `intake_captcha_secret`            | `""` (empty → CAPTCHA dormant) |
| `SAMUS_INTAKE_TRUSTED_PROXY_HOPS` | `intake_trusted_proxy_hops`    | `1` (XFF entries from the right that are trusted) |
| `SAMUS_STRIPE_REJECT_TEST_MODE` | `stripe_reject_test_mode`        | `True` (drop livemode=false events in prod; §4.8) |
| `<WORKCELL>_URL`              | `gateway_urls[workcell]`           | per-service Docker DNS name   |
| `SQS_<WORKCELL>_QUEUE_URL`    | `sqs_queue_urls[workcell]`         | `""` per workcell             |

### 3.3 Capability Enforcement (`capabilities.py`)

Static registry mapping each service name to its allowed capabilities:

```python
SERVICE_CAPABILITIES = {
    "gateway":     {"dispatch", "dlq_read", "autonomy_plan", "budget_admin"},
    "leadgen":     {"score", "qualify"},
    "prospecting": {"discover", "build_call_sheet"},
    "scaffold":    {"generate_assets"},
    "fulfillment": {"plan_execution"},
    "memory":      {"read", "write", "query", "delete", "stats", "graph", "customers"},
    "feedback":    {"ingest"},
    "outreach":    {"advance_call", "log_outcome", "send_message"},
    "optimizer":   {"select_arm", "update_arm", "optimize_portfolio"},
    "proposal":    {"generate_proposal", "validate_proposal"},
    "seo":         {"audit_site", "optimize_page", "generate_content",
                    "audit_and_report"},
    "finance":     {"snapshot", "codb_summary", "runway", "liabilities", "declines",
                    "debts", "actions", "info_gaps", "hardship", "payment_links",
                    "stripe_webhook", "recent_payments"},
    "voice":       {"initiate_call", "fetch_call", "list_calls", "handle_webhook"},
    "intake":      {"submit_lead", "list_leads"},
    "crm":         {"read_prospects", "read_contacts", "read_conversations",
                    "read_call_state", "read_opportunities", "read_tasks",
                    "read_artifacts", "convert_lead", ...},  # CRM set has
                    # expanded post-Phase-6 (write_conversation, write_call_state,
                    # write_task, write_opportunity, advance_opportunity,
                    # write_artifact, find_opportunity_for_email, log_feedback,
                    # auto_create_opportunity, etc.); see capabilities.py.
    "strategy":    {"evaluate", "dispatch_strategy_action", "record_outcome",
                    "build_context", "rank_portfolio", "propose_allocation",
                    "update_bandit_arm"},
}
```

`check_capability(service, capability)` raises `HTTPException(403)` with
`detail="capability denied: {capability}"` when not in the allowed set.

**Universal enforcement**: every endpoint in every workcell's `app.py` calls
`check_capability()` at the top before any business logic.

### 3.4 Governance (`governance.py`)

Risk classification operates on three term sets matched against the lowercased
combination of objective + requested actions: `critical` (irreversible/financial/legal),
`high` (production/migration/bulk-messaging), and external-action terms that also
escalate to `high`. `approval_decision(objective, actions, approvals)` wraps
`classify_risk` and checks explicit approvals; critical requires
`["owner", "governance"]`, high requires `["owner"]`, normal requires nothing.

### 3.5 Autonomy Engine (`autonomy.py`)

MAPE-K loop (Monitor-Analyze-Plan-Execute with Knowledge):

1. **Observe** — captures `task_id`, `objective`, `inputs` with an ISO timestamp.
2. **Orient** — keyword-matches the objective against workcell-affinity terms.
3. **Decide** — builds an ordered `Plan` of `PlanStep` entries.
4. **Act** — serializes the plan into a result dict.

### 3.6 Dead-Letter Queue (`dlq.py`)

Per-service JSONL ledgers under `/opt/samus/data/dlq/`. `enqueue_failure`,
`read_pending`, `mark_replayed`, `read_archive`. Archive is the single
`replayed_archive.jsonl`.

### 3.7 Replay Worker (`replay_worker.py`)

`replay_gateway_dlq(limit=25)` re-attempts pending gateway DLQ items via
`signed_post_json` to the target's `/work`, with idempotency keyed by
`replay:{event_id}`. Marks each item `replayed`, `skipped`, or `replay_failed`.

### 3.8 Inter-Service HTTP Client (`http_client.py`)

`signed_post_json(base_url, path, payload)` — async httpx POST signed with the shared
HMAC key + nonce + timestamp, wrapped in `retry.retry_request` for circuit-aware
backoff. Trace ID propagated via header. Every internal cross-workcell call goes
through this; no workcell calls another directly.

### 3.9 LLM Budget Store (`llm_budget.py`)

Per-workcell daily token budget with EMA-smoothed adaptive sizing. See
[Section 5](#5-llm-budget-system) for the full design.

### 3.10 LLM Client Wrapper (`llm_client.py`)

`anthropic_messages(*, workcell, api_key, prompt, system=None, cache_system=False,
allow_expensive_model=False, ...)` — canonical Anthropic Messages entry point.
Chains Layer A (`llm_global_budget.can_spend()` $-cap) → Layer B (model floor regex)
→ Layer C (circuit breaker) → Layer 0 (`llm_budget.can_spend()` per-workcell tokens)
before any HTTP call. Post-flight `record_spend()` writes both the per-workcell row
and the GLOBAL $-row. Raises `BudgetExceeded` (quota / cap spent),
`ModelNotPermitted` (Layer B), `CircuitOpen` (Layer C), or `LlmCallError`
(transport/parse). Errors are recorded as `outcome=error` and don't punish the
workcell's EMA. `record_outcome(workcell, outcome=...)` lets callers flip the
auto-recorded success to failure after their own validation. `cache_system=True`
wraps the system prompt in an ephemeral `cache_control` block (Layer D / §5.4) —
warm hits within the 5-minute window read at ~10% input cost.

### 3.11 Metrics (`metrics.py`)

Prometheus `CollectorRegistry` with HTTP request counter + histogram, task outcome
counter, plus the LLM token gauges/counters described in [Section 5](#5-llm-budget-system).
`metrics_response()` returns the Prometheus text-format Response for `/metrics`.

---

## 4. Workcell Reference

Each workcell follows the same shape: `app.py` (FastAPI), `service.py` (business
logic), `models.py` (Pydantic), optional `worker.py` (SQS sidecar). Every workcell
registers its capabilities in `common/capabilities.py` and exposes `/work` for
TaskEnvelope parity with the gateway.

### 4.1 Gateway (`backend/gateway/`)

Edge service. Only path the operator hits directly. Routes to a workcell either by
SQS enqueue (if `SQS_<WORKCELL>_QUEUE_URL` is set) or HTTP fallback to the workcell's
internal `<WORKCELL>_URL`.

Endpoints:
- `POST /dispatch/{target}` — capability `dispatch`. Validates envelope, routes via SQS or HTTP.
- `POST /autonomy/plan` — capability `autonomy_plan`. Risk-classified MAPE-K cycle.
- `GET /dlq/{service}` — capability `dlq_read`. Pending failures for a service.
- `GET /dlq/archive` — capability `dlq_read`. Replay history.
- `GET /admin/llm_budgets` — capability `budget_admin`. Per-workcell LLM budget snapshot.

### 4.2 Prospecting (`backend/prospecting/`)

Discovers prospects via Google Places, scores + classifies, enriches with
owner-contact signals, optionally runs a deep SEO audit for warm/hot prospects,
templates callsheets, and emits the daily CSV + human-readable TXT call list.
Callsheets feed the morning dialer in voice; the TXT is attached to the 08:00
morning brief email.

**Pipeline stages** in `service.process_discovery` (executed per
`DiscoveryRequest`):

1. **Discover** — for each `(zip, industry)` pair (NOT zip-only — the
   single-pass-industries iteration was the cause of the 90%-real-estate
   collapse before v1.2.0), call `discover_for_zipcode` with
   `max_per_industry = max(3, max_results_per_zip // len(industries))`. A
   process-global `seen_prospect_ids` set deduplicates by Google `place_id`
   across overlapping zip search radii. `discover_for_zipcode` runs each
   candidate through `exclusions.exclusion_reason()` and drops government /
   public-sector offices (by Places `types`) plus operator-denylisted orgs
   (`EXCLUDED_DOMAINS` / `EXCLUDED_NAME_SUBSTRINGS`) — excluded prospects are
   logged and skipped before the per-zip cap (v1.5.2; see `exclusions.py`).

2. **SEO score + owner enrichment** (Step 2a — shared homepage fetch) — this
   pass now runs **before** lead scoring (it was after, pre-v1.6.1) so the
   scorer's SEO-opportunity component sees a real `seo_score`. When
   `enable_seo_audit` and/or `enable_owner_enrichment` are True (default
   both), `crawler.fetch_homepage` runs once per prospect; `classify_website`
   records reachability onto `ProspectRecord.website_status` (a prospect with
   no `website_url` short-circuits to `website_status="no_website"`). The page
   dict is passed to `seo_audit.score_seo` (5-check heuristic, 0-100) AND
   `enrichment.enrich_from_page_with_fallback`. Enrichment uses a three-stage
   cascade with early-exit on the first stage that yields `owner_email`:
     a) Homepage HTML — regex emails, social URLs, JSON-LD Person, author meta
     b) `/contact` + `/about` on same domain — same extractors
     c) `mbasic.facebook.com/{handle}/about` — fragile by design; FB blocks
        unauthenticated bots aggressively, so failures degrade to empty signals
   Toggle: `enable_facebook_enrichment` (default True).

3. **Score + priority** (Step 2b) — runs after the Step 2a SEO pass so
   `score_prospect` reads the populated `seo_score`. `score_prospect` returns a
   continuous 0-100 score that is the sum of four equally-weighted 25-point
   components: industry fit (`INDUSTRY_WEIGHTS` capped at 25 — 7 Yuba-market
   verticals plus legacy categories), review rating (`(rating-3.0)/2.0`, so
   3.0★ → 0, 5.0★ → 25), review volume (log-scaled, saturating at 500 reviews
   so early reviews carry real weight), and SEO opportunity (the *inverse* of
   `seo_score` — worse SEO = better lead, since HustleForge sells SEO; an
   unreachable/absent site reads as max opportunity by design).
   `classify_priority` buckets to `hot`/`warm`/`low` at the recalibrated
   thresholds **70 / 45** (the old 75/50 cuts were unreachable under the
   pre-v1.6.1 step-function formula that topped out at 52).

4. **Full audit + report for warm/hot** (Step 2.7, `enable_full_audit_for_warm`,
   default True) — for every prospect with `call_priority != "low"`, fire
   `backend.seo.service.audit_and_report` (deep `bs4` audit + recommendations
   + LLM content drafts). Writes `<artifact_root>/customers/<slug>/seo_report.md`
   and stores the path on `ProspectRecord.seo_report_path`. The same warm/hot
   audit also runs the passive security audit (§4.4); its A-F grade is lifted
   from `AuditResult.findings["security"]["grade"]` onto
   `ProspectRecord.security_grade` (empty when the security audit is disabled
   or failed). Cold prospects skip Step 2.7 to keep LLM spend under the $1/day
   cap — typical Yuba run produces ~5-15 warm/hot prospects out of ~48 total,
   well under budget.

5. **Callsheet** (Step 3) — `build_call_sheet` templates the per-prospect
   WHY-WE-CALLED bullets, OPENER/VOICEMAIL/OBJECTIONS scripts. Optional LLM
   personalization via `build_call_sheet_smart()` (Top-N gate below).

6. **CSV + TXT export** (Step 4) — `csv_export.write_call_list` writes the
   34-column canonical CSV (`CSV_COLUMNS`, now including `website_status`,
   `seo_score` and `security_grade`); `text_export.write_morning_call_list`
   writes the operator-readable TXT (surfaces `📄 Full audit: <path>` line for
   prospects with `seo_report_path` populated). The call-list sort in
   `text_export._sort_key` is `call_priority` → `lead_score` descending →
   **`security_grade`** (worse grade ranks higher — more trust problems to
   pitch a fix for, ungraded prospects last) → `seo_score`.

**Top-N gate** (Lever 2.2): `build_call_sheet_smart()` only fires the LLM when
`call_priority == "hot"` AND `lead_score >= 75`; lower-tier prospects always take
the templated path. Drops LLM fires to ~15-25% of the cohort, keeping daily spend
under the production $1/day cap. The pre-extracted system prompt
(`_CALLSHEET_INSTRUCTIONS`) rides in the cached prefix via `cache_system=True`
(Lever 1.3); only the variable `{prospect json}` payload counts as fresh input.
`max_tokens=350` (Lever 1.2).

Adaptive layer: `dynamic_script.py` (commit 38b9c35) consumes `intelligence.py`
output (pitch_angle, products, signals) and substitutes from hardcoded lookup
tables — bandit-style template selection without an LLM call.

**Operator surface** (host-side, NOT containerized):
- `scripts/Run-ProspectingDaily.ps1` — geo-ring state machine. Reads
  `<artifact_root>/prospecting_geo_state.json`, builds the cumulative zipcode
  set for the active ring (Yuba core → Sutter/Yuba outer → Placer Lincoln/Auburn
  → Roseville → Folsom/Elk Grove → Sacramento metro), fires the pipeline with
  DPAPI-loaded `GOOGLE_PLACES_API_KEY`, `ANTHROPIC_API_KEY`, and
  `GOOGLE_PAGESPEED_API_KEY` (last two optional). `-AdvanceRing` manually
  promotes the active ring (no auto-advance — operator decides "local pool is
  exhausted"). Transcript logs to `logs/prospecting_daily/`.
- `scripts/Register-ProspectingDailySchedule.ps1` — Windows scheduled task
  `Samus Prospecting Daily` at 07:30 (30 min before the 08:00 morning brief).
- `scripts/Run-SeoAudit.ps1` — one-shot deep audit of any URL, same
  `audit_and_report` pipeline, output to `<artifact_root>/customers/<slug>/`.

### 4.3 Leadgen (`backend/leadgen/`)

Scores inbound leads against the leadgen rubric and classifies them into segments.
Also serves as the `/dispatch/leadgen` target for live scoring during voice calls
(Morgan SDR Node 4 in `recovery/vapi_sales_agent_config.md`).

### 4.4 Scaffold, Fulfillment, Proposal, Optimizer, SEO

Long-running content-generating workcells. Each ships HTTP app + SQS worker sidecar.
SEO uses `llm_client.anthropic_messages` for the `generate_content` capability
(templated fallback when budget denies or model fails).

**SEO top-N gate** (Lever 2.2): `seo.service.generate_content` only fires the LLM
when `len(target_keywords) >= 2` AND `len(optimization_data.on_page_changes) >= 1`;
otherwise the templated path drafts the content. `_SEO_INSTRUCTIONS` rides in the
cached system prefix (Lever 1.3) with `max_tokens=900` (Lever 1.2).

**SEO passive security audit** (`backend/seo/security_audit.py`, v1.6.0): a
strictly passive, non-exploitative, zero-LLM security & trust-posture review
that runs as an enrichment step inside `audit.audit_url`. It reuses the
homepage response (headers + html + http-resource list) the SEO audit already
fetched and adds at most ~6 benign HTTP GETs, one TLS handshake, and DNS
lookups — no auth attempts, no injection, no POST, no exploitation; the
passive contract is documented in the module docstring. `audit_security(url,
headers=, html=, http_resources=)` returns `{"grade", "findings",
"probe_requests_used", "checks_run"}`; `audit_url` attaches it to
`AuditResult.findings["security"]` (no `AuditResult` schema change). Six check
families: (1) HTTP security headers — CSP, HSTS, clickjacking
(`X-Frame-Options`/CSP `frame-ancestors`), `X-Content-Type-Options`,
`Referrer-Policy`, `Permissions-Policy`; (2) TLS certificate — issuer +
expiry + days-remaining via stdlib `ssl`/`socket`, warns under 21 days; (3)
email-authentication DNS — SPF/DMARC/DKIM/CAA via `dnspython`; (4) WordPress
platform exposure — REST user enumeration (`/wp-json/wp/v2/users`), `/xmlrpc.php`,
`/readme.html`; (5) exposed artifacts — `/.well-known/security.txt`,
`/.git/config`; (6) cookie flags + mixed content from data already in hand.
Each emits a `SecurityFinding` (`id`/`severity`/`category`/`title`/`evidence`/
`risk`/`remediation`); `severity` adds an `info` tier. The A-F `Security Grade`
is deterministic (100 minus severity penalties: critical 40 / high 20 /
medium 8 / low 3 / info 0). Every network/DNS/TLS failure degrades to a clear
finding and never raises out of the SEO audit. Gated by
`Settings.seo_security_audit_enabled` (env `SAMUS_SEO_SECURITY_AUDIT_ENABLED`,
default True); when False the audit is skipped and the report section omitted.
`report._render_security_posture` renders the customer-facing "Security &
Trust Posture" section (grade + plain-English blurb + findings grouped
worst-first + `- [ ]` remediation checklists) after Recommendations and before
Content Drafts. The warm/hot prospecting full audit (§4.2 Step 2.7) lifts the
resulting grade onto `ProspectRecord.security_grade`.

### 4.5 Memory (`backend/memory/`)

Two-surface workcell: ephemeral k/v (in-process store with TTL, prefix query,
pagination) plus Neo4j graph (write_node, write_relationship, query). Voice
end-of-call dispatches structured lead summaries here at namespace `voice.calls`.

### 4.6 Feedback (`backend/feedback/`)

Public SES bounce/complaint receiver. Strict SNS X.509 signature verification
(`sns_signature.py`) runs before any business logic. Writes suppression rows to
`samus_suppression` and audit rows to `samus_feedback_events`.

### 4.7 Outreach (`backend/outreach/`)

SES email send via `common/email_backend.py` (sendgrid | ses adapter selector).
FSM for advance_call/log_outcome capabilities. Vapi-channel `send_message` deferred
to the voice workcell.

**Outreach → daily-calls follow-up cadence** (v1.5.0): on email-channel
success, `outreach.service.send_message` best-effort dispatches the touch into
`samus-crm` via `signed_post_json_sync` (`_dispatch_outreach_to_crm`) so a sent
outreach enters the operator's follow-up call cadence. It posts two records:
a Conversation (`outcome="outreach_sent"`, with `company`/`phone`/`subject` in
`structured_data`) and a CallState upsert (`state="outreach_sent"`,
`next_attempt_at` = send date + `_FOLLOW_UP_DELAY_DAYS` = 2). The dispatch is
fully guarded — a missing CRM URL / HMAC key, or any HTTP failure, logs a
warning and never fails the outreach send. `OutreachMessageRequest` carries
optional `company`/`phone` so the Conversation has the contact detail the
operator needs to dial. The morning brief reads the resulting CallState rows
via `crm.service.list_follow_ups_due` (§4.10 / morning brief below).

### 4.8 Finance (`backend/finance/`)

Read-only Stripe ingest (balance, charges, payouts, subscriptions) + CODB registry
(YAML) + runway math. `samus_auto_fulfill_offers` whitelist triggers automatic
post-payment fulfillment when a payment-link sale also supplies the customer's
website URL via Stripe `custom_fields`. Stripe webhook receiver (HMAC-verified via
`stripe_webhook_secret`) for `checkout.session.completed`. Outbound SendGrid for
payment receipts.

**Test-mode rejection in production** (intake-hardening, v1.6.x):
`finance.webhook.handle_stripe_webhook` rejects test-mode events
(`livemode=false`) when the runtime env is production — gated on
`Settings.is_production` AND `SAMUS_STRIPE_REJECT_TEST_MODE` (default True). In
prod a test-mode `checkout.session.completed` no longer advances a customer to
`paid` or sends a receipt; the gate is inert in non-production env so test
fixtures (which use `livemode=False`) still process normally.

**Usage metering** (`backend/finance/metering.py`): per-call-minute usage
metering for the AI Digital Receptionist. The receptionist is billed flat base
+ per-call-minute overage; the metered half is a Stripe `usage_type=metered`
price bound to a Meter. After each inbound call the voice workcell calls
`report_call_minutes`, which pushes a meter event (minutes rounded UP, telecom
convention) keyed on the `call_id` as the meter event `identifier` so a webhook
redelivery is idempotent and cannot double-bill. `report_call_minutes` NEVER
raises — a metering failure must not fail the inbound webhook — and every
outcome (success or failure) is appended to `meter_events.jsonl` for operator
reconciliation / manual replay.

### 4.9 Voice (`backend/voice/`)

Outbound Vapi caller (Morgan SDR loop) + inbound webhook receiver + the AI
Digital Receptionist.

Endpoints:
- `POST /voice/call` — `initiate_call`. Operator-driven outbound. `vapi_api_key`
  unset → `vapi_error=vapi_api_key_unset` (graceful).
- `GET /voice/call/{id}` — `fetch_call`. Single-call status.
- `GET /voice/calls` — `list_calls`. Recent calls.
- `POST /vapi/webhook` — `handle_webhook`. HMAC-SHA256 signature verified against
  `vapi_webhook_secret` (`x-vapi-signature` header). `SAMUS_VOICE_VERIFY_WEBHOOK=0`
  opts out for tests; production refuses with 503 if secret unset (no degradation —
  inbound trust boundary). `handle_webhook_event` detects the call direction:
  outbound end-of-call events extract `structuredData.lead_summary` and dispatch
  to `samus-memory` at `voice.calls/<call_id>`; inbound end-of-call events are
  forked to the AI Digital Receptionist path below.

**AI Digital Receptionist** (`inbound.py`, `inbound_storage.py`,
`receptionist_config.py`, `provision.py`, `client_summary.py`): inbound-call
handling for receptionist *client* businesses — when a caller rings a client's
DID, Vapi answers with that client's inbound assistant (greeting, FAQ,
appointment / callback capture, voicemail, transfer) and POSTs an
`end-of-call-report`. `receptionist_config.py` resolves the owning client from
the called number via a per-client `ReceptionistConfig` YAML at
`<artifact_root>/customers/<slug>/receptionist/config.yaml` (file-backed,
short-TTL cached on the inbound hot path). `inbound.py` then extracts the
assistant's structured `InboundSummary`, persists recording / transcript /
voicemail + a normalized `InboundCallRecord` via `inbound_storage.py`
(`calls/<id>/call.json`), writes an inbound `Conversation` + `Artifact` rows to
`samus-crm`, opens an `OperatorTask` when the caller asked for an appointment
or callback, and emails the client on a voicemail or an urgent call — every
step best-effort so a CRM / mail outage never fails the webhook.
`provision.py` is the operator CLI that buys a Vapi DID and binds it to an
inbound assistant (`python -m backend.voice.provision`). `client_summary.py`
emails each client a weekly / monthly digest of the calls their receptionist
handled, rendered as a pure read over the per-call `call.json` artifact tree.

Optional ngrok embedded tunnel: when `NGROK_AUTHTOKEN` is set, the workcell opens
its own tunnel at startup and auto-PATCHes the Vapi assistant's `serverUrl`. With a
reserved domain (`NGROK_RESERVED_DOMAIN`) the URL is stable across restarts.

Morning dialer (`backend/voice/dialer.py`, `POST /voice/dial_call_list`) reads a
prospecting-emitted CSV call list, filters by TCPA hours + operator-configured
priorities + max_calls cap, and walks it via `VapiClient.create_call`.

### 4.10 CRM (`backend/crm/`)

Owns the 7 customer-pipeline DDB tables. Single-source-of-truth pattern: every other
workcell calls `samus-crm` via HMAC for any read/write of these entities (mirrors how
every workcell goes through `samus-memory` for k/v + graph state).

Tables owned (all DDB, single-PK):

| Table | PK | Phase 1 endpoints |
|---|---|---|
| `samus_prospects` | `prospect_id` | `GET /crm/prospects/{id}` |
| `samus_contacts` | `contact_id` | `GET /crm/contacts/{id}`, `GET /crm/contacts?prospect_id=` |
| `samus_conversations` | `conversation_id` | `GET /crm/conversations/{id}`, `GET /crm/conversations?prospect_id=` |
| `samus_call-State` | `prospect_id` | `GET /crm/call-state/{prospect_id}` (one current state per prospect) |
| `samus_opportunities` | `opportunity_id` | `GET /crm/opportunities/{id}`, `GET /crm/opportunities?stage=` |
| `samus_operator_tasks` | `operator_task_id` | `GET /crm/operator-tasks/{id}`, `GET /crm/operator-tasks?status=open` |
| `samus_artifacts` | `artifact_id` | `GET /crm/artifacts/{id}`, `GET /crm/artifacts?owner_entity_id=` |

Phase 1 write surface (one endpoint, the high-leverage conversion):

```
POST /crm/convert/lead
  body: {"lead_id": "lead_...", "assigned_to": "ops@hustleforge.tech"}
  flow:
    1. Read lead from samus_onboarding_leads
    2. If contact already exists with same email -> return existing IDs (status="existing")
    3. Else if prospect already exists with same website_url -> reuse it, attach new contact
    4. Else create both Prospect + Contact, return new IDs (status="created")
    5. Audit-log the conversion (PII-redacted: email_tail only, no pain_points)
```

Network posture: `samus-internal` only — no public surface. Other workcells (intake,
voice, finance, outreach) dispatch via `signed_post_json` for any CRM mutation.

`Prospect` reuses `backend.prospecting.models.ProspectRecord` to avoid duplicate
definitions; the other 6 entities are net-new and modeled against the
actual DDB schemas (which evolved from `recovery/prospect_schema.py` — `accounts` folded
into `prospects`, `activities` split into `conversations`+`call-State`, task PK renamed
to `operator_task_id`).

**Outreach follow-up lane** (v1.5.0/v1.5.1): `CallStateValue` gains the
`outreach_sent` state (an outreach message has gone out, the prospect awaits a
follow-up call on the day in `CallState.next_attempt_at`). New `FollowUpDue` /
`FollowUpList` Pydantic models (`FollowUpDue` is a *derived view*, not a stored
table). `crm.service.list_follow_ups_due(today)` scans `samus_call-State` for
`outreach_sent` CallStates whose `next_attempt_at` is due, joining each to its
originating outreach Conversation for the company + phone the operator needs to
dial. A prospect self-clears off the list once a call is logged — that moves
CallState off `outreach_sent`. Each `FollowUpDue` also carries a deterministic
"second opportunity" upsell: `_suggest_upsell` scans a corpus of the prospect's
Conversation transcripts / summaries / subjects + their Opportunity
(`next_step` + `name`), keyword-maps the strongest interest signal to a catalog
`sku_id` (resolved through `backend.catalog.registry`), and fills `upsell_sku` /
`upsell_name` / `upsell_pitch` — all three stay `""` when no signal clears the
bar or the top two SKUs tie (never a guessed pitch). No LLM.

**Operator CLIs**: `crm/log_call.py` records a hand-dialed call into the CRM —
the operator works the morning call list by hand and inputs each call's outcome
+ notes; it writes a Conversation + refreshes the prospect's CallState through
the canonical service layer (the same path the Vapi webhook uses), and also
appends an operator journal line to `call_outcomes_<date>.jsonl` (written first,
so a call is never lost on a CRM write failure). Invoked per-call by
`scripts/Log-Call.ps1`. `crm/create_opportunity.py` mints a tracked Opportunity
for a later-stage deal (a follow-up call that converts, or a hand-dialed
prospect who agreed to a product) — the booked→Opportunity wiring in `log_call`
fires only at first-dial time, so this is the path for everything after; the
minted `opportunity_id` is the exact-attribution key for a `client_reference_id`
buy link.

**Persistence fixes** (v1.5.0): `crm.persistence.safe_scan` previously applied
`Limit` as the DynamoDB scan *window*, silently dropping filtered rows past the
first page — it now paginates via `ExclusiveStartKey` so every filtered `crm`
query (`list_*` + the `convert_lead` dedup lookups) honours `limit` as a result
count. `crm.persistence.safe_put` runs every item through `_coerce_floats`
(recursive float→`Decimal` coercion) — boto3's DynamoDB resource rejects native
Python floats outright, and Opportunity rows carry float fields.

**Phase 2+ deferred** (each one a separate session):
- Pipeline FSM endpoints (autonomous_closer port)
- Hivemind graph projection (memory workcell mirrors Prospect→Contact→Conversation edges to Neo4j)
- DDB GSIs for sub-second lookups when full-table scans get too slow

### 4.11 Intake (`backend/intake/`)

Public receiver for the hustleforge.tech onboarding form. Mirrors the feedback
workcell's isolation pattern (own container, own image, samus-edge + samus-internal
networks). Form HTML lives in WordPress as an Elementor HTML block; submits via
`fetch()` to `https://api.hustleforge.tech/intake/onboarding`.

Also hosts the YouTube-notification email path: `youtube_ingest.handle_youtube_email()`
classifies an inbound channel notification, fetches the transcript, optionally
distills with Anthropic, and writes a `:YouTubeInsight` Hivemind node.

**YouTube top-N gate** (Lever 2.2): the distill call only fires when transcript
status is `ok` AND `len(transcript.text) >= SAMUS_YT_MIN_TRANSCRIPT_CHARS` (default
1000). Shorts and captionless previews skip the LLM call but the full transcript is
still persisted to disk + a stub KB node is written for operator triage. The system
prompt opted-in to caching (`cache_system=True`) with `max_tokens=700` (Lever 1.2).

**Public-ingestion hardening** (intake-hardening, 2026-05-20): `POST
/intake/onboarding` is backed by three additional defences (CORS alone does not
protect it — it is a browser-only mechanism, ignored by `curl` and scripts).
(1) A DynamoDB-backed per-source-IP rate limiter (`rate_limit.py`) — fixed-window
counters in the `samus_idempotency` table (atomic `ADD`, TTL'd rows) so the
ceiling holds across Cloud Run instances; defaults 5/min + 30/hour per IP + a
600/hour cross-IP global backstop; a breach returns HTTP 429 with a structured
JSON detail + `Retry-After`; the limiter **fails OPEN** so a DynamoDB hiccup
never blocks a real lead. (2) An optional Cloudflare-Turnstile CAPTCHA
(`captcha.py`) — dormant until the operator seals
`SAMUS_INTAKE_CAPTCHA_SECRET`; when set, the route requires + server-side-verifies
a `captcha_token` field (popped from the raw body before Pydantic validation, so
the persisted lead schema is unchanged) and **fails CLOSED** (a missing /
invalid token, or a verification call that cannot complete, rejects with HTTP
400). (3) Trusted-proxy XFF — `intake.app._client_ip` no longer trusts the
spoofable leftmost `X-Forwarded-For` entry; it takes the entry
`SAMUS_INTAKE_TRUSTED_PROXY_HOPS` (default 1) positions from the right and falls
back to the unspoofable socket peer when XFF is shorter than the trusted chain;
the rate limiter keys on this corrected IP. The operator email mirror
(`intake.service._format_operator_email_body`) sanitizes attacker-controlled
lead fields: CR/LF stripped from the Subject-line + single-line body fields
(header-injection defence), and `pain_points` fenced + per-line-prefixed so a
forged structured marker cannot pass as operator-authored text. See §6.4.

Endpoints:
- `POST /intake/onboarding` — `submit_lead`. CORS-allowed from
  `settings.intake_allowed_origins` (default `https://hustleforge.tech`). Pydantic
  validates name/email/company/website_url/service_interest[]/pain_points/
  monthly_budget/timeline against the exact HTML form contract. 24h dedup keyed by
  `sha256(email.lower() | website_url.lower() | pain[:200])`. Persisted to
  `samus_onboarding_leads` (PK=lead_id); audit ledger captures the lead even when
  DDB is unreachable.
- `GET /intake/leads` — `list_leads`. Operator-only paginated scan.

### 4.12 Strategy (`backend/strategy/`)

Cross-workcell decision layer. HTTP-only — no SQS worker (no `BaseSqsWorker` to
graft onto). Three sub-surfaces:

**Portfolio + bandit** (`portfolio_manager.py`, commits f7d52c8 + 8915682). UCB1
multi-armed bandit (`_BANDIT_STATS` dict, `update_bandit()`, `_ucb1_score()`) +
RL credit assignment (`record_outcome()` mapping won/lost/stale to bandit reward).
`propose_allocation(portfolio_state, market_signals, api_key)` fires **one** LLM
call routed through `anthropic_messages(workcell="strategy")` and returns an
`AllocationDecision(priorities, deprioritize, actions, raw_text, parse_error)`.
This is the **only** place in Samus that runs LLM reasoning across multiple jobs.

**Event-driven triggers** (`triggers.py`, Lever 3.2). Five signal-change detectors
walked by `check_and_fire()` (short-circuits on the first true):

1. `pipeline_ev_step` — `sum(score_opportunity(p))` shifts by `≥ portfolio_min_signal_change * prev` (default 15%) between successive daily snapshots.
2. `bandit_divergence` — top-1 arm changes in any bandit family.
3. `new_cohort` — a discovery batch adds prospects whose combined `lead_score` exceeds pipeline median.
4. `operator_manual` — `manual_signal()` set by an HTTP route or CLI; rate-limited 1/hr via in-process token bucket.
5. `budget_recovery` — `efficiency_ema < 0.4` on `prospecting` or `seo` workcell.

Each fire = one `propose_allocation()` LLM call. Expected cadence 5–15 fires/day
in normal operation; trigger spend ($0.03–$0.08/day at Haiku rates) is informational-capped
by `portfolio_workcell_dollar_cap=$0.20/day` and hard-capped by the global $1/day budget.

**Tick scheduler** (`start_tick_loop`). Standalone daemon `threading.Timer` chain
at `portfolio_tick_interval_sec` (default 900 = 15 min). Callers wire into FastAPI
`lifespan` (startup → capture handle, shutdown → `handle.stop()`) or a CLI long-runner.
Pure-function-friendly via explicit `TriggerContext` injection — no coupling to a
specific worker.

**Storage**:
- Snapshots: DDB `samus_portfolio_snapshots` (PK=`bucket_day` YYYY-MM-DD, one row
  per UTC day). JSON-file fallback at `SAMUS_PORTFOLIO_SNAPSHOT_PATH` mirrors
  `common/llm_budget._JsonBackend`.
- Trigger audit ledger: jsonl at `SAMUS_PORTFOLIO_TRIGGER_LEDGER` (best-effort
  append; never raises).

**Pure-logic siblings** (no LLM, no DDB beyond their own minimal needs):
`optimizer.py` (rule-based ranker), `capability_marketplace.py` (in-memory credit
ledger), `credit_ledger.py` (RL credit-assignment tracker), `trust_scorer.py`
(trust-weighted autonomy math), `engine.py` / `dispatcher.py` / `service.py` /
`crm_client.py` (wiring). The v1.4.0 reward-density layer
(`reward_density.py`, `momentum_tracker.py`, `regret_engine.py`,
`saturation_monitor.py`, `predictive_allocator.py`, `policy_compiler.py`) is
likewise all pure-logic, zero-LLM.

### 4.13 Morning Brief (`backend/morning.py`)

Not a containerized workcell — a host-side daily briefing assembled by
`backend.morning` and shipped at 08:00 via `scripts/Send-Morning.ps1`. The brief
has a SALES section with two lanes:

- **Call list** — `_render_call_list` reads the structured `call_list_<date>.csv`
  written by the prospecting pipeline and renders one scannable line per prospect.
  The sort is `call_priority` → `lead_score` descending → `security_grade` (worse
  grade ranks higher, ungraded last) → `seo_score`, matching `text_export`.
- **Follow-ups due** — `_render_follow_ups` reads CRM CallState via
  `crm.service.list_follow_ups_due` (the outreach follow-up cadence from v1.5.0)
  and renders one line per due prospect (company, phone, days-waiting, the
  outreach subject), with an `↗` upsell hint when `FollowUpDue.upsell_pitch` is
  populated. The lane self-omits when there are no follow-ups or DynamoDB is
  unreachable. `scripts/Show-Morning.ps1` loads the Samus AWS creds from DPAPI
  (optional) so the brief can read DynamoDB — absent creds simply omit the lane.

### 4.14 Pipeline Wiring — the Two Funnels

Samus runs two revenue funnels. Workcells dispatch to each other through the
gateway (`POST /dispatch/<target>` → SQS sidecar, else HTTP fallback to
`/work`); every cross-workcell hop below is a best-effort *signed* dispatch —
a config gap or transport failure logs a warning and never undoes the
producer's own committed write.

```
OUTBOUND COLD FUNNEL — operator works a daily call list
──────────────────────────────────────────────────────────────────────────
 prospecting  (Run-ProspectingDaily.ps1 — 07:30 daily)
   discover → score → callsheet  ──►  call_list_<date>.csv
                                         │
                  ┌──────────────────────┴───────────────────────┐
            Log-Call.ps1                                  outreach.send_message
            ──► crm: Conversation                         (on email success)
                + CallState + Opportunity                 ──► crm: Conversation(outreach_sent)
                                                               + CallState → follow-up cadence
                                                               (re-surfaces in the morning brief)

INBOUND DEAL FUNNEL — a deal moves through the Opportunity FSM
──────────────────────────────────────────────────────────────────────────
 intake  POST /intake/onboarding   (public form @ hustleforge.tech)
   submit_lead ──► samus_onboarding_leads
       └─[operator] POST /crm/convert/lead ──► Prospect + Contact ──► Opportunity

 crm Opportunity FSM:  new → qualified → proposal → negotiation → closed_won│lost
                                            │                         │
                  Unit P (v1.8.0) ──────────┘                         └────── v1.7.0
                  advance_opportunity(target="proposal")          terminal close
                      │ _dispatch_intake_to_proposal                  │ _dispatch_outcome_to_strategy
                      ▼                                               ▼
              gateway /dispatch/proposal                    gateway ──► strategy.record_outcome
                      │                                        (reward-density bandit learns)
                      ▼
              proposal.generate_proposal
                      │
          ┌───────────┴───────────────┐
   status == approved           CRM-linked (any status)
          │ Unit S (v1.8.0)            │ (pre-v1.8.0)
          ▼                            ▼
   gateway /dispatch/scaffold    gateway /dispatch/crm
          │                            └─► crm Artifact (kind=proposal, source=proposal)
          ▼
   scaffold.generate_scaffold  ──► renders the client-facing proposal_pack
          │ Unit S (v1.8.0) _dispatch_artifact_to_crm
          ▼
   gateway /dispatch/crm  ──►  crm Artifact (kind=proposal, source=scaffold)

REAL-TIME CALL SCORING — external Vapi sales agent
──────────────────────────────────────────────────────────────────────────
 Vapi "Morgan SDR" agent  (recovery/vapi_sales_agent_config.md — Node 4 tool hook)
   ──► gateway /dispatch/leadgen ──► samus-leadgen SQS ──► LeadgenWorker
                                                              └─ score_lead → LeadScore(tier)
   Unit L (v1.8.0) pins the Samus side of this contract; deploying the Vapi
   agent config is an operator step, external to this repo.
```

Before v1.8.0 `proposal`, `scaffold` and `leadgen` were built and tested but
had **zero inbound dispatchers** — three orphaned workcells. Units P/S/L (see
the v1.8.0 changelog) wire them in: the CRM `proposal`-stage transition draws
the proposal workcell, an approved proposal draws scaffold, and both register
their deliverables back to CRM so the operator pulls every artifact for a deal
from one place. `leadgen`'s producer is external (the Vapi agent), so Unit L
pins the dispatch contract with tests rather than adding an in-repo caller.

---

## 5. LLM Budget System

Samus runs every Anthropic call through a 4-layer cost-control stack in
`backend/common/llm_client.anthropic_messages()`. The layers compose top-down —
any one denying short-circuits the HTTP call and raises `BudgetExceeded` or
`ModelNotPermitted` or `CircuitOpen`; callers handle by falling back to the
templated / non-LLM path.

**Production policy** (operator-chosen, see `project_samus_llm_token_policy` memory):

| Scope | Cap | Enforced by |
|-------|-----|-------------|
| Global, all workcells | **$1.00 / day** (Haiku $1/MTok input + $5/MTok output + $0.10/MTok cache-read) | Layer A — `llm_global_budget.py` |
| Per-workcell tokens | 100k tokens/day base, EMA-adaptive (0.5x–2.0x) | Layer 0 — `llm_budget.py` |
| Per-job LLM calls | **0 or 1** per job (deterministic top-N gates in callsheet / seo / youtube; cf. §4.2, §4.4, §4.11) | Caller-side |
| Per-cycle (portfolio) | 1 per `propose_allocation()` fire; expect 5–15 fires/day total | Caller-side + §4.12 triggers |
| Cross-job deterministic workcells (fulfillment / proposal / outreach / leadgen / crm / finance / voice) | **0** LLM calls — rule-based only | §5.5 AST lint guard |

The 4 cost-control layers (top → bottom inside `anthropic_messages()`):

| Layer | Module | What it gates on |
|-------|--------|------------------|
| **A — global $-cap** | `llm_global_budget.py` | Sum across all workcells today, in dollars |
| **B — model floor** | `llm_client.py` (regex check) | Model id must match `llm_cheap_model_pattern` unless `allow_expensive_model=True` |
| **C — circuit breaker** | `llm_budget.py` (consecutive-error tracker) | N consecutive errors per workcell → 5-min cooldown |
| **0 — per-workcell tokens** | `llm_budget.py` (`can_spend` + `record_spend`) | Token-count quota with EMA-driven daily cap |

A successful call records spend to both Layer 0 (per-workcell tokens, by kind:
input / output / cache-creation / cache-read) and Layer A (dollars), via two
DDB writes against the same `samus_llm_budgets` table.

### 5.0 Per-workcell token quota (Layer 0)

Every Anthropic call books two events through `backend/common/llm_budget.py`:

- **pre-flight** `can_spend(workcell, est_tokens)` — returns `(allowed, reason)`.
  Callers that get `allowed=False` fall back to the templated / non-LLM path.
- **post-flight** `record_spend(workcell, input_tokens, output_tokens, outcome,
  cache_creation_input_tokens=0, cache_read_input_tokens=0)` — ingests
  Anthropic's `usage` block, classifies the call as `success` / `failure` /
  `error`, and (when the call carried `cache_control` blocks) splits the input
  bucket into fresh / cache-creation / cache-read for accurate cost math.

### Adaptive Sizing

```
base   = settings.llm_workcell_base_token_budget     # default 100_000
factor = 0.5 + 1.5 * efficiency_ema                  # 0.0 -> 0.5x, 1.0 -> 2.0x
quota  = max(base * floor_pct, base * factor)        # floor at 10% of base
```

`efficiency_ema` is an exponentially-weighted moving average of "did the caller
mark this call as a success?" with `alpha = 0.05` (~60 calls to fully respond to a
step change). LLM-call errors (network, 5xx, parse failure) are tracked separately
and **do not** affect the EMA — a transient outage shouldn't punish the workcell's
future quota. Workcells with fewer than 10 non-error calls bypass adaptive scaling
(don't punish fresh workcells with insufficient signal).

### Outcome Classification

| Outcome   | Triggered by                                  | Effect on EMA                            |
|-----------|-----------------------------------------------|------------------------------------------|
| `success` | HTTP 200 + parseable response                 | EMA pulled toward 1.0; `success_count++` |
| `failure` | Caller validated output and rejected it       | EMA pulled toward 0.0; `failure_count++` |
| `error`   | Network / 5xx / parse error before any output | No EMA change; `error_count++`           |

Callers that auto-receive `outcome=success` from the wrapper but discover during
their own validation that the model returned unusable output (missing fields,
malformed JSON) call `record_outcome(workcell, outcome="failure")` to flip the
classification — the spend stays counted, but efficiency reflects reality.

### Storage

DDB table `samus_llm_budgets` is shared across two PK shapes:

- **`PK = <workcell>`** — one row per workcell (Layer 0 quota state).
- **`PK = "GLOBAL"`** — one row carrying today's dollar spend across all workcells
  (Layer A state). Atomic `UPDATE ADD` keeps both rows correct under concurrent
  writes.

Daily counters reset by checking `bucket_day` on every read — when today's date
differs from stored, daily fields zero out but the EMA + call history are
preserved. JSON-file fallback at `SAMUS_LLM_BUDGET_PATH` (default
`/opt/samus/data/llm_budgets.json`) when DDB is unreachable; the global-cap row
falls back independently to `SAMUS_LLM_GLOBAL_BUDGET_PATH`. Catastrophic store
failure returns `allowed=True` — work never blocks on the budgeter (allow-on-
persistence-failure is intentional; the operator alarms on cap breach but the
agent doesn't deadlock).

### 5.1 Global $-cap (Layer A — `llm_global_budget.py`)

A second budget store enforces a **single dollar amount per day** across every
workcell. The default is `llm_global_daily_dollar_cap=1.0` per operator
production policy (the 29ba2df hardening commit defaulted to $25; Samus runs
tighter).

Dollar math uses `llm_pricing.py` — a static table keyed on the model id prefix:

| Model family | Input $/MTok | Output $/MTok | Cache-read $/MTok | Cache-write $/MTok |
|--------------|-------------:|--------------:|------------------:|-------------------:|
| Haiku 4.5    | $1.00 | $5.00 | $0.10 | $1.25 |
| Sonnet       | $3.00 | $15.00 | $0.30 | $3.75 |
| Opus         | $15.00 | $75.00 | $1.50 | $18.75 |

(Sonnet + Opus rates are tabled for safety only; the model floor — Layer B —
rejects them by default.)

Pre-flight: `llm_global_budget.can_spend(estimated_dollars)` — denies when
today's spend + estimated would exceed the cap. Post-flight: `record_spend(...)`
adds actual dollars to the GLOBAL row. Estimation uses `usage` from the wrapper
(after Anthropic returns) for accuracy; pre-flight uses a conservative
`max_tokens`-based upper bound.

### 5.2 Model floor (Layer B)

`llm_client.anthropic_messages()` raises `ModelNotPermitted` if the requested
model doesn't match `llm_cheap_model_pattern` (default `^claude-haiku-`) unless
the caller passes `allow_expensive_model=True`. Defense-in-depth against an
accidental Opus call that would cost 20x. Layer B fires before any HTTP call
goes out.

### 5.3 Circuit breaker (Layer C)

Per-workcell consecutive-error counter in `llm_budget.py`. After
`llm_circuit_breaker_threshold` (default 10) errors in a row,
`can_spend(workcell, ...)` denies for `llm_circuit_breaker_cooldown_sec`
(default 300 s = 5 min). Resets on any single success/failure outcome.
Stops retry storms from burning Anthropic quota on guaranteed-failing calls.
Metric: `samus_llm_circuit_trips_total{workcell}`.

### 5.4 Prompt caching (Layer D)

`anthropic_messages()` accepts `cache_system: bool = False`. When True, the
`system` prompt (whether passed as a string or as a list of content blocks) is
wrapped in `{"type":"text", "cache_control":{"type":"ephemeral"}, ...}` and
shipped with the Anthropic prompt-cache beta header. Repeated calls inside the
5-minute cache window read the cached prefix at ~10% the normal input cost.

Used by all three current LLM callers — every workcell's stable instruction
block (extracted as module-level `_CALLSHEET_INSTRUCTIONS` / `_SEO_INSTRUCTIONS`
/ `_SYSTEM_PROMPT`) rides in the cached prefix; only the variable per-job
payload counts as fresh input (Lever 1.3).

The wrapper's `_extract_usage()` returns `input_tokens`,
`output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` from
Anthropic's response; `record_spend()` persists all four into the WorkcellBudget
row + the GLOBAL dollar row.

### 5.5 AST lint guard (Lever 2.3)

`tests/test_llm_caller_lint.py` walks `backend/` and fails if any module other
than the 3-file allowlist (`common/llm_client.py` + the two legacy direct-httpx
fallback paths in `prospecting/callsheet.py` + `seo/content.py`) touches the
Anthropic surface — either by `import anthropic` or by containing the literal
`api.anthropic.com`. Adding a 4th caller is a reviewer chokepoint: the
`_ALLOWLIST` constant must be edited explicitly.

### Telemetry

Prometheus instruments (in `common/metrics.py`):

- `samus_llm_tokens_total{workcell, kind=input|output|cache_creation|cache_read}` — Counter
- `samus_llm_calls_total{workcell, outcome=success|failure|error}` — Counter
- `samus_llm_budget_quota_tokens{workcell}` — Gauge (adaptive daily quota)
- `samus_llm_budget_used_tokens{workcell}` — Gauge (today's consumption)
- `samus_llm_efficiency_ema{workcell}` — Gauge (rolling EMA driving the quota)
- `samus_llm_dollar_used_today{scope}` — Gauge (`scope=GLOBAL` for the day's $ spend vs cap)
- `samus_llm_cache_hit_ratio{workcell}` — Gauge (cache-read / total-input ratio)
- `samus_llm_circuit_trips_total{workcell}` — Counter (Layer C trips)

Operator surface: `GET /admin/llm_budgets` on the gateway returns the per-workcell
snapshot (quota, used, remaining, EMA, daily counters) plus the store's base
budget / alpha / floor constants. The same endpoint surfaces the `GLOBAL` row
($-spend today vs cap, circuit-breaker state per workcell, last cache-hit ratios).

### Callers

| Workcell      | Module                              | Calls/job ceiling | Top-N gate |
|---------------|-------------------------------------|------------------:|------------|
| `prospecting` | `backend/prospecting/callsheet.py`  | 0 or 1            | `call_priority == "hot"` AND `lead_score >= 75` |
| `seo`         | `backend/seo/content.py`            | 0 or 1            | `>= 2 target_keywords` AND `>= 1 on_page_changes` |
| `intake`      | `backend/intake/youtube_ingest.py`  | 0 or 1            | `len(transcript.text) >= SAMUS_YT_MIN_TRANSCRIPT_CHARS` (default 1000) |
| `strategy`    | `backend/strategy/portfolio_manager.py` | 1 per fire    | Event-driven via `backend/strategy/triggers.py` (5 signal-change detectors; see §4.12) |

All four callers route through `llm_client.anthropic_messages()` and degrade to
the templated / templated-equivalent path on `BudgetExceeded` / `ModelNotPermitted` /
`CircuitOpen` / `LlmCallError`. On parse failure they call
`record_outcome("<workcell>", outcome="failure")` so the workcell's EMA reflects
the wasted spend.

Adding a fifth LLM caller is fenced by the AST lint guard (§5.5) — the
`_ALLOWLIST` constant must be edited and reviewed.

---

## 6. Inbound / Public Surfaces

Three workcells terminate public traffic. Each has its own trust boundary.

### 6.1 Gateway

HMAC-SHA256 signed requests via `VerifyHMACMiddleware`. Operator-facing; internal
callers sign with the shared key (DPAPI `SharedHmacKey`). 300 s timestamp window,
NonceStore replay defense.

### 6.2 Feedback (SES via SNS)

SNS X.509 signature verification per AWS spec (SHA1+RSA / SHA256+RSA), HTTPS-only
`SigningCertURL` host allowlist, TopicArn allowlist (configurable), in-process cert
TTL cache. Replay defense via `IdempotencyStore` keyed on `sns:msgid:{MessageId}`.
`SAMUS_FEEDBACK_VERIFY_SNS=0` opts out for tests.

### 6.3 Voice (Vapi webhooks)

HMAC-SHA256 of raw body with `vapi_webhook_secret` (`x-vapi-signature` header),
constant-time compare. Refuse-don't-degrade for missing secret (503).
`SAMUS_VOICE_VERIFY_WEBHOOK=0` opts out for tests.

### 6.4 Intake (browser POST from marketing site)

CORS-only — there's no per-message signature because the form is unauthenticated.
Abuse story:

- Pydantic validation with field-length caps matching the HTML form's `maxlength`.
- 24h dedup keyed by `sha256(email|website_url|pain[:200])` — same lead inside the
  window short-circuits to `status=duplicate` with no second DDB write.
- `extra="forbid"` on the request model so spammers can't smuggle unexpected fields.
- CORS `allow_origins=[settings.intake_allowed_origins]` — defaults to
  `https://hustleforge.tech` only, no wildcards.
- Cloud Run `max-instances=5` is the natural throughput ceiling.

<!-- BEGIN intake-hardening section (feat/samus-intake-hardening) -->
**Rate limiting + CAPTCHA + trusted-proxy XFF (feat/samus-intake-hardening, 2026-05-20).**
CORS does NOT protect this endpoint — it is a browser-only mechanism, so `curl`
and scripts ignore it entirely. Three additional defences now back the public
POST:

- **Per-IP rate limit** (`backend/intake/rate_limit.py`). Fixed-window counters
  in the `samus_idempotency` DynamoDB table (atomic `ADD`, TTL'd rows) so the
  ceiling holds across Cloud Run instances — a per-instance dict counter would
  miss a flood spread over instances. Defaults: 5/min + 30/hour per source IP,
  plus a 600/hour cross-IP global backstop. Configurable via
  `SAMUS_INTAKE_RATE_LIMIT_PER_MINUTE` / `_PER_HOUR` / `_GLOBAL_PER_HOUR` /
  `_ENABLED`. A breach returns HTTP 429 with a structured JSON detail and a
  `Retry-After` header. The limiter **fails OPEN**: any DynamoDB error logs a
  warning and allows the request — losing a sale to a backend hiccup is worse
  than briefly tolerating abuse.
- **Optional Turnstile CAPTCHA** (`backend/intake/captcha.py`). Dormant by
  default. When the operator seals `SAMUS_INTAKE_CAPTCHA_SECRET`, the route
  requires a `captcha_token` field and server-side-verifies it against the
  Cloudflare siteverify API. The token is popped from the raw request body
  before Pydantic validation, so the persisted lead schema is unchanged.
  CAPTCHA **fails CLOSED**: a missing/invalid token, or a verification call
  that cannot complete, rejects the request with HTTP 400.
- **Trusted-proxy XFF.** `_client_ip` trusts only the proxy-appended portion of
  `X-Forwarded-For`. The leftmost entries are attacker-controlled; the real
  client is the entry `SAMUS_INTAKE_TRUSTED_PROXY_HOPS` (default 1, matching
  Cloud Run / a single Caddy) positions from the right. When XFF is shorter
  than the trusted chain, it falls back to the unspoofable socket peer. The
  rate limiter keys on this corrected IP.

The operator email mirror (`_format_operator_email_body`) sanitizes
attacker-controlled lead fields: CR/LF stripped from Subject-line and
single-line body fields (header-injection defence), and `pain_points` fenced +
per-line-prefixed so a forged structured marker cannot pass as
operator-authored text. `submit_lead` output reaches only this plain-text
email today — if an HTML/markdown surface is ever added, HTML-escape the same
fields at that boundary.
<!-- END intake-hardening section -->

To activate CAPTCHA, the operator seals `SAMUS_INTAKE_CAPTCHA_SECRET` (DPAPI /
Cloud Run secret) and adds the Turnstile widget to the marketing-site form.

---

## 7. DynamoDB Tables

| Table                       | PK / SK                | Owner workcell        | Purpose                                       |
|-----------------------------|------------------------|-----------------------|-----------------------------------------------|
| `samus_task_state`          | `task_id`              | all (via `worker_base`) | Lifecycle: pending / processing / completed / failed |
| `samus_idempotency`         | `idempotency_key`      | all                   | Dedup key for SQS message processing. Also carries the intake rate-limiter's fixed-window counter rows (§6.4). |
| `samus_suppression`         | `email`                | feedback              | SES bounce/complaint suppression list         |
| `samus_feedback_events`     | `event_id`             | feedback              | Audit row per SNS notification                |
| `samus_onboarding_leads`    | `lead_id`              | intake                | Raw onboarding form submissions               |
| `samus_llm_budgets`         | `workcell`             | all LLM callers       | Per-workcell daily token counters + EMA. Also stores `PK="GLOBAL"` sentinel row for the cross-workcell dollar cap (§5.1). |
| `samus_portfolio_snapshots` | `bucket_day` (YYYY-MM-DD) | strategy             | One row per UTC day: pipeline EV, prospect count, bandit top-arms, efficiency-EMA-by-workcell. Feeds `strategy/triggers.py` signal-change detection (§4.12). |
| `samus_prospects`           | `prospect_id`          | crm                   | Companies being pursued                       |
| `samus_contacts`            | `contact_id`           | crm                   | People at prospect companies                  |
| `samus_conversations`       | `conversation_id`      | crm                   | Multi-turn exchange threads (calls, emails)   |
| `samus_call-State`          | `prospect_id`          | crm                   | Current FSM state of outbound dialer per prospect |
| `samus_opportunities`       | `opportunity_id`       | crm                   | Sales deals + pipeline stage                  |
| `samus_operator_tasks`      | `operator_task_id`     | crm                   | Human-facing to-do items                      |
| `samus_artifacts`           | `artifact_id`          | crm                   | Generated deliverables (proposals, audits)    |

All tables `PAY_PER_REQUEST` billing, region `us-west-1`.

**Entity-shape notes** (fields that recent work added to the Pydantic models, not
new tables):

- `ProspectRecord` (the `samus_prospects` shape, reused as `crm.Prospect`) carries
  `website_status` (reachability classification — `no_website` / dead / ok set in
  prospecting Step 2a), `business_description` (homepage scrape, Places
  editorialSummary fallback), and `security_grade` (the A-F passive-security grade
  from the warm/hot full audit, §4.2 Step 2.7) — alongside the existing
  `seo_score` and `seo_report_path`.
- `Conversation` (the `samus_conversations` shape) gains inbound AI-receptionist
  fields, all defaulting to the outbound-call shape so existing rows are
  non-breaking (`model_config` is `extra="ignore"`): `direction`
  (`outbound`/`inbound`), `customer_id` (the receptionist CLIENT slug whose phone
  was answered — NOT the caller; `""` for outbound rows), `caller_number`
  (inbound caller ID, E.164), `answered`, and `voicemail_left`.

Provisioning runbook lives in the operator workflow docs (`scripts/` README). Until
a table exists, the owning workcell silently falls back to JSON file storage under
`/opt/samus/data/` so dev / first-boot / table-being-created doesn't block traffic.

---

## 8. SQS Queues

Per-workcell pairs: `samus-<workcell>-jobs` (work queue) + `samus-<workcell>-dlq`
(dead-letter on max-receive-count). Workers consume `*-jobs`; SQS auto-moves
poison/exhausted messages to `*-dlq` where the `gateway /dlq/{service}` endpoint can
inspect and the operator can replay via `replay_worker.replay_gateway_dlq()`.

Workcells with provisioned queues: leadgen, prospecting, scaffold, fulfillment,
outreach, optimizer, proposal, seo. HTTP-only Phase 1: feedback (deployed
separately), finance, voice, intake.

---

## 9. Tests (`tests/`)

`pytest` with autouse fixtures in `conftest.py`:

- `_reset_settings_cache` — reloads `Settings` per test so env overrides take effect.
- LLM-budget store reset + JSON tmpfile redirect — per-test budget state isolated.
- Default `SAMUS_FEEDBACK_VERIFY_SNS=0` and `SAMUS_VOICE_VERIFY_WEBHOOK=0` —
  dedicated test files re-enable per-test to exercise the signature gates.
- `DDB_LLM_BUDGETS_TABLE=""` — store skips DDB, only touches the JSON tmpfile.

Workcells with comprehensive test coverage: every workcell except `tools/`. End-to-end
integration tests in `test_e2e_integration.py` walk gateway → workcell → memory
through the HTTP fallback path.

Recent test additions track the changelog: `test_prospecting_scorer.py` (v1.6.1
continuous-scorer rebuild + recalibrated thresholds), `test_seo_security_audit.py`
(v1.6.0 passive security audit, 44 tests), `test_intake_rate_limit.py` /
`test_intake_captcha.py` plus added cases in `test_intake_app.py` /
`test_intake_service.py` / `test_finance_webhook.py` (intake-hardening),
`test_crm_persistence.py` / `test_crm_follow_ups.py` /
`test_outreach_crm_dispatch.py` / `test_morning_follow_ups.py` (v1.5.0 outreach
follow-up cadence), and the `test_strategy_*` set (v1.4.0 reward-density layer).

The v1.1.0 LLM cost-control test set (7 files: `test_common_llm_{circuit_breaker,
global_budget, model_floor, pricing, prompt_cache}.py`, `test_llm_caller_lint.py`
AST guard, `test_strategy_triggers.py` for event-driven portfolio triggers) remains
intact.

---

## 10. Out of Scope (Operator-Only Surfaces)

The following are operator workflows on the host, not Samus containers:

- DPAPI secret store (`_shared/scripts/Hustleforge.Secrets.psm1`).
- Morning briefing (`scripts/Send-Morning.ps1`, `Show-Morning.ps1`).
- CRM call logging (`scripts/Log-Call.ps1` → `backend.crm.log_call`).
- Health monitor (`scripts/health_monitor.py`).
- Recovery transcripts and operator playbooks under `recovery/`.
- Executive docs (LLC formation, EIN, filings) under `Executive Docs/` (gitignored).

These exist alongside Samus but never call into the container stack.
