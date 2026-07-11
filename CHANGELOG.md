# Changelog

Notable changes per release. Full engineering detail for each version is in the sections below.

---

## Unreleased

_(No unreleased changes.)_

---

## 2.2.1 — 2026-07-10

Portfolio-readiness release. Infrastructure work + engineering-judgment documentation + pre-public sanitization + inbound-email routing engineering.

### CI + supply-chain

- 7 GitHub Actions workflows added under `.github/workflows/`:
  - `ci.yml` — ruff lint + format check + pip-audit
  - `tests.yml` — pytest matrix on Python 3.11 + 3.12 with Codecov upload
  - `typecheck.yml` — mypy on `backend/common/` (lenient baseline)
  - `codeql.yml` — Python SAST with `security-and-quality` query pack
  - `gitleaks.yml` — full-history secret scan on every push/PR
  - `trivy.yml` — filesystem CVE scan + Dockerfile config scan (SARIF to Security tab)
  - `scorecard.yml` — OpenSSF Scorecard weekly grade
- `.github/dependabot.yml` — weekly grouped pip + Actions updates; `cryptography` and `structlog` excluded from grouped auto-PRs (supply-chain pinned)
- `pyproject.toml` — added `[tool.mypy]` (lenient baseline), `[tool.coverage.run/report]` scoped to `backend/common`
- `requirements-dev.txt` — added `pytest-cov>=5.0`, `mypy>=1.11`
- `LICENSE` — MIT, added at repo root for GitHub license detection

### Documentation

- 10 new root-level engineering-judgment documents in first-person voice: `ENGINEERING_DECISIONS.md`, `ARCHITECTURAL_TRADEOFFS.md`, `LESSONS_LEARNED.md`, `SYSTEM_EVOLUTION.md`, `FAILURE_MODES.md`, `KNOWN_TECHNICAL_DEBT.md`, `SCALABILITY.md`, `PERFORMANCE.md`, `INTERVIEW_NOTES.md`, `REPOSITORY_REVIEW_GUIDE.md`
- `docs/DESIGN.md` rewritten with rejected-alternatives reasoning
- `docs/SECURITY.md`, `docs/OPERATIONS.md`, `docs/DEVELOPMENT.md`, `docs/PROTOCOL.md` refined or added
- Reviewer-facing documents in `review/`: `ENGINEERING_SUMMARY.md`, `DESIGN_DECISIONS.md`, `TRADEOFFS.md`, `INTERVIEW_GUIDE.md`, `KNOWN_LIMITATIONS.md`, `TESTING_EVIDENCE.md`, `REPOSITORY_AUDIT.md`
- README restructured: 12 CI + language + license badges, "Repository at a glance" table with real counts (22 apps / 15 workers / 505 test files / ~5,617 test functions / 22 Dockerfiles / 8 ADRs / 12 Codex chapters), security posture section referencing the actual files, PowerShell operator surface reorganized into 4 categories with all 30 scripts verified present
- All release history extracted from `ARCHITECTURE.md` into this changelog

### Engineering

- Client-intent routing: `backend/crm/client_intent_routing.py` — pure function mapping the LLM classifier's intent tag to `(operator-task kind, due_at, title prefix)` so the CRM queue surfaces urgent work without a new priority field
- Post-drain categorized forwarder: `backend/intake/email_forwarder.py` — re-sends every processed inbound message to the operator with a `[CATEGORY/INTENT]` subject prefix and trashes the original; low-confidence hits route to `[URGENT/UNCLASSIFIED]` for review; governed by `SAMUS_FORWARD_ENABLED`, `SAMUS_FORWARD_TO_EMAIL`, `SAMUS_FORWARD_CLASSIFY_MIN_CONFIDENCE` (defaults keep the forwarder off)
- `backend/crm/models.py` extended with routing shape; `backend/intake/gmail_poller.py` invokes the router; `backend/intake/gmail_api_client.py` gets `trash` + `send-as` helpers
- Content-based client-directory association + Titan HTML forward support (autonomy commits `cd34fa3`, `814e735`)

### Security / sanitization (pre-public prep)

- Infrastructure identifiers redacted across 35 tracked files: GCP project ID → `${GCP_PROJECT}`, project number → `${GCP_PROJECT_NUMBER}`, runtime service account, real `.run.app` webhook URLs
- 22 workcell Dockerfiles: `ARG BASE_IMAGE` default now references `${GCP_PROJECT_ID}`; Cloud Build passes `--build-arg BASE_IMAGE=…` so cloud deploys are unaffected
- Cloud Build YAMLs (`docker/cloudbuild.yaml`, `docker/cloudbuild-intake.yaml`): substitution defaults and comments redacted
- Deploy scripts (`Deploy-SamusCloudRun.ps1`, `Register-CloudSchedulerJobs.ps1`, `Register-SamusCloudDutyCycleSchedule.ps1`, `Set-SamusCloudDutyCycle.ps1`): operator provides project state at run time
- Customer PII: real client identifiers (`bbc4me.org`, `mightyhelpinghands.com`) replaced with `example.com` placeholders in campaign templates, test fixtures, and generic code paths (files with legitimate operational client data — `clients/sample_school/campaign.yaml`, `scripts/website_orders/harmony.json` — retain real data internally and are excluded from the public snapshot)
- Operator email: `hartman530@gmail.com` no longer hardcoded as `_DEFAULT_EMAIL_TO` in `backend/gateway/morning_ritual_task.py` and `production_health_task.py`; both now require `SAMUS_MORNING_EMAIL_TO` / `SAMUS_HEALTH_EMAIL_TO`; `Send-Morning.ps1`, `Run-ProductionHealth.ps1`, `Register-ProductionHealthSchedule.ps1` defaults reference env vars
- Live-tier Stripe publishable key removed from `recovery/onboarding_form_schema.py` (publishable keys are technically safe to disclose but identify the Stripe account; rotation recommended before public flip)
- 5 disposable one-shot scripts deleted (`_oneshot_bootstrap_secret_manager.ps1`, `_oneshot_setup_creds.ps1`, `_oneshot_smoke_recreate_gateway.ps1`, `_oneshot_update_inbox.ps1`, `_oneshot_wp_link_surgery.py`) — they held hardcoded infrastructure state
- Post-sanitization verification: 0 tracked references to any of the redacted identifiers; 0 hits across common credential patterns (AWS, Stripe, Google API, GitHub, Slack tokens); Ed25519 signatures and SHA-256 file hashes retained as cryptographic verifiers (not secrets)

### Architecture

- `ARCHITECTURE.md` rewritten as a clean architecture reference — all per-version release narrative extracted into this changelog. Header now points to `CHANGELOG.md` for release history.

### Known limitations (still true at this release)

- CodeQL on private repos requires GitHub Advanced Security (paid); Scorecard `publish_results` set to `false` while repo is private; Codecov coverage upload requires `CODECOV_TOKEN` secret while private
- Test suite has ~40 known-pre-existing environment failures (documented in v2.2.0 notes) unrelated to the CI/portfolio work in this release
- Pre-sanitization identifiers remain in git history; the public snapshot repo is the mitigation (single-commit orphan, no history exposure)

---

## 2.2.0 — 2026-07-07

### Added

- Institutional-memory precedent linkage via `backend/cognitive/belief_ledger.py` + `backend/common/codex/registry.py` + `backend/cognitive/intelligence_cycle.py`.
- Strategic-compression rollup: `GuidanceLaw` rows materializing nightly DISTILL lessons as durable evidence-cited business rules; top-N laws surface in morning brief.
- Organizational-economics report: six-metric joined report (coordination cost, decision latency, context switching, cognitive overhead, approval friction, communication entropy) over existing surfaces; degrades gracefully on missing sources.
- Epistemic governance: `Belief.depended_by` + `link_decision` + contradiction hook that files ADR-0019-severity HOTL approval when a flipped belief has downstream dependents.
- Capability-market persistence + quorum bridge: JSONL-backed `PersistentCapabilityMarketplace` + HMAC-signed publisher over the cross-agent `quorum_hub`. Dormant until a caller instantiates it.
- Self-generated scientific method: `propose_next_experiment` closes the observe→hypothesize→experiment→measure→publish→institutionalize loop; high-risk proposals gate on ADR-0019 HOTL approval before arming.

### Rejected

- Recursive organizational design (self-restructuring departments/management layers) — `governance/org_debt.py` scopes itself to single-agent reality; no coordination-cost ROI at current scale.

### Test results

5,747 passed / 5,787 collected. 40 failures confirmed pre-existing / environment-specific: 13 from gitignored finance fixture files absent from worktree copy, 16 from a pre-existing immutable-baseline hash drift on `backend/common/audit_ledger.py` (flagged for operator re-sign), 10 from a `PermissionError` on `E:\Hustleforge\Samus\data\artifacts` in non-elevated environment (passes 44/44 with `SAMUS_ARTIFACT_ROOT` overridden).

---

## 2.1.1 — 2026-07-06

### Added

- Causal uplift analysis (`backend/experiments/uplift.py`): per-arm two-proportion uplift with `spurious_risk` confounder guard. Optional `SAMUS_EXP_UPLIFT_GATE` requires real uplift before promoting.
- Deliberation router (`backend/common/deliberation.py`): maps `{value, urgency, uncertainty, reversibility}` to reasoning-depth ladder `FAST<STANDARD<DEEP<DEBATE<ESCALATE`. Affordability probe reads LLM-budget store. Advisory and side-effect-free.
- Belief ledger (`backend/cognitive/belief_ledger.py`): durable beliefs with Laplace-smoothed confidence, `contradicted` transition when counter-evidence overtakes support, `contradictions()`/`stale_beliefs()` ranked by economic impact.
- Resilience benchmarks (`backend/experiments/resilience.py`): deterministic perturbation scenarios (cost shock / error storm / channel degraded / calm baseline) run through the real control loop. Not LLM-generated.
- Control-loop friction (`backend/entropy/friction.py`): `decision_entropy` (quota-cut decision flip rate) and `coordination_cost` (mean workcells adjusted per tick).
- Organizational debt (`backend/governance/org_debt.py`): 0..1 debt score per workcell blending karma, LLM-budget efficiency EMA, and circuit-breaker state.

### Rejected

- Runtime ontology mutation — would break the constrained graph-schema security guarantee. Discovered concepts belong in LLM-classification insight reports over existing nodes.

### Test results

5,392 → 5,442 passing. Full suite green.

See [`docs/releases/2.1.1.md`](docs/releases/2.1.1.md).

---

## 2.1.0 — 2026-07-06

Doc-reconciliation release. Code had advanced ~195 commits ahead of reference between v2.0.0 and this cut.

### Added

- Unified business-event ledger (`backend/common/business_events_shim.py` + `/admin/journey`).
- HOTL approval queue (ADR-0019 severity + TTL) with dormant levers armed by default.
- Real-time harm-suppression on `outreach.send_message`.
- Opportunity scoring v2: urgency decay, full cost, confidence, priority.
- Economic task arbiter + channel-level bandit + daily ROI roll-ups (`/admin/economics`).
- Closed reward loop: auto terminal reward on CRM close, voice-arm stamping, outreach ledger.
- Experiment registry + nightly promoter + nightly memory consolidation.
- `cash_engine` self-pacing autonomy: auto_stake sweep → step queue walking audit→proposal→contact→outreach.
- Control-tick quota enforcement wired (`backend/gateway/control_tick.py` ENFORCE stage, clamped [0.25x, 2.0x], TTL-bounded, kill-switch `SAMUS_CONTROL_TICK_ENFORCE=0`).
- CRM conversation projection to Hivemind graph (default ON, `SAMUS_CRM_HIVEMIND_PROJECTION_ENABLED=false` to disable).
- KG auto-tiering on ingest (`SAMUS_KG_TIER_MODE=label`).
- Telegram operator brief channel.
- Social OAuth CLI (`backend/outreach/social_oauth_cli.py`).

### Security

- Immutable integrity gate (`backend/identity/immutable_manifest.py` + Ed25519-signed `immutable_baseline.json`) enforce-by-default: drift in 12 protected files aborts boot in `SAMUS_ENV=production`.

### Deferred

- `path_optimizer` wiring (would override LLM cost-policy).
- DDB GSIs for CRM (full-table scans adequate at current volume).
- Strategy `capability_marketplace` / `credit_ledger` / `trust_scorer` (Phase-5 substrate).

### Test results

5,251 → 5,285 passing.

---

## 2.0.0 — 2026-05-26

Multi-track release: security hardening re-integration, production checkpoint, voice console, perf sweep, axiom layer.

### Security

- Per-service HMAC identity: `SAMUS_HMAC_KEY_<SERVICE>` per-service signing with `SAMUS_SHARED_HMAC_KEY` fallback; signed `X-Samus-Caller` identity folded into MAC; `CALLER_GRANTS` caller→callee authorization matrix (deny-by-default, `SAMUS_AUTHZ_MODE=off|audit|enforce`).
- SSRF-safe fetch (`backend/common/safe_fetch.py`): rejects non-http(s) schemes, blocks private/loopback/link-local/multicast/reserved/CGNAT IPs, re-validates every redirect hop.
- DLQ path validation: service name validated against `^[a-z_]{2,32}$`, resolved path asserted under DLQ root.
- In-process fixed-window rate limiter (`backend/common/rate_limit.py`) wired to LLM-backed and outbound-action routes.
- Atomic Stripe webhook idempotency: `claim_event_id()` (`O_CREAT|O_EXCL`) taken before any side effect; fail-open `verify_stripe_signature` boolean removed.
- Voice Vapi webhook signature check cannot be disabled in production.
- CRM route body-spread injection closed: path ID forced server-side on `AdvanceOpportunityBody`, `UpdateOperatorTaskBody`, `UpsertCallStateBody`.
- SNS SubscribeURL host validated against AWS SNS HTTPS allowlist; `SAMUS_FEEDBACK_VERIFY_SNS` fails closed in production.

### Added

- Voice operator console (`backend/voice/console.py` + `static/console.html`): browser-based, HMAC-exempt, gated by `SAMUS_VOICE_CONSOLE_TOKEN`.
- Pluggable ledger: `JsonlLedger` becomes interface; `FirestoreLedger` sibling for Cloud Run append-only audit.
- KG tiering: `SAMUS_KG_TIER_MODE` + `backend/memory/tiers.py` + `graph_client.promote_node` / `nodes_in_tier`.
- Worker `/health` endpoint for Cloud Run health checks.
- Axiom layer: `axioms/` (`inviolable_axioms.yaml`, `meaning_anchors.yaml`, `ecosystem_phase.signed.yaml`, `informed_consent.spec.yaml`). `backend/governance/efh_evaluator.py` (EFH evaluator). `backend/observability/confusion_emitter.py`.
- Protocol contract: `protocol_contract.yaml` (v0.3.5 AL).
- Calling enrichment: shared call-outcome taxonomy (`backend/crm/call_outcomes.py`), `gatekeeper`/`not_interested`/`hung_up` outcome codes, `contact_validation.py`, `callsheet_intel.py`.

### Changed

- Perf sweep: process-wide reusable `httpx.Client` cache (`backend/common/shared_http.py`); `JsonlLedger` hoisted to worker lifetime (one instance, no per-message mkdir); `JsonlLedger.tail()` reads from EOF in 8KB blocks; `JsonlLedger.rotate_by_age()` archives stale records; `BaseSqsWorker.run_forever` shutdown-cancellable via `stop_event`.

### Test results

2,772 passing at production-checkpoint cut.

---

## 1.9.0 — 2026-05-20

Codebase-wide orphan-functionality remediation. 3 read-only crawl agents surfaced built-but-undispatched workcells, dead-code paths, and telemetry gaps.

### Added

- `signal_filter` wired into `prospecting.process_discovery` as Step 2c pre-qualification gate (toggleable, default on, fail-open).
- `template_recovery` serving deterministic zero-token fallback at three LLM-failure paths in prospecting callsheet.
- Prospecting SQS worker gains action parity with `/work` route.
- SEO `optimize_page`/`generate_content` dispatch routing gets first test coverage.
- Telemetry: `latency_to_resolution_sec`, `estimated_close_probability`, per-vertical token-cost rollups, conversion-funnel ledger (`backend/common/conversion_funnel.py`), `infrastructure_health` scalar.
- Strategy observability: `GET /strategy/bandit-stats` + `read_bandit_stats` work action.
- Control-tick subsystem: `backend/gateway/control_tick.py` (`POST /admin/control-tick`, `GET /admin/control-ticks`), runs entropy scan + portfolio rebalance per tick, records to `backend/common/control_tick_ledger.py`.

### Fixed

- Latent Cloud Run bug: `r"E:\..."` audit-ledger defaults replaced with `/opt/samus/data/<workcell>/` across `leadgen`, `scaffold`, `prospecting`, `fulfillment`.

### Deferred

- `path_optimizer` wiring (would override LLM cost-policy).
- Control-tick quota enforcement (follow-up, closed in v2.1.0).

### Test results

~150 new tests; 2,531 passing.

---

## 1.8.0 — 2026-05-20

`proposal`, `scaffold`, and `leadgen` workcells — built but with zero inbound dispatchers — wired into the inbound deal funnel.

### Added

- `crm.service.advance_opportunity` dispatches `generate_proposal` to proposal workcell on non-terminal advance to `proposal` stage.
- `proposal.service.generate_proposal` dispatches `scaffold` on approved result; scaffold registers rendered pack as CRM artifact.
- `test_leadgen_vapi_contract.py` pins Samus-side entry points against literal Vapi Node 4 payload.
- All three dispatch hops best-effort and zero-LLM.

### Fixed

- `leadgen` and `scaffold` audit-ledger default path fixed (Cloud Run compatibility).

### Test results

21 new tests; 2,422 passing.

---

## 1.7.0 — 2026-05-20

`strategy` workcell wired into the daily loop via Decide→Attribute→Learn→Persist cycle.

### Added

- DynamoDB-backed bandit store (`backend/strategy/bandit_store.py`, table `samus_strategy_bandit`, atomic ADD, JSON-file fallback).
- `process_discovery` Step 2.6 calls `portfolio_manager.select_best_policy(industry)` and stamps `ProspectRecord.policy_family`.
- `policy_family` + `industry` + reward-signal snapshot ride through operator tools onto `Opportunity`.
- CRM terminal transition dispatches outcome to strategy workcell; `strategy.record_outcome` builds `RewardSignal` and calls `update_policy_bandit`.
- Per-prospect LLM cost (`ProspectRecord.llm_cost_usd`) flows into `RewardSignal.token_cost_usd`.

### Test results

~92 new tests; 2,401 passing.

---

## 1.6.1 — 2026-05-20

Lead scorer rebuild and security tie-breaker on call list.

### Changed

- Scorer replaced: four continuous equally-weighted 25-point components (industry fit, review rating, review volume log-scaled, SEO opportunity as inverse of `seo_score`). Old step-function scorer had unreachable `hot` threshold.
- `classify_priority` thresholds recalibrated: hot ≥70, warm ≥45.
- `process_discovery` reordered: SEO + enrichment (Step 2a) now runs before lead scoring (Step 2b).
- Call-list sort gains `security_grade` as tie-breaker after `lead_score`; worse grade ranks higher.

### Added

- `ProspectRecord.security_grade` (A/B/C/D/F) lifted from v1.6.0 security audit.

---

## 1.6.0 — 2026-05-20

Passive security and trust-posture audit added to customer-facing SEO report.

### Added

- `backend/seo/security_audit.py`: strictly passive, non-exploitative, zero-LLM. Six check families: HTTP security headers, TLS certificate, email-authentication DNS (SPF/DMARC/DKIM/CAA via `dnspython`), WordPress platform exposure, exposed artifacts, cookie flags + mixed content.
- `SecurityFinding` Pydantic model with `severity` (adds `info` tier), `risk`, `remediation`.
- "Security & Trust Posture" section in SEO report with A-F grade and remediation checklists.
- `SAMUS_SEO_SECURITY_AUDIT_ENABLED` toggle (default True).

### Test results

44 new tests in `test_seo_security_audit.py`.

---

## intake-hardening — 2026-05-20

Four passive-review findings on public-facing ingestion paths closed.

### Security

- DynamoDB-backed per-source-IP rate limiter on `POST /intake/onboarding` (`backend/intake/rate_limit.py`): fixed-window, keyed in `samus_idempotency` table, atomic ADD, TTL'd; defaults 5/min + 30/hour per IP + 600/hour global ceiling; fails OPEN on backend error.
- Optional Cloudflare Turnstile CAPTCHA (`backend/intake/captcha.py`): activates when `SAMUS_INTAKE_CAPTCHA_SECRET` is set; fails CLOSED.
- Header-injection defence: CR/LF stripped from `company`/`email` before email Subject; `pain_points` fenced and per-line-prefixed.
- `intake.app._client_ip` takes `SAMUS_INTAKE_TRUSTED_PROXY_HOPS` positions from right of `X-Forwarded-For` (not leftmost / spoofable).
- `finance.webhook.handle_stripe_webhook` rejects `livemode=false` events in production.

---

## 1.5.2 — 2026-05-20

Prospecting institutional-exclusion filter.

### Added

- Government / public-sector exclusion at discovery time by Google Places `types`.
- Operator denylist (`backend/prospecting/exclusions.py`): `EXCLUDED_DOMAINS` + `EXCLUDED_NAME_SUBSTRINGS`, applied before per-zip cap.

---

## 1.5.1 — 2026-05-20

Follow-up upsell signal in morning brief.

### Added

- `crm.service.list_follow_ups_due` runs `_suggest_upsell`: keyword-maps prospect conversation corpus to catalog SKU; fills `FollowUpDue.upsell_sku` / `upsell_name` / `upsell_pitch`. Zero-LLM. Never guesses when signal ties or clears no bar.

---

## 1.5.0 — 2026-05-20

Sent outreach feeds the operator daily call list as a follow-up.

### Added

- `CallStateValue.outreach_sent`; `FollowUpDue` / `FollowUpList` models; `crm.service.list_follow_ups_due(today)`.
- `outreach.service.send_message` best-effort dispatches Conversation + CallState upsert to CRM on email success.
- Morning brief SALES section renders "Follow-ups due" lane.

### Fixed

- `crm.persistence.safe_scan` was applying `Limit` as scan window, silently dropping filtered rows past first page. Now paginates via `ExclusiveStartKey`.

---

## 1.4.0 — 2026-05-19

Strategy workcell moves from reactive UCB1 toward predictive reward-density allocation.

### Added

- `reward_density.py`: `RewardSignal` + `compute_reward_density()` weights success by enrichment depth, SEO gap, infra health, token-efficiency, latency penalty.
- `momentum_tracker.py`: per-vertical `IndustryForecast` (momentum, EMA trend).
- `regret_engine.py`: cumulative regret + regret-per-token.
- `saturation_monitor.py`: per-vertical `saturation_risk`.
- `predictive_allocator.py`: `forecast_score()` + `should_proactively_shift()`, feeding `forecast_density` trigger that fires before `bandit_divergence`.
- `policy_compiler.py`: deterministic Closer Policy Compiler emitting `CloserExecutionProfile` (cadence, channel priority, proposal depth, token budget, retry policy) per vertical. Policy surface only — no autonomous execution.
- `portfolio_manager.py` gains reward-density-weighted `update_bandit` + hierarchical `update_policy_bandit`/`select_best_policy` (arms are industry→policy-family).

---

## 1.3.0 — 2026-05-19

Five new deterministic (zero-LLM) workcells forming an observe/decide/recover/coordinate layer.

### Added

- `signal_filter`: 7-axis `ProspectSignal` → `should_enqueue` weighted-score admission ≥0.62. Keeps low-probability prospects out of queue/LLM/SEO/outreach paths.
- `path_optimizer`: `efficiency_ema`-driven execution-route selection (autonomous_llm / hybrid_template / deterministic_scaffold / safe_static_fallback).
- `template_recovery`: deterministic constant-time scaffold library; zero additional token spend on LLM failure.
- `portfolio_controller`: portfolio-level signal tracking + adaptive token-quota/priority-weight rebalancing.
- `entropy`: weighted `entropy_score` from queue/error/retry/LLM-failure signals → countermeasure recommendations.

Stack is now 21 HTTP workcells + 10 SQS worker sidecars.

---

## 1.2.0 — 2026-05-19

Prospecting pipeline overhaul.

### Added

- Cross-`(zip, industry)` dedupe + per-industry quota in `service.process_discovery`.
- `scorer.INDUSTRY_WEIGHTS` extended with 7 Tier A+B verticals.
- Per-prospect `seo_score` via `crawler.fetch_homepage` + `seo_audit.score_seo` (Step 2.5).
- `backend.prospecting.enrichment`: three-stage cascade (homepage → `/contact`+`/about` → `mbasic.facebook.com`) populating owner_email + social handles + JSON-LD owner_name.
- Step 2.7: full `seo.audit_and_report` pipeline for warm/hot prospects; report written to `artifacts/customers/<slug>/seo_report.md`.
- `scripts/Run-ProspectingDaily.ps1` (geo-ring state machine), `Register-ProspectingDailySchedule.ps1` (07:30 daily), `Run-SeoAudit.ps1` (one-shot).

### Fixed

- Real-estate-fills-the-cap regression: `process_discovery` now enforces per-industry quota, not per-zip total.

---

## 1.1.0

LLM token minimization plan.

### Added

- 4-layer LLM cost control: global $1/day cap + model floor + circuit breaker + prompt caching.
- Per-call deterministic top-N gates in prospecting, SEO, intake.
- AST lint guard for LLM callers.
- Event-driven portfolio triggers (`backend/strategy/triggers.py`).
